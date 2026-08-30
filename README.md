# Global News Digest

Automated global news digest system that fetches from 57 sources (54 RSS feeds + 2 Sina Finance APIs + 1 HN Firebase API; exact count drifts ±1–2 as trial sources rotate) and delivers HTML email reports once daily, with LLM-based article classification, periodic health monitoring and automatic failover.

## Architecture

```
Cron (1x daily: 12:15 BJT)
 └── global-news-cron-wrapper.sh
      └── unified-global-news-sender.py
           ├── news-sources-config.json (57 sources)
           ├── Sina Finance JSON API (2 sources)
           ├── HN Firebase API (1 source, structured data with scores)
           └── RSS/Atom feeds (54 sources, includes active trials)

Cron (1x daily: 12:05 BJT)
 └── rss-health-check.py
      ├── news-sources-config.json (reads + auto-edits on failover)
      └── logs/rss-health.json (consecutive failure state)
```

## Scripts

| Script | Description |
|--------|-------------|
| `unified-global-news-sender.py` | Core engine — parallel fetches from all sources via ThreadPoolExecutor, generates newspaper-style HTML email with per-article timestamps, sends via SMTP. Uses stdlib `html.escape()` with `quote=False` for title/text escaping (escapes `&<>` but leaves quotes and apostrophes as-is for email client compatibility) |
| `global-news-cron-wrapper.sh` | Cron wrapper — manages logging, config validation, environment setup, and error handling |
| `news-sources-config.json` | Central config for all news sources with per-source name, URL, type, keywords, article limit, and max age |
| `rss-health-check.py` | Health monitor — checks RSS sources in parallel, tracks consecutive failures, auto-swaps to fallback URLs after 3 failures, sends email alerts |

## RSS Health Monitor

`rss-health-check.py` runs every 6 hours and performs four checks per source:

1. **HTTP reachability** — GET with 10s timeout
2. **Parse validation** — valid XML (RSS/Atom) or JSON (Sina API)
3. **Article count** — at least 1 article present
4. **Freshness** — newest article within `max_age_hours` threshold

### Automatic failover

When a source fails **3 consecutive checks** and has a known fallback URL, the monitor:
- Edits `news-sources-config.json` directly (text-level URL swap, preserves formatting)
- Records the original URL in `logs/rss-health.json` for potential revert
- Sends an email alert

Sources with fallbacks (RSSHub mirrors):

| Source | Fallback |
|--------|----------|
| 虎嗅 | rsshub.rssforever.com/huxiu/article |
| IT之家 | rsshub.rssforever.com/ithome |
| 少数派 | rsshub.rssforever.com/sspai/matrix |
| 钛媒体 | rsshub.rssforever.com/tmtpost/recommend |
| 界面新闻 | rsshub.rssforever.com/jiemian/list/4 |
| Solidot | rsshub.rssforever.com/solidot |
| 南方周末 | rsshub.rssforever.com/infzm/2 |

Sources without fallbacks (BBC, NYT, Economist, Bloomberg, etc.) trigger alert-only — no auto-swap.

36氪 was removed from this table on 2026-08-06: upstream `36kr.com/feed` now serves a
火山引擎 WAF challenge page, and every 36kr route on the mirror answers 503. A fallback
pointing at a verified-dead URL is worse than no fallback, so the source was demoted instead.

### Usage

```bash
# Console report
python3 rss-health-check.py

# Console report + email alert (if issues found)
python3 rss-health-check.py --email
```

## News Sources (57)

> Authoritative list lives in `news-sources-config.json` (RSS) + `config/rss-registry.json` (categories/status). Trial-promoted feeds rotate, so this list may drift ±1–2 between README updates. Snapshot: 2026-08-17 (rebuilt by script from config + registry, not hand-counted; both agree at 54 with zero drift).

**RSS/Atom (54)**, grouped by registry category:

