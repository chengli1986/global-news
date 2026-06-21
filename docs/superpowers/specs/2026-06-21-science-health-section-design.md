# 设计：global-news 邮件新增「科学·健康」板块（待办 C 最小版本）

- **日期**：2026-06-21
- **状态**：设计已批准，待写实现计划
- **范围**：global-news 邮件摘要新增一个 LLM-fed 板块 `🔬 科学·健康 SCIENCE & HEALTH`，收拢当前散在「社会观察」板块的基础科学 + 医学健康文章。
- **前序**：方案 B（文章级分区路由，`2026-06-14-category-driven-region-grouping-design.md`）+ rss-production-review 待办 C 提醒（`2026-06-13-rss-production-quality-review-design.md`）。

## 1. 背景与动机

rss-production-review 周报长期挂着「方案 C 待办」提醒：`healthcare`/`vertical`/`global_south` 三类 category 的源近 30 天约 1480 篇入选，无专属板块，散落在「社会观察」等现有板块里，稀释了 STAT/Quanta/KFF/Guardian Science 等高质量源的价值。

根因：Stage 4 LLM 的 topic 标签只有 `{politics, business, tech, consumer_tech, society}`，没有 science/health。科学/医学文章被判成 `society` → 落进 `REGION_SOCIETY`。

本设计是方案 C 的**最小第一步**：只建「科学·健康」一个板块，上线后跑两周看入选结构，再决定是否做第二步「深度·专题」（收 ProPublica/Foreign Policy 等 vertical 调查/地缘源）。第二步**不在本次范围**。

## 2. 已确认的设计决策

| 决策点 | 选定 | 理由 |
|--------|------|------|
| 收录范围 | 基础科学（物理/数学/天文/生物）+ 医学健康/公共卫生 | 聚焦、边界清晰；气候/环境、消费科技不收 |
| topic 标签 | 单一 `science_health`（科学+健康合一） | 最小版本，对应单一板块 |
| china geo 路由 | 主题优先 → 进科学·健康（china 不拦截） | 与 tech_ai/politics 一致，中国科学文章与全球可比 |
| 路由方式 | 纯文章级 LLM 路由（方式 1） | 与方案 B 架构同方向；文章级精度（源的非科学文仍各归各板块） |
| 板块名 | `🔬 科学·健康 SCIENCE & HEALTH` | 中英 + emoji，与现有板块风格一致 |
| 显示位置 | 「经济学人」之后、「社会观察」之前（第 10 位） | 两个 LLM-fed 软主题相邻，科学·健康优先级略高于泛社会观察 |

**否决方案**：
- 方式 2（source category 软锁定整源进板块）——违背文章级精度，且与方案 B 刚删除的「源级路由」反向，开倒车。
- 方式 3（LLM + 关键词兜底）——科学主题关键词难枚举、易误伤，复杂度不值最小版本。
- 收录范围扩到「广义知识·深度」一锅——等于直接做方案 C 全量，违背最小版本本意。

## 3. 实现改动清单

全部集中在 `unified-global-news-sender.py`，共 5 处代码改动 + prompt + 测试 + 文档。

### 3.1 新增区域常量（L72 区）
```python
REGION_SCI_HEALTH = "🔬 科学·健康 SCIENCE & HEALTH"
```

### 3.2 扩 topic 标签（L44）
```python
TOPIC_LABELS = frozenset({"politics", "business", "tech", "consumer_tech", "society", "science_health"})
```
`SUBTOPIC_LABELS` 不动——`science_health` 无子类（与 politics/society 同列），不进 `SUBTOPIC_LABELS`，校验逻辑（L976）自然跳过。

### 3.3 REGION_GROUPS 加空 zone（L666 区）
在 `REGION_ECONOMIST` 条目之后、`REGION_SOCIETY` 条目之前插入：
```python
(REGION_SCI_HEALTH, []),  # LLM-fed (science/health topic routed via Stage 4)
```
空源列表 = 纯 LLM-fed，与 `REGION_CONSUMER_TECH`/`REGION_SOCIETY` 同模式。

