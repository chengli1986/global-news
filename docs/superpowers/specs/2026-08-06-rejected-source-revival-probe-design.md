# 已下线源复活探测 (Rejected Source Revival Probe)

**日期**: 2026-08-06
**状态**: 设计已批准，待实现

## 背景

2026-08-06 下线 36氪（commit `d434cf4`）：上游 `36kr.com/feed` 被火山引擎 WAF 拦截（返回 HTTP 200 + `content-type: text/html` 的 JS 挑战页），镜像 `rsshub.rssforever.com/36kr/*` 全部路由 503。两条路都堵死，故 demote 到 `rejected`。

这类下线是**技术性的、可逆的**——上游解除拦截或 RSSHub 修好路由，源随时可能恢复可用。但现有机制不会告诉我们：

- **discovery 捡不回来**：`load_existing_sources()` 只含 `production`/`trialing`，rejected 不在其中；36氪 是 `discovered_via: "legacy"` 源，从未走过候选流程，也不在 prior candidates 里。理论上能被 AI search 重新提名，实际上不可靠。
- **health-check 看不见**：源已从 `news-sources-config.json` 移除，健康检查只遍历 config 里的源。
- **demote 是终态**：`rss-promote-candidate.py:53` 只接受 `status == "discovered"`，无法直接 promote 回来。

结果是：源恢复了也没人知道，除非人工想起来去试。

## 目标

每周自动探测**技术性下线**的源是否恢复，恢复了在已有周报里提醒。人工决定是否恢复入池。

**非目标**：自动恢复入池。恢复要动 registry status，且当初下线的口径判断（如 36氪 只剩 AI 垂类路由、与「中国科技/AI」重叠）需要人重新拍板。

## 设计

### 挂载点

搭车 `rss-production-review.py`——周日 09:30 BJT 已有 cron（`30 1 * * 0` UTC）、已有 SMTP 发信、已有周报 HTML 骨架。不新增 cron entry，不新写发信代码。

与已有的「方案 C 待办提醒条」（`plan_c_reminder_html`）是同一种搭车模式。

### 探测范围（排除法）

两个条件同时满足才探测：

1. `status == "rejected"` **且**条目带 `production` 字段——即曾经真正进过生产。
   这把 registry 的 153 条 rejected 收窄到 13 条。被排除的 123 条 `pool-cap` 是候选池容量上限自动淘汰的 discovered 候选，**从未进过生产**，不属于「下线的源」。
2. `reject_reason` **不含**以下质量类关键词（大小写不敏感）：
   `pool-cap`、`rotation-group-laggard`、`zombie`、`duplicate`、`auto-removed`。
   `reject_reason` 为空的条目也排除（无从判断下线原因，宁可不探）。

**为何用排除法而非白名单**：将来出现新的技术性下线原因（如 `dns-fail`）会自动纳入探测；反过来用白名单（`waf|403|timeout|…`）则会静默漏掉。宁可多探两条无害的，不可漏。

**当前命中 3 条**（2026-08-06 实测）：

| 源 | reject_reason | URL |
|---|---|---|
| 36氪 | `waf-block-upstream-and-fallback-route-503` | `https://36kr.com/feed` |
| Endpoints News | `persistent-403-removed-from-sources-2026-05-25` | `https://endpts.com/feed/` |
| Nikkei Asia via rsshub | `persistent-timeout-removed-from-sources-2026-05-21` | `https://rsshub.rssforever.com/nikkei/asia` |

每周 3–6 次 HTTP 请求，可忽略。

### 探测哪些 URL

对每个源探测**至多两个** URL：

1. registry 条目里记的 `url`（原址）
2. 该源名在 `rss-health-check.FALLBACK_URLS` 里的镜像 URL（若存在）

36氪 已于 `d434cf4` 从 `FALLBACK_URLS` 移除，故当前只探原址。这是有意的——那条镜像已验证全线 503，探它是浪费。若将来重新登记镜像，探测自动跟上。

### 「恢复」的判据

**必须能解析出 ≥1 个 item/entry，而不是 HTTP 200。**

这是 36氪 事故的直接教训：WAF 挑战页返回 200 + `text/html`，纯状态码检查会误判为「恢复了」。判据落到「真的解析出了文章」这一层，才对得上「这个源能用」的实际含义。

复用 `rss-health-check.py` 的 `check_source()`，不另写一套 XML 解析。

`rss-health-check.py` 文件名带连字符，不能直接 `import`——用 `importlib.util.spec_from_file_location` 加载，仓内已有先例（`scripts/benchmark_classifier_providers.py:27`、`scripts/dry_run_classifier.py:39` 都这样加载 sender）。

`check_source(name, url, source_type, max_age_hours)` 是纯函数（不读 config/state），返回 `{"ok", "error", "article_count", "newest_age_hours"}`。用法两点：

- **判据取 `article_count >= 1`，不取 `ok`**。因为 `ok` 还包含 staleness 判定，而一个刚恢复的源文章可能很旧——那不该算「没恢复」。
- `max_age_hours` 传一个极大值（`24 * 365`）绕过 staleness 分支，让它只做「抓得到 + 解析得出文章」这一件事。

WAF 挑战页在这条路径上会落到 `XML parse error` → `article_count=0` → 判为未恢复，正是想要的行为。

### 失败处理：fail closed

探测中任何异常（超时、DNS、解析崩溃）一律记为**「仍未恢复」**，不向上抛。

理由：周报是每周唯一一次的例行件，不能因为一个附加功能的网络抖动而整封发不出去。这与 `rss-production-review` 现有的稳健性取向一致。

代价是网络抖动那周会漏报一次恢复——下周会再探，可接受。

### 输出

周报 HTML 新增一节「已下线源复活检测」，**仅在有源恢复时出现**（无发现则完全不占版面，避免每周一节「无事发生」淡化信噪比）。

每条列出：源名、下线原因、下线日期、恢复的是哪个 URL、抓到几篇文章、最新文章时间。

附一句操作提示：恢复入池需手工把 registry `status` 从 `rejected` 改回，`rss-promote-candidate.py` 不收 rejected 状态。

## 测试

与现有 `find_zombies`/`find_degraded` 同款：纯函数 + 注入 fetcher，**不打真实网络**。

必须覆盖：

- WAF 挑战页（HTTP 200 但 0 item）→ **不算恢复**（36氪 回归测试）
- HTTP 503 → 不算恢复
- 真实 feed（≥1 item）→ 算恢复
- 质量类下线的源（`rotation-group-laggard`/`zombie`/`duplicate`/`pool-cap`）→ 不被探测
- 无 `production` 字段的 rejected 候选 → 不被探测
- `reject_reason` 为空 → 不被探测
- fetcher 抛异常 → 记为未恢复，不向上抛
- 无源恢复时 → 周报不出现该节

## 影响面

- `rss-production-review.py`：新增探测函数 + 报告节，接入 `cmd_run`
- `tests/test_rss_production_review.py`：新增测试
- 无 config 变更、无 cron 变更、无新依赖（沿用 stdlib）