- **tech_ai** (12): Ars Technica, BBC Technology, IEEE Spectrum, MIT Technology Review, NYT Technology, Solidot, TechCrunch, The Verge, Wired, 虎嗅, 量子位, 钛媒体
- **global_finance** (10): BBC Business, Bloomberg, Bloomberg Econ, Bloomberg Politics, CNBC, Economist Business, Economist Finance, Economist Leaders, FT, NYT Business
- **vertical** (8): Carbon Brief, Economist Science, Foreign Policy, IPS News, Nautilus Magazine, ProPublica, Quanta Magazine, The Guardian Science
- **hk_sea** (5): CNA, Korea Herald, Philippine Daily Inquirer, Rappler, SCMP
- **europe** (6): Al Jazeera English, BBC World, France24 English, Politico Europe, RFI English, The Guardian World
- **china_depth** (5): BBC中文, RFI中文, 澎湃新闻, 端傳媒 Initium Media, 纽约时报中文
- **north_america** (3): Globe & Mail, Politico US Politics, The New Yorker
- **healthcare** (4): Endpoints News, KFF Health News, Science News, STAT News
- **global_south** (1): Daily Maverick

**API sources (3)**: 中国科技/AI, 中国财经要闻 (Sina Finance JSON APIs), Hacker News (Firebase API)

## English Title Translation

English news titles are batch-translated to Chinese using GPT-4.1-mini (approximately 70 titles per send). The translated Chinese title is displayed as the primary headline, with the original English title shown as an italic subtitle below it.

## LLM-Based Article Classification

Articles from mixed-content sources are classified into correct sections using GPT-4.1-mini. The `classify_articles()` method sends all article titles (except locked sources: Canada, Economist) in a single API call, receiving a numbered JSON dict mapping each article to one of five categories: `tech`, `finance`, `politics`, `china`, `asia`. Falls back to keyword-based reclassification if the API call fails.

### 文章级分区路由（2026-06-14, 方案 B）

所有 production 源的文章统一走上面的文章级 LLM 标签归入邮件板块（`_collect_region_articles` 遍历**全部** `news_data` 源，而非只手工 `REGION_GROUPS` 清单）；不再有"源不在手工清单就全堆其他区"的盲区。新源无 LLM 标签时兜底 `REGION_OTHER`（"其他 OTHER"，理想接近空）。healthcare/科学类文章归入「🔬 科学·健康 SCIENCE & HEALTH」专属板块（方案 C 第一步，2026-06-21；`docs/superpowers/specs/2026-06-21-science-health-section-design.md`）；vertical 调查/地缘类（ProPublica/Foreign Policy 等）仍暂散现有板块，待方案 C 第二步「深度·专题」。Spec: `docs/superpowers/specs/2026-06-14-category-driven-region-grouping-design.md`

## LLM Fallback Chain

Both translation and classification use a multi-provider fallback chain to ensure resilience:

```
GPT-4.1-mini → Gemini 2.5 Flash → Gemini 2.5 Flash-Lite → keyword fallback
```

Retry behavior:
- HTTP 429 (rate limit): 1 retry after 2 seconds, then move to next provider.
- HTTP 5xx (transient server error): retried with exponential backoff (5s, 10s) on OpenAI; Gemini calls fast-fail with `max_retries=2` (one retry then move on) since flash 503 is a known regional capacity issue and waiting longer rarely recovers.
- Socket read timeout: retried 3s later (up to `max_retries`); avoids triggering Gemini fallback for a single slow OpenAI request.

The email includes an LLM Status banner when fallback is active (orange for FALLBACK, red for FAILED), hidden when all calls succeed via the primary provider.

## Cross-Send Deduplication

Articles are tracked across daily sends via `logs/sent-today-YYYY-MM-DD.json`. Previously sent articles are filtered out to avoid repetition. Premium sources (Economist, FT, Bloomberg, NYT) can resurface after a 4-hour cooldown period.

## Article Timestamps

Each news item displays its publication time and relative age alongside the source:

- **HTML email**: `via BBC World · 03/01 14:30 (2h ago)`
- **Console output**: `via BBC World [03/01 14:30]`

Timestamps are shown in Beijing Time (BJT). Relative age displays as minutes, hours, or days (e.g., `3m ago`, `5h ago`, `2d ago`). Publication dates are parsed from RSS `pubDate`/`published`/`updated` fields and Sina API `ctime` unix timestamps.

## Time Slots

Each delivery is tagged by Beijing time:

| Slot | BJT | Label |
|------|-----|-------|
| Late night | 00:00 | 🌙 深夜档 |
| Morning | 08:00 | 🌅 早间档 |
| Afternoon | 16:00 | 🌆 午后档 |

## Requirements