### 3.4 路由分支（`_route()`，L1262 `topic == "society"` 分支之前）
```python
if topic == "science_health":
    return (REGION_SCI_HEALTH, "llm:topic:science_health")
```
放在 4b 主题优先区。因为 4a 仅对 `china + {society, business, consumer_tech}` 拦截，`science_health` 不在该集合，china geo 自然落到此分支 → 主题优先，符合决策表。

### 3.5 LLM prompt（L880 区 topic 定义 + 示例）
topic 定义新增：
> `"science_health"`: scientific discoveries (physics/math/astronomy/biology), medicine, public health, medical research. NOT climate/environment (→ politics), NOT consumer gadgets (→ consumer_tech).

新增 3 个示例（含 1 个 china 高歧义、1 个边界）：
- `"James Webb finds early galaxy"` → `{topic:"science_health", geo:"us"}`
- `"FDA approves new Alzheimer's drug"` → `{topic:"science_health", geo:"us"}`
- `"中国团队发表癌症免疫疗法突破"` → `{topic:"science_health", geo:"china"}`（china 不拦截）

### 3.6 渲染：零改动
`_collect_region_articles` 的 `region_order = [rt for rt, _ in self.REGION_GROUPS] + [REGION_OTHER]`（L1314）自动包含新 zone；render loop（L1767）直接遍历。无需改动。

### 3.7 rss-production-review.py：零改动
待办 C 检测已埋好（`rss-production-review.py:307-310` 读 sender 文件、判断是否含字符串 `REGION_SCI_HEALTH`）。sender 一出现该常量，周报提醒条自动消失。

## 4. 既有行为（知会，不处理）

渲染循环对**每个** region 先无条件渲染标题（L1767-1781），空板块会显示 `🔬 科学·健康 (0)` 空标题。这是全站既有行为，非本次引入。本板块对应源近 30 天约 1480 篇入选 ≈ 日均 ~49 篇，几乎不可能出现 0 篇，故不额外处理（YAGNI）。

若未来想统一隐藏空板块，可在 render loop 加 `if not region_articles: continue`，但会改变所有板块行为，应另开任务、单独测试，不混入本次。

## 5. 测试（TDD）

- `tests/test_region_routing.py`：
  - `_route("science_health", "us", None)` → `REGION_SCI_HEALTH`
  - `_route("science_health", "china", None)` → `REGION_SCI_HEALTH`（验证 china 不被 4a 拦截）
  - `_route("science_health", "global", None)` → `REGION_SCI_HEALTH`
- `tests/test_classification.py`：
  - `science_health` 在 `TOPIC_LABELS` 中、LLM 校验（L971）放行不被 drop
- 回归：跑全量测试套件（当前 301）确保零回归。

## 6. 文档同步

- `README.md`：板块数 / 区域列表更新（+1 板块）。
- `.claude/CLAUDE.md`（global-news repo）：如列出板块则同步。
- 测试计数若变动，同步 README。

## 7. 上线后观察与第二步

- 上线后这批源的科学/医学文章自动进新板块；周日 rss-production-review 邮件待办 C 提醒条自动消失。
- 跑两周后评估：新板块日均篇数、命中源结构、误分率（科学文是否真进了、非科学文是否误入）。
- 据此再决定是否做**第二步「深度·专题」**（ProPublica/Foreign Policy 等 vertical 调查/地缘源）。`global_south`（实质仅 Daily Maverick）量太薄，折进现有区域板块，不单开。第二步另起 spec。

## 8. 不做（YAGNI）

- 不做「深度·专题」「全球南方」板块（本次只做科学·健康）。
- 不改 source category / registry。
- 不改源级路由（方案 B 已删，不复活）。
- 不统一隐藏空板块（既有行为，本板块不触发）。
- 不加科学主题关键词兜底。
