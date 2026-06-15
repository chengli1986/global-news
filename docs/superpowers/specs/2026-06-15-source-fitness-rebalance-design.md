# 源池实测优胜劣汰（S&P 式 rebalance）— 设计文档

- **日期**: 2026-06-15
- **状态**: 设计中（待用户 review，重点确认 §6 待定参数）
- **关联**: [rss-production-review](2026-06-13-rss-production-quality-review-design.md)（本设计是它的进化）、[方案 B 文章级分区](2026-06-14-category-driven-region-grouping-design.md)

## 1. 背景与动机

**用户诉求**：让生产源池"整体更优"、沉淀精品（2026-06-15 讨论，明确选"源池整体更优"而非"怕误杀"或"给候选二次机会"）。

**关键判据之争（已厘清）**：不用"发现时的五维打分"做沉淀依据——其中 `uniqueness`(20%) 是相对当下池子的、会过期，`freshness`(15%) 是发现那一刻的快照，用不同时刻、不同稀缺度的旧分数做全局排序会系统性高估"当年稀缺、现在平庸"的源。**真正的"优质"判据是实测贡献**：源在真实邮件里到底有没有人看（`production-source-log` 的 selected）。

> 系统早有此伏笔：`production-source-log` 的设计注释原话即 *"the long-term input to a future S&P-500-style rebalancing of the source list"*。本设计兑现这个未完成的愿景。

**现状缺口**：`rss-production-review`（6-13 上线）只揪"绝对僵尸"（30d `selected ≤ 1`），抓不到"有贡献但明显比同类差"的源——源池缺少**相对优胜劣汰**。

## 2. 目标 / 非目标

### 目标
- 在 `rss-production-review` 上**新增一类"建议轮换"**：按实测贡献做**组内**相对淘汰，沉淀精品。
- **保多元**：绝不因小众领域天然量少而把整个领域砍光。

### 非目标
- 不碰发现五维打分 / pool-cap（那是入场券筛选，不是沉淀机制）
- 不自动 demote（**永远人工确认**，用户硬性要求，复用 `rss-demote-source.py`）
- 不新建 cron（搭现有 `rss-production-review` 周报）

## 3. 核心设计

### 3.1 判据：实测贡献
用 `production-source-log` 的 30d `selected` 总量（与现有 A 僵尸同源数据）。

### 3.2 只在"同类"内比（按 category 分组）—— 保多元的关键
医疗跟医疗比、科技跟科技比。**避免**小众领域（医疗/全球南方/深度调查天生量少）被财经/科技碾压而系统性砍光（那会与方案 B 刚接入 27 个多元源的成果背道而驰）。

### 3.3 legacy 老源豁免（设计决定，待 review）
31 个 legacy 老源（BBC/Bloomberg/FT/Economist/SCMP/NYT/南方周末 等）**无 category**，无法组内比；且它们是人工精选的大媒体、贡献稳定（FT 420/Bloomberg 388 等）。**默认豁免**，rebalance 只作用于有 category 的 discovery 来源（质量参差、正需要持续验证）。
> 开放点：若想把 legacy 也纳入，需先给它们补 category（见方案 B spec 的"彻底模式"讨论）。

### 3.4 "垫底"判定（含低频保护）
某 category 内，源同时满足才标"建议轮换"：
1. 该组源数 **> 保底数 N**（否则整组豁免——见 3.5）
2. 该源 30d selected 是**组内最低**，且 **< 组内中位数的一半**（"明显比同类差"，而非略低）
3. 沿用现有低频保护：`active_days ≥ 7` 样本闸 + 转正满 30d 在岗宽限（不误杀低频高质/新转正源）

### 3.5 领域保底
每个 category **至少保留 N 个**源（默认 N=3）。组内源数 ≤ N 时整组豁免淘汰。
> 实测影响范围：当前有 category 的组里只有 europe(6)/vertical(5)/tech_ai(4)/china_depth(4) > 3，即 rebalance 当前只可能动这 4 组的垫底；healthcare(3)/hk_sea(3)/north_america(2)/global_south(1) 全部因保底豁免。这是保守的（宁可少动）。

### 3.6 与现有 A 僵尸并列
- **A 绝对僵尸**（现有）：`selected ≤ 1`，全局适用（含 legacy）——抓"几乎零贡献"
- **轮换候选**（新增）：组内相对垫底——抓"有贡献但同类里明显最差"
两者互补，都进周报、都建议而非自动执行。

### 3.7 执行 + 腾位
- 搭进 `rss-production-review` 周报邮件，新增"♻️ 建议轮换"段（源 + 组内排名 + 数据 + demote 命令）
- **永远人工确认**才 demote（复用 `rss-demote-source.py`）
- demote 后腾的空位由现有 discovery → trial → 转正管线自然补，**无需额外接线**

## 4. 影响 / 风险

- ✅ 沉淀精品 + 保多元两头兼顾
- ✅ 复用现有脚本/数据/周报，改动集中在 `rss-production-review.py`
- ⚠️ 当前实际只动 4 个大组的垫底（作用范围有限，但安全；随源池增长自然扩大）
- ⚠️ legacy 豁免 = 南方周末(6%)/HKFP(18%) 这类低效老源暂不被轮换（待 review 是否接受）

## 5. 测试策略

pytest（合成 production-source-log + registry fixture）：
- 组内垫底被标轮换；组内中位数以上不标
- 保底：组源数 ≤ N 整组豁免
- 低频保护：active_days < 7 / 在岗 < 30d 不标
- legacy（无 category）不参与轮换
- 与 A 僵尸不重复标记同一源
- 周报含"建议轮换"段 + demote 命令；无候选时不显示该段

## 6. 待定参数（请 review 时重点确认）

| 参数 | 默认 | 说明 |
|------|------|------|
| **每类保底 N** | 3 | 越大越保守（动得越少） |
| **"垫底"阈值** | 组内最低 且 < 组内中位数 ½ | 越严越少误判 |
| 实测窗口 | 30d | 同 A 僵尸 |
| 节奏 | 跟周报（每周看） | 不新建 cron |
| **legacy 豁免** | 是 | 见 3.3，是否接受？ |

## 7. 开放问题（review 后回填）
- 保底 N=3 是否合适？是否要按 category 重要性给不同保底？
- "垫底"阈值松紧（中位数 ½ vs 其他）？
- legacy 老源是否真豁免，还是后续给它们补 category 一并纳入？