- Python 3, curl
- No external packages — stdlib only (`urllib`, `xml.etree`, `smtplib`, `concurrent.futures`)
- SMTP credentials in `~/.stock-monitor.env`:
  ```
  SMTP_USER=your@email.com
  SMTP_PASS=your_app_password
  MAIL_TO=recipient@email.com
  NEWS_MAIL_TO=user1@email.com,user2@email.com   # optional, falls back to MAIL_TO
  NEWS_MAIL_BCC=bcc@email.com                    # optional, BCC recipients
  ```

Current recipients (4 TO + 1 BCC):
- `ch_w10@outlook.com`, `sunying1588@163.com`, `liuzhiwen@shenyuanele.com`, `cjl1656@qq.com`
- BCC: `tangwanshan@outlook.com`

## Cron Schedule

```cron
# News digest: 1x daily at 12:15 BJT (04:15 UTC, via cron-wrapper)
15 4 * * * ~/cron-wrapper.sh --name global-news-12 --timeout 720 --lock -- ~/.openclaw/workspace/global-news-cron-wrapper.sh email

# RSS health check: 1x daily at 12:05 BJT (04:05 UTC)
5 4 * * * ~/cron-wrapper.sh --name rss-health-12 --timeout 120 -- python3 ~/.openclaw/workspace/rss-health-check.py
```

## AutoResearch — Digest Quality Pipeline

An automated experimentation system (Phase B) that tunes news digest quality through fixture-based replay and scoring.

### Components

| Script | Description |
|--------|-------------|
| `digest_pipeline.py` | Dedup (Jaccard bigram similarity >0.5), keyword ranking, region-based quotas |
| `evaluate_digest.py` | Replays fixture snapshots, scores on 5 dimensions (coverage, relevance, freshness, diversity, dedup) |
| `digest-tuning.json` | Tuning parameters — weights, thresholds, quota allocations |
| `scripts/wrapper-autoresearch-news.sh` | Cron wrapper for automated experiments (daily 13:00 BJT) |
| `scripts/rss-source-discovery.sh` | Fully automated RSS discovery pipeline — LLM candidate generation, scoring, trial promotion (daily 04:15 BJT, 25min budget) |
| `autoresearch/program.md` | Experiment program and hypothesis tracking |
| `autoresearch/results.tsv` | Experiment results log |

### How it works

1. **Fixture capture**: `tests/YYYY-MM-DD.json` snapshots of raw fetched articles
2. **Pipeline replay**: `digest_pipeline.py` processes fixtures with current tuning params
3. **Quality scoring**: `evaluate_digest.py` measures 5 dimensions, produces composite score
4. **Current score**: 0.8728 (from baseline 0.8207, latest as of 2026-04-12; AR in CONTROLLED PAUSE — auto-skips until 10 fixtures)

## RSS Source Discovery

Daily LLM-driven pipeline that surfaces new high-quality RSS candidates and queues them for trial:

- **Cron**: daily 04:15 BJT via `scripts/rss-source-discovery.sh` (25min budget, lock-protected)
- **Pipeline**: discover (Claude Code search) → dedupe → validate (HTTP/parse/freshness) → score (6 dimensions) → save into registry → trigger trial-manager
- **Categories** (9 total, `config/rss-discovery-categories.json`):
  - `global_finance`, `tech_ai`, `china_depth`, `hk_sea` (incl. Japan/Korea/India queries),
  - `europe`, `north_america`, `healthcare`, `vertical`, `global_south` (Latin America + Africa + Middle East — added Apr 29)
- **Score dimensions**: reliability, freshness, content_quality, content_depth, authority, uniqueness → weighted final
- **Pool cap**: top 70 candidates kept (raised 50→70 on 2026-05-26 — pool was permanently saturated, masking demand for under-represented categories); lowest-scoring auto-rejected as `pool-cap` when exceeded
- **Excellent badge**: score ≥ 0.85 = will auto-promote on next trial-manager run

## RSS Trial Manager

Automated source promotion pipeline that turns high-scoring discovery candidates into active sources, then graduates the ones that prove their value in real digest emails:

- **Auto-promotion**: candidates with score ≥ `PROMOTE_THRESHOLD` (0.85, lowered from 0.90 on Apr 29 — trial system arbitrates edge cases)
- **Concurrency**: up to `MAX_CONCURRENT_TRIALS` = 2 active trials simultaneously (Apr 25 upgrade), max 1 promotion per day, category mutex (no two trials in the same category at once)
- **Trial period**: `TRIAL_DAYS` = 7 days (Jun 20, raised from 3 — mirrors GMIA 7-day model)
- **Graduation rule** (Jun 20, tightened to match 7-day window): source must pass **both** gates to auto-graduate:
  - **Volume**: `total selected ≥ AUTO_KEEP_MIN_SELECTED` (10, raised from 5; proportional: 5×7/3 rounded down)
  - **Distribution**: `days_with_content ≥ MIN_DAYS_WITH_CONTENT` (4, raised from 2; mirrors GMIA ≥4/7 days gate) — at least N distinct days must have produced ≥ 1 selected article
  - Either gate failing → auto-removed. Distribution gate prevents promoting bursty sources that pass volume on a single spike day
