# RSS Production 源在岗质量复查 — 设计文档

- **日期**: 2026-06-13
- **状态**: 设计已批准，待写实现计划
- **作者**: brainstorming 协作（用户 + Claude）
- **关联**: [rss-source-discovery](2026-04-06-rss-source-discovery-design.md) · [rss-trial-manager](2026-04-11-rss-trial-manager-design.md)

## 1. 背景与缺口

global-news 的源治理目前是"只进不出"：discovery 不断找新源 → 0.85 门槛 → 3 天 trial 双门槛 → 转正 production。源**转正后**只剩 `rss-health-check`（可达性 + stale，3x/天自动换源）在看，**没有任何逻辑复查它在岗后的贡献度与内容质量**。

关键事实（已核实 2026-06-13）：

| 维度 | 现状 |
|------|------|
| 可达性 + 新鲜度 | ✅ `rss-health-check.py`，连续失败 3 次自动 swap fallback、恢复自动 revert |
| 贡献度 + 内容质量数据采集 | ✅ `logs/production-source-log.jsonl`（Phase 0 fitness telemetry，2026-05-26 起），每源每次发送记 `fetched/selected/avg_title_len/avg_desc_len/pct_with_desc/pct_with_author` |
| 退役执行工具 | ✅ `rss-demote-source.py`（手动 CLI，registry+config 同步 demote） |
| **贡献度/质量评估 → 触发 demote** | ❌ **完全缺失**：`production-source-log.jsonl` 零消费端 |

**结论**：数据基础（telemetry）和退役工具（demote CLI）都已就位，缺的只是中间那个"周期性读 telemetry、判断源是否该退役"的**评估器**。trial 源有 `trial-manager` 做"评估 → keep/remove"，production 源缺少对称的"在岗复查 → demote"环节。

> 佐证价值：该 telemetry 是 5-26 加的，5-27 就靠人工翻它发现了 Endpoints News + Nikkei Asia 的僵尸 drift（见 `rss-demote-source.py` 注释）。数据确实能暴露问题，只是目前靠人偶尔看。

## 2. 目标与非目标

### 目标
- **A. 揪僵尸源**：转正后长期几乎不被 selected 的源 → 自动识别 + 邮件建议 demote
- **B. 防悄悄变质**：源仍可访问但内容退化（描述变空 / 变标题党 / 署名消失）→ 邮件预警
- 保障 production RSS pool 的质量与稳定输出

### 非目标（YAGNI）
- **不自动执行 demote** —— 永远人工口头确认后执行（复用 `rss-demote-source.py`）
- **不替代 `rss-health-check`** —— 可达性 / stale / 自动换源不在本机制职责内
- 不做 dashboard / UI
- 不改动 discovery / trial / classifier 任何逻辑
- 不新增数据采集（复用现有 `production-source-log.jsonl`）

## 3. 判定逻辑

### 3.1 A — 僵尸源（自动识别，建议 demote）

僵尸 = **"源还在发文，但发的文一直没人要"**。在滚动窗口 `W = 30 天`内，候选须**同时满足**：

1. registry `status == production`
2. **在岗宽限**：转正满 30 天（registry 无转正日期的 legacy 源视为早已在岗，通过）
3. **源还活着**：窗口内 `sum(fetched) > 0`
4. **样本充足**：窗口内"有 fetched>0 的不同日期数 ≥ 7"——否则样本不足，**跳过不判**（这是低频源的安全阀：周刊类源 30 天内出文天数少，不会被误判僵尸）
5. **几乎零贡献**：窗口内 `sum(selected) ≤ 1`

→ 标记为**僵尸候选**，邮件中附可直接执行的命令。

**职责边界**：`sum(fetched) == 0`（源没出文）**不算僵尸**，交给 `rss-health-check` 的 stale/可达性逻辑，二者不重叠。

**设计取舍**：低频源因"样本不足"被跳过 = 宁可漏判、不可误杀。低频高质源数量少，人工偶尔看即可。

### 3.2 B — 内容变质（仅预警，纯人工判断）

复用 telemetry 已有的内容质量字段，**对比该源自身历史基线**（绝不用绝对阈值——每个源 RSS 风格不同，Foreign Policy 类天生用短摘要）：

