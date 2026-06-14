# Category 驱动的 Production 源自动归组（治本）— 设计文档

- **日期**: 2026-06-14
- **状态**: 设计中（待用户深入讨论老源/板块后定稿）
- **关联**: [rss-production-review](2026-06-13-rss-production-quality-review-design.md)（发现本问题的来源）

## 1. 背景与缺口

global-news 有**两套互不相通的分类系统**：

| 系统 | 位置 | 用途 | 维护方式 |
|------|------|------|---------|
| `category` | `rss-registry.json`（每个源一个字段） | discovery 发现时打的主题标签（9 类） | discovery 自动 |
| `REGION_GROUPS` | `unified-global-news-sender.py:665` | 邮件排版板块 + 配额竞争分区 | **手工硬编码源名清单** |

`_collect_region_articles`（`:1303`）**只遍历 `REGION_GROUPS` 清单里的源**。不在清单的源，其文章连"被分区/被 Stage 4 改判"的机会都没有，直接兜底进"其他"区**全量展示**（`_count_trial_selected` 的 `in_ungrouped` 分支）。

**断层根因**：discovery/trial 自动毕业新源时只更新 registry/config，**从不同步那份手工 `REGION_GROUPS` 清单** → 每个新源默认掉进"其他"区。

**现状量化**（2026-06-14 实测）：
- production 59 源中 **28 个有 category（全在"其他"区裸奔）/ 31 个无 category（legacy 老源，靠手工清单归组）**
- "其他"区 27 个源（含 STAT/Foreign Policy/Wired/Politico Europe/澎湃/端传媒 等优质源）全量展示、不参与任何配额竞争

> 历史注：曾有过 `_CATEGORY_TO_REGION` 映射，Task 6 删除改为 Stage 4 LLM routing（`:704` 注释）。本设计以更稳的形式复活"category→组"映射，且与现有 LLM routing 共存。

## 2. 目标与非目标

### 目标
- 让**有 category 的源按 category 自动归入对应板块**（治本：以后 discovery 毕业的源自动归位，断层永久消除）
- "其他"区大幅缩小（理想接近空）

### 非目标（YAGNI）
- **不动 31 个无 category 老源**（继续走手工 `REGION_GROUPS` 清单 → 零回归）
- **不补全老源 category**（混合模式即达成治本目标）
- **不改 Stage 4 LLM routing 矩阵**（`_route`）
- 不做 UI

## 3. 设计

### 3.1 核心：`CATEGORY_TO_REGION` 映射表（新增常量）

```
CATEGORY_TO_REGION = {
    "tech_ai":       REGION_AI_FRONTIER,
    "china_depth":   REGION_CHINA,
    "hk_sea":        REGION_ASIA_PAC,
    "europe":        REGION_POLITICS,
    "north_america": REGION_POLITICS,
    "healthcare":    REGION_SCI_HEALTH,    # 新组
    "vertical":      REGION_DEEP_DIVE,     # 新组
    "global_south":  REGION_DEEP_DIVE,
    "global_finance": REGION_MACRO_MARKETS, # 当前 production 无此 category 源，映射备将来
}
```

当前各 category 源数（有 category 的 28 个）：tech_ai 4 / china_depth 4 / europe 6 / north_america 2 / healthcare 3 / vertical 5 / hk_sea 3 / global_south 1。

### 3.2 新建 2 个板块

- `REGION_SCI_HEALTH` =「🔬 科学/健康」（收 healthcare：STAT News / KFF Health News / The Guardian Science）
- `REGION_DEEP_DIVE` =「📑 深度/专题」（收 vertical + global_south：Foreign Policy / ProPublica / Quanta / Carbon Brief / IPS News / Daily Maverick）

两组加入 `REGION_GROUPS`，**源名清单留空**（成员由 3.3 的 category 派生动态填充）。

**验证发现（修正"挪用空组"的原方案）**：原计划改造现有 2 个空组（消费科技/社会观察），但 `_route`（`:1251/1254/1263`）显示这俩空组实际在接 Stage 4 LLM 路由（consumer_tech / society 主题文章）——**空的只是源名清单，不是没内容**。故改为**新建独立组**，2 个空组保留不动。板块总数 10 → 12。

### 3.3 源→组 派生（混合模式核心）

构建一张 `{源名: 板块}` 映射（建议在 sender 初始化时一次性构建），优先级：

1. **手工 `REGION_GROUPS` 清单**（31 个老源，**最高优先**，保持原归属）
2. 手工没有 → 查 registry 该源 `category` → `CATEGORY_TO_REGION` → 板块（28 个新源自动归位）
3. 都没有 → 兜底"其他"区（今后理想为空）

`_collect_region_articles` 改为用这张派生表确定每个源的 default 板块，**取代当前"只遍历手工 source_names"**。Stage 4 的 `_reclassify_article` 逻辑不变（文章仍可被 LLM 改判到其他板块）。

### 3.4 配额

`digest-tuning.json` `region_quotas` 新增 2 个 key，沿用空组量级：
- 科学/健康：`min 3 / max 8`
- 深度/专题：`min 6 / max 10`

现有组配额**不变**（实测现有组 max 足够装下新增源：AI/前沿 13+4=17≤20、全球政治 6+8=14≤22、亚太 6+3=9≤14、中国要闻 3+4=7≤22）。上线后随 rss-production-review 快照观察 1–2 周再调。

### 3.5 已知行为（可接受）

`_route` 不认识这 2 个新组，所以新归组源的文章**仍可能被 Stage 4 LLM 改判**到现有组（如某 healthcare 文章被判 `topic=society` → 进社会观察组）。这不影响治本目标——目标是"让源进入分区系统、参与配额竞争"，而非钉死每篇文章去向。新组的稳定内容 = 该组 category 源中未被 LLM 改判走的文章。

## 4. 影响与风险

- ✅ 31 个老源、现有板块**零变化**（混合模式）
- ⚠️ 28 个新归组源从"全量展示"→"配额竞争"，**曝光下降**（已与用户确认接受；换取版面均衡 + 冷门组 min 保底 + 主题板块清晰）
- ✅ "其他"区大幅缩小
- 风险：动了核心 `_collect_region_articles`（每天 3 封邮件的排版）→ 必须充分测试 + 真实数据对拍

## 5. 测试策略

pytest，覆盖：
- `CATEGORY_TO_REGION` 映射完整性
- 源→组 派生三优先级：手工优先（老源不变）/ category 兜底（新源归位）/ fallback 其他
- 2 个新组出现在 `REGION_GROUPS`，且能被 quota 逻辑识别
- 集成：有 category 的源文章进对应板块（不再 fall through 其他区）
- **向后兼容**：31 个老源归属与改动前逐一一致（防回归）
- 真实数据对拍：改动前后各源所属板块 diff，人工确认符合预期

## 6. 开放问题（待用户深入讨论）

> 用户已预告：定稿前要就**老源 / 板块**做更深入的讨论。以下为可能的议题，讨论后回填并更新本 spec：
- 31 个无 category 老源是否要补 category（从混合 → 彻底纯派生）？
- 板块体系是否要进一步重整（现有 12 组是否有合并/拆分空间）？
- europe / north_america 都并入"全球政治"是否会让该组过载？是否需要独立"欧洲"组？
- 新组命名 / emoji / 排序位置？