- **Tier lifecycle** (fixed 2026-08-30): a trial source is admitted **explicitly** into `source_tiers.commodity` (boost 0.6), graduates to `standard` (1.0) via `rss_registry.set_tier()`, and has its tier removed on rejection. Per spec `2026-04-11-rss-trial-manager-design.md` (`Trial tier | commodity (boost 0.6) | Low priority prevents displacing proven sources`), **running a trial at 0.6 is deliberate, not a bug** — every trial competes from the low-priority tier by design.
  - Two traps this closes. First, the entry step was never implemented (spec §Entry actions #2), so trial sources sat in **no** tier list and only reached 0.6 through `digest_pipeline._get_tier()`'s fallback. Same ranking, but the "every live source carries an explicit tier" invariant broke on every single trial — that is what `test_every_live_source_has_a_tier` was going red about; it was a real spec violation, not a mid-trial false positive. Second, graduation must use `set_tier`, **not** `assign_default_tier`: the latter no-ops on a source that already carries a tier, so a commodity-admitted trial would have stayed at 0.6 forever.
  - ⚠ The fallback for an untiered source is **commodity (0.6)**, not `standard` (1.0). Two docstrings claimed 1.0 until 2026-08-30; both are corrected and a characterization test now pins the semantics.
- **Backfill** (Apr 29): each `cmd_run` re-aggregates `[start_date, today]` from `logs/trial-source-log.jsonl` so any missed day (including the trial-creation day) is reconstructed idempotently
- **Script**: `rss-trial-manager.py` (subcommands: `run` / `status` / `keep [name]` / `remove [name]` / `retry name`)
- **State**: `config/rss-registry.json` (unified — replaced the old `trial-state.json` + `discovered-rss.json`)
- **Integration**: called automatically at the end of `scripts/rss-source-discovery.sh`

## Production Source Fitness (Phase 0 / 0.5)

Per-send telemetry that captures whether each production source is still pulling its weight — the long-term input to a future S&P-500-style rebalancing of the source list:

- **Phase 0** (2026-05-26): every send writes `(ts, source, fetched, selected)` to `logs/production-source-log.jsonl` for every registry production source
- **Phase 0.5** (2026-05-26): RSS sources additionally write 4 per-article quality signals — `avg_title_len`, `avg_desc_len`, `pct_with_desc`, `pct_with_author`
- **Coverage** (2026-05-27): registry production = 51 sources (18 AI-discovered + 33 legacy backfilled). Earlier coverage was 18/52 RSS — Bloomberg / FT / CNBC / BBC / Economist / SCMP and other pre-2026-04-21 sources had no registry entry until `scripts/backfill_legacy_to_registry.py` reconciled the two configs
- **Lifecycle tools**: `rss-promote-candidate.py` (discovered → production), `rss-demote-source.py` (production → rejected, syncs both `news-sources-config.json` and `rss-registry.json` to prevent drift), `scripts/backfill_legacy_to_registry.py` (one-time legacy reconciliation, idempotent)
- **Candidate dedup — 4 passes** (`dedup_candidates`): ①URL normalize ②existing/prior URL ③publisher name within the batch ④**same-feed content check** (added 2026-07-26): when a candidate shares a domain with an existing source, both feeds' article links are compared and ≥50% overlap means it's the same feed under a different path. Passes ①-③ compare surface strings only, which is how 端傳媒 `/rss/` and 端传媒 `/feed/` — different URL, different name, identical articles — both graduated into production. Existing feed is fetched once per domain and cached; unreachable → fail open. Verified against all 19 same-domain pairs in the live pool (Economist ×4, BBC ×3, Bloomberg ×3, SCMP, Guardian, NYT, RFI, anyfeeder ×3): max overlap 25% (SCMP), so real section feeds keep a 2× margin under the threshold.
- **Phase 0 scope**: data collection only; no automated action.
- **Phase 1** (2026-06-13, `rss-production-review.py`): a weekly evaluator now consumes this telemetry — see below. Demote stays human-confirmed.

### Production source review (`rss-production-review.py`)

Weekly in-production quality review (test-period cadence) that reads `logs/production-source-log.jsonl` + the registry and emails a report. It NEVER demotes anything itself.

- **A — zombie sources** (auto-flagged, suggests demote): production sources still publishing (`fetched>0`) but ~never selected (`selected≤1` over a 30-day window), gated by a 30-day on-tenure grace period and an `active_days≥7` sample floor so low-frequency sources aren't misjudged. `fetched==0` (source not publishing) is left to `rss-health-check`. Each candidate carries a ready-to-paste `rss-demote-source.py` command.
- **B — content degradation** (warning only): `pct_with_desc` / `avg_desc_len` / `pct_with_author` drifting down vs the source's OWN baseline (60-day cap, recent-7d vs prior) — never absolute thresholds, so natively-short-summary sources (Foreign Policy etc.) aren't penalised.
- **♻️ Rotation — 组内实测优胜劣汰** (2026-06-15, suggests demote): within each `category`, the lowest-**selection-rate** source that's *also below half the group's median rate* **and also declining against its own prior window** is flagged for rotation — 沉淀精品同时保多元. legacy(无 category)豁免；每类保底 3 个；沿用 A 的低频/在岗宽限保护；与 A 绝对僵尸不重复. Spec: `docs/superpowers/specs/2026-06-15-source-fitness-rebalance-design.md`
  - **A third gate: the source must also be declining against ITSELF** (added 2026-08-30): flagged only if its rate is at least `ROTATION_MIN_SELF_DECLINE` (5pp) below its own rate in the **immediately preceding** window `[now-2w, now-w)`. Group-relative alone cannot tell "this source got worse" from "the whole pool's selection policy changed": on 2026-06-15 the funnel started actually filtering (before it, **26 sources sat at exactly 100% selection**) and every source halved in the same week — KFF 100%→5.9%, CNA 69%→27.5%, The Guardian World 44%→11.8% — while their B-section metadata stayed flat for four months and CNA's `pct_with_desc` actually *improved* 0.17→0.67. The baseline is deliberately the adjacent window, not all history, since all-history would fold that pre-change 100% back in and make everything look like a collapse. **A baseline under 10 fetched articles means no flag** — demote is irreversible, so "cannot compute a baseline" must not read as "baseline is fine". Pass `min_self_decline=None` to test the group gate in isolation.
  - **Two ratio gates** (added 2026-08-17): below half the group median **and** a further `ROTATION_MIN_MARGIN` (3pp) below that threshold. The ratio test alone is a cliff at the median — the 2026-08-16 report flagged RFI English at 30.39% while same-group The Guardian World sat safe at 31.37% against a 30.88% threshold. Half a article's difference decided permanent removal vs nothing.
  - **🛡️ Sole-region exemption** (added 2026-08-17): a group laggard that is the **only** live source feeding one of `evaluate_digest.SOURCE_TO_REGION`'s digest sections is not flagged — demoting it would leave that section with no source-level supply at all. This closes a ratchet: every demote raises the group median, which mechanically manufactures the next laggard. Demoting Dawn Pakistan (hk_sea) pushed the group median 60.7% → 68% and turned CNA from "shielded by the one-per-group cap" into a −7.11pp headline candidate — and ASIA-PACIFIC has only CNA. The exemption is **never silent**: those sources get their own report section (with the region named and no demote command), so "we owe this section a second source" stays visible instead of disappearing.
  - **口径 = 入选率 `selected/fetched`, not the absolute count** (fixed 2026-07-26): `fetched` is set by each source's `limit` quota in `news-sources-config.json` (limit=3 → ~99 articles per 30d, limit=6 → ~198), so absolute selection counts are not comparable across quotas and small-quota sources were being flagged systematically. IEEE Spectrum was flagged at 43 selected vs a group median of 96 while actually converting 44% — level with limit=6 peers at 45%.
  - **The ratchet is now printed, not just guarded against** (added 2026-08-30): each rotation row carries **who becomes the next laggard if you run this command**, computed by re-running the same criteria with that source excluded. On 2026-08-30 demoting The Guardian World would have promoted RFI English (27.1%) to laggard by a 0.6pp margin; the self-decline gate above now spares it, and the column reads `无`. The report also states plainly that **demote is irreversible** — `rejected` is terminal and `rotation-group-laggard`/`zombie` sit in `QUALITY_REJECT_MARKERS`, so the revival probe skips them by design.
  - **Config context, annotated but never judged** (added 2026-08-30): selection rate is driven by three config knobs — `tier_boost` (0.1–1.5), region quota caps, and board membership. Rotation and exemption rows now show the source's tier/boost and its board's quota, and a notice appears whenever the list contains a down-weighted source. CNA is the live case: the only `commodity` (0.6×) source in hk_sea against four `standard` (1.0×) peers. **No "board saturation %" is reported**: computing one requires assuming a source's default board is where its articles actually land, and under that assumption MACRO/AI measure 139%/141% — Stage 3 geo and Stage 4 LLM routing move articles elsewhere, so the premise is false. Config facts only; the judgement stays with the human.
- **Action model**: report only — demote is human-confirmed via `rss-demote-source.py`. Test period emails every week (incl. a full-pool contribution snapshot); cadence and thresholds to be tuned after observation.
- **Spec**: `docs/superpowers/specs/2026-06-13-rss-production-quality-review-design.md`
- **Revival probe** — sources demoted for *technical* reasons (WAF block, persistent 403/timeout, dead mirror route) are re-probed each week. A source counts as revived only when it parses to ≥1 article — an HTTP 200 is not enough, since WAF challenge pages return 200 with `text/html`. Quality-based demotions (zombie, rotation laggard, duplicate, pool-cap) are never probed: they were removed for what they published, not for being unreachable. The weekly report gains a section only when something actually revived. The probe also reports the **final URL** after following redirects — this matters because a registry URL like `endpts.com/feed/` might be a 301 permanent redirect to `endpoints.news/feed/`, and restoring from the report requires the live URL, not the stale registry entry. Restoring a source to the pool goes through `rss-trial-manager.py retry "<name>"` (added 2026-08-17), which resets it to `discovered` so it re-enters the trial queue — technically-demoted production sources are eligible alongside `auto-removed` trials, while quality rejections still refuse. Before that path existed the report could only tell you to hand-edit registry JSON.
  - **Live-pool exclusion**: candidates whose name *or* normalised URL still appears in `news-sources-config.json` are skipped. The registry allows same-name entries (30 name collisions as of 2026-08-17; Endpoints News alone had 4), so probing every `status=rejected` row reported a source that had never left the pool — and acting on it would have slipped a duplicate feed past `rss-promote-candidate.py`'s URL-based idempotency check.

## Scoring v2

Rebalanced weights (Apr 2026): reliability 0.25→0.10, content_quality 0.20→0.25, authority 0.20→0.30. New `content_depth` sub-dimension (avg description length post-HTML-strip) penalizes paywall summaries. Low-frequency correction: sources with ≤10 articles/check use gentle freshness decay (weekly journals not penalized).

### Tests

```bash
python3 -m pytest tests/ -q   # 414 tests (pipeline + trial manager + discovery + sender + rss_registry + demote + backfill + production-review + region-routing + science-health + revival probe + region-rules liveness + contract defenses + rotation ratchet visibility + trial tier lifecycle + self-baseline rotation gate + config-context annotation)
./scripts/check-deleted-state-refs.sh            # pre-commit check: no refs to deleted state files
./scripts/check-shell-prompt-assignments.sh      # pre-commit check: multi-line shell VAR="..." must have : "${VAR:?...}" guard
```

**Production-config write guard** (`tests/conftest.py`, added 2026-08-30): an autouse fixture byte-compares `digest-tuning.json`, `news-sources-config.json` and `config/rss-registry.json` around every test; any test that mutates one fails and the file is restored on the spot. It exists because a real incident: extending `remove_trial_from_config()` to clear tiers made two pre-existing tests — which patched `SOURCES_FILE` but not `_reg.TUNING_FILE` — delete ProPublica from the **live** `digest-tuning.json`, with the suite still fully green. "This test isolates the file it knew about when it was written" expires the moment the code under test touches one more file, so the guard compares files instead of trusting every test to remember a patch.

## Development

This repo includes a `.claude/CLAUDE.md` with repo-specific context for [Claude Code](https://claude.ai/claude-code) — stdlib-only constraint, config source of truth, RSS failover mechanics, and pubDate parsing quirks. Claude Code agents automatically load this context when working in the repo.

## License

MIT