- 近期 = 最近 7 天的中位数；基线 = 最近 7 天**之前**的历史中位数（两段不重叠，避免近期数据稀释基线）
- **最小样本**：基线 ≥ 10 条、近期 ≥ 5 条记录才比较，否则跳过
- 任一信号触发预警：
  - `pct_with_desc`：基线 > 0.8 且 近期 < 0.3 → **描述变空**（付费墙截断 / RSS 退化）
  - `avg_desc_len`：近期 < 基线 × 0.4（缩水 > 60%）→ **变标题党**
  - `pct_with_author`：基线 > 0.5 且 近期 < 基线 × 0.5（腰斩）→ **署名消失**

→ 列入邮件"⚠️ 变质预警"段，纯信息性，**不给 demote 建议**，完全由用户判断。

> 所有阈值均为测试期初值，按观察结果调整。

## 4. 产出：邮件报告

复用现有 `curl` SMTP 模式（同 discovery/trial-manager），发往 `ch_w10@outlook.com`。

- **A 段（僵尸候选）**：表格 `源名 / 类别 / 30天 fetched / 30天 selected / 在岗天数`，每行附可粘贴命令：
  `python3 ~/global-news/rss-demote-source.py --name "X" --reason "zombie-30d-no-selected"`
- **B 段（变质预警）**：表格 `源名 / 退化信号 / 基线 → 近期`
- **全池 fitness 快照（测试期）**：所有 production 源的 30 天 fetched/selected 概览，供用户评估判定是否合理
- Subject: `[RSS Pool 复查] 周报 — N 僵尸候选 / M 变质预警 — MM月DD日`

**测试期 vs 稳定期**：测试期**每次都发**（含全池快照，透明度优先，便于评估输出质量）；稳定后可改为"无异常则静默"。

## 5. 执行模型

```
rss-production-review.py run   (周度 cron)
   │ 读 production-source-log.jsonl (30d 窗口) + rss-registry.json
   ▼
 算 A 僵尸候选 + B 变质预警 + 全池快照
   ▼
 发 HTML 邮件报告  ──────────────►  用户阅读
                                      │ 口头确认某源该 demote
                                      ▼
                          rss-demote-source.py --name ... --reason ...
                          （已存在，registry+config 同步，零新开发）
```

- demote **永远人工确认**，机制本身不写任何配置 → 无破坏性操作 → 无需自动 demote 的防误杀兜底
- cron 用 `~/cron-wrapper.sh` 包装（timeout / lock / JSONL / 失败告警），与其他任务一致

## 6. 频率与数据约束

- **测试期：周度**（建议周日固定时段，错开 discovery 04:15 与 health-check）
- 滚动窗口固定 30 天，与频率独立（每周看一次过去 30 天）
- **数据现状约束**：`production-source-log.jsonl` 自 2026-05-27 起，今日仅 ~17 天，**未满 30 天窗口**。故测试初期 1–2 周 A 段可能偏空/保守（数据量 + 在岗宽限不足），属预期；测试期先重点验证 B 段与整体输出格式。数据攒满 30 天（约 2026-06-26 后）A 段才完整。
- 同一僵尸源在被 demote 前会连续数周出现在报告里（30 天窗口内持续命中）——测试期视为"稳定性确认"特性，非缺陷。

## 7. 测试策略

pytest，合成 `production-source-log.jsonl` fixture 覆盖：

- 高频僵尸源（天天 fetched、selected=0）→ 判僵尸 ✅
- 低频高质源（出文天数少、selected 少但 > 1）→ 不判僵尸（样本/贡献双安全）✅
- 低频源出文天数 < 7 → 样本不足跳过 ✅
- 新转正源（在岗 < 30 天）→ 宽限期跳过 ✅
- `fetched=0` 源 → 不判僵尸（交 health-check）✅
- 变质源（`pct_with_desc` 1.0→0.1）→ B 预警 ✅
- 天生短摘要源（基线本就低）→ 不误判 B ✅
- 邮件 HTML：`MIME-Version` 头、无重复 style、标题 escape

## 8. 未来演进

- 测试期（周度 + 每次发报告）验证判定准确性与输出质量
- 验证通过后：转**月度**（每月 2 号，错开 profile-refresh 的 1 号）或保持周度但"无异常静默"
- 阈值（窗口 30d / selected≤1 / 样本 7 天 / B 各信号）按观察迭代
- demote 始终人工确认（用户硬性要求），不规划全自动路径
