# 文章级分类驱动的源自动归组（治本，方案 B）— 设计文档

- **日期**: 2026-06-14
- **状态**: 设计中 v2（方案从"源级 category 映射"改为"放开排版入口、复用已有文章级 LLM 分类"；用户 2026-06-14 拍板**先 B 后 C**）
- **关联**: [rss-production-review](2026-06-13-rss-production-quality-review-design.md)
- **取代**: 本文件 v1（源级 `CATEGORY_TO_REGION` 映射 + 新建 2 组）—— 经讨论否决，见下"认知反转"

## 1. 背景与认知反转

**两套分类系统**（同 v1）：registry 的 `category`（discovery 自动，9 类） vs sender 的 `REGION_GROUPS`（手工硬编码源名清单，邮件板块+配额分区）。

**认知反转（v1→v2 的关键）**：pipeline **早已有文章级 LLM 实时分类**：
- `classify_articles`（`:749`）对 **`self.news_data` 全部源的每篇文章**跑 4-stage + LLM(gpt-4.1-mini)，打 `(topic, geo, subtopic)` 标签，`_route`(`:1223`) 文章级 route 到 region。
- 主题体系 `TOPIC_LABELS = {politics, business, tech, consumer_tech, society}`（`:44`）。
- **每篇文章按自己的标题分类**——天然解决"一个源产出多 category 新闻"的难点（不看源、只看文章）。

**真正的断层**：渲染入口 `_collect_region_articles`（`:1303`）**只遍历 `REGION_GROUPS` 手工清单的 35 个源**；不在清单的 27 个源，文章被 `ungrouped` 逻辑（`:1859`）**整批塞进"其他 OTHER"板块** —— 它们的 LLM 标签打好了，**渲染时却没用上**。

> 所以问题不是"缺文章级分类功能"，而是"排版那步把 27 个源漏了"。v1 的源级 `CATEGORY_TO_REGION` 映射会与这套已有的文章级分类**冗余打架**，且把多 category 的源钉死——故否决。

## 2. 目标 / 非目标

### 目标（方案 B）
- **放开渲染入口**：让所有 production 源的文章都进 `_collect_region_articles`，用每篇**已有的 LLM 文章级标签** route 到板块。
- 解决"一个源多 category"（文章级天然处理）。
- "其他"区大幅缩小（理想接近空）。
- **完全复用现有引擎**：不动 4-stage 分类器、不扩主题、不建新组。

### 非目标
- 不扩 `TOPIC_LABELS`、不改 `_route` 矩阵、不建新板块 → 这些归**方案 C**（后续阶段，§6）
- 不动 31 个老源的归属（向后兼容、零回归）

## 3. 设计（方案 B）

### 改动 1：`_collect_region_articles` 遍历所有源
- **现**：`for region_title, source_names in REGION_GROUPS: for src in source_names:` —— 只收清单内源。
- **改**：遍历 `self.news_data` 全部源；每篇文章查 `self._classifications[(src, idx)]` 的 `region`：
  - `region` 非空 → 进该板块（已由 Stage 4 LLM route）
  - `region is None` → fallback `_source_default_region(src)`

### 改动 2：`_source_default_region` 新源走兜底（**删除 category 映射**）
仅在文章 `region is None`（`_route` 遇畸形/unknown topic-geo 组合的少数情况，或 EMERGENCY 降级模式）时触发：
- **老源**（在手工 `REGION_GROUPS` 清单）：用清单默认组（**不变，向后兼容**）
- **新源**（不在清单）：→ "综合/其他"兜底区（改动 3）

> **去掉了 v1 残留的 `CATEGORY_TO_REGION` 源级映射**（经讨论确认多余）。主路径完全是文章级 LLM route，category 映射只服务"LLM 未给标签 × 新源"这个极小交集，而这些文章进"综合"兜底区已足够——不值得为它引入 registry 读取 + 维护一张映射表（DRY/YAGNI）。**排版从此只认文章级标签，完全不碰 registry `category`。**

### 改动 3：`ungrouped`/"其他"区
B 后所有源都进分区，`ungrouped`（`:1859`/`:2072`）只剩"无 LLM 标签 + 无 category 默认"的极少数。保留为小兜底"综合 OTHER"，理想接近空。

### 配额
B **不建新组**，沿用现有 `region_quotas`。27 源文章涌入现有组会加剧竞争——上线后随 rss-production-review 快照观察 1–2 周，看哪组 max 不够再调。

## 4. 已知结果（B 的预期，已与用户确认接受）

`TOPIC_LABELS` 无 science/health，故 healthcare/vertical 文章**主要靠 LLM 散进现有板块**（不集中成专属板块）：
- healthcare（STAT/KFF/Guardian Science）→ 多半 `society`，部分 `tech`
- vertical：Foreign Policy/ProPublica→`politics`/`society`；Quanta→`tech`/`society`；Carbon Brief→`society`/`business`；IPS→`politics`
- 少数无 LLM 标签的 → 走改动 2 兜底（老源手工默认 / 新源进"综合"区）

这是 B 的设计取舍：先用现成引擎把 27 源接入分区竞争（消除"其他区裸奔"），暂不追求专属新板块。

## 5. 影响 / 风险

- ✅ 31 老源、现有板块逻辑**零变化**（手工 fallback 路径不变）
- ⚠️ 27 源文章从"其他区全量展示"→"现有板块配额竞争"，**曝光下降**（已接受；换版面均衡）
- ✅ "其他"区从 27 源缩到接近空
- ⚠️ 动核心 `_collect_region_articles` + `_source_default_region` + `ungrouped` 渲染（每天 3 封邮件排版）→ **必须充分测试 + 真实数据改动前后对拍**

## 6. 方案 C（后续阶段，本次不实现）

**本质（经讨论厘清）**：现 `TOPIC_LABELS`（5 类）对照 registry 的 9 类 category，**正好缺 healthcare（健康/医疗）与 vertical（科学/深度调查）两个维度**——这两类文章因此无法被文章级精确识别，只能挤进 `society`/`tech`。其余 7 类 category 都能由 `topic+geo` 表达。所以 C = **给 LLM topic 补上这 2 类**。

具体：扩 `TOPIC_LABELS`（+health/science 等）+ 改 LLM prompt + 改 `_route` 输出新组 + 建「科学/健康」「深度/专题」组 + 配额 + **重新验证 gpt-4.1-mini 对新主题的识别质量**。动 4-stage 分类器核心、风险高，**届时用 B 上线后的真实散落数据指导扩哪些类**，单独 spec。

## 7. 测试策略

pytest：
- `_collect_region_articles` 遍历所有源、按 `_classifications` route（有标签进对应组）
- `_source_default_region` 三级 fallback（手工优先 / category 默认 / 兜底）各分支
- **向后兼容**：31 老源的文章归属与改动前逐一一致（防回归）
- 集成：27 源的文章按 LLM 标签进区域板块，不再整批落"其他"区
- 真实数据对拍：改动前后每篇文章所属板块 diff，人工抽查符合预期

## 8. 已敲定决策

- **删除** `CATEGORY_TO_REGION` 源级映射（多余，见改动 2）—— 排版只认文章级 LLM 标签
- 新源的 LLM-未分类文章 → 进"综合/其他"兜底区；**保留一个极小兜底**（不强制归类，以免把无标签文章硬塞错板块）
- 老源 fallback 路径不变（向后兼容）
