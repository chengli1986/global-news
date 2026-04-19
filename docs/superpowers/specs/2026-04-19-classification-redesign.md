# News Classification Redesign — Two-Axis Funnel

**Date**: 2026-04-19
**Status**: Design — Awaiting plan/implementation
**Author**: Claude Opus 4.7 + user discussion
**Related**: `unified-global-news-sender.py` (lines 493-697 region/classify), `digest-tuning.json`, `digest_pipeline.py`

---

## §1 Mission Statement

Redesign the news article → email region classification from a **single-stage 5-category LLM call** to a **4-stage funnel with 2-axis (topic × geo) labels**, so that:

1. ~52% reclassification chaos (current state) drops to <15%
2. Chinese-source content (界面/虎嗅/钛媒体/中国财经要闻 etc.) stays in CHINA region by default instead of scattering to GLOBAL FINANCE
3. Foreign-source articles about Canada/Asia-Pac get routed to those geo regions instead of being topic-classified
4. LLM call volume reduced from ~200 articles/send to ~60-100/send
5. Each routing decision carries a `reason_code` for debugging and auditability
6. New regions (TECH split, FINANCE split, SOCIETY) provide finer information density

This is **NOT** a re-architecture of the digest pipeline (dedup + rank + quota stays). Only the **classification + region assignment** layer changes.

---

## §2 Problem Statement (Current State)

**Architecture as of 2026-04-19** (per `unified-global-news-sender.py`):

```
~234 articles → classify_articles() → 5 LLM categories (tech/finance/politics/china/asia)
                       ↓
                _CATEGORY_TO_REGION mapping → 5 of 7 email regions
                (CANADA + ECONOMIST fed only by source lock)
                       ↓
                _LOCKED_SOURCES bypass LLM (CBC, Globe & Mail, Economist × 4)
```

**Issues observed in 2026-04-19 emails** (logged at `~/.openclaw/workspace/logs/news-sender-20260419.log`):
- Reclassification rate: 52% (105 of 202 articles moved)
- GLOBAL FINANCE region: 14 of 32 articles (44%) from Chinese sources (中国财经要闻 × 7, 界面 × 3, 虎嗅 × 2, 36氪 + 钛媒体)
- CHINA region contains: NYT Business × 4, BBC World × 2, TechCrunch × 1 — defensible but inconsistent with "domestic society/culture" definition
- CANADA-topic articles from FT/SCMP/Guardian → routed to FINANCE/POLITICS, never reach CANADA
- LLM `china` label definition explicitly excludes tech/finance/politics → forces Chinese business news away from CHINA

**Root cause**: LLM categories conflate **topic** (tech/finance/politics) with **geography** (china/asia), forcing artificial mutual exclusion. CANADA has no LLM category at all.

---

## §3 Decisions Log

| # | Decision | Rationale |
|---|----------|-----------|
| **Q1** | **Topic exemption**: Chinese `society/business` → CHINA; Chinese `tech/politics` → topic regions | Geo dominates for personal-context regions, topic dominates for global-comparison regions |
| **Q2** | **Structural split**: TECH → AI/前沿 + 消费科技; FINANCE → 市场/宏观 + 公司/产业 (7 → 9 zones) | Single TECH/FINANCE bucket too noisy; user prefers structural separation over inline chips |
| **Q3** | TECH/FINANCE sub-topic boundaries (AI products consumer-side → AI/前沿; crypto → 市场/宏观; OpenAI funding → AI/前沿; Tesla earnings → 公司/产业) | Specific per-article rules (see §4.4) |
| **Q4** | **New SOCIETY zone** for `society + non-China geo` | Separate low-priority lifestyle/culture content from high-density political/tech regions |
| **Q5** | ASIA-PACIFIC includes India + Australia/NZ + Taiwan + Mongolia; Russia/Central Asia routes by topic | Geographic broadening; Taiwan placed neutral to avoid editorial stance |
| **F1** | `max_total_articles = 150` (up from 120) | 10 zones × ~15 articles avg gives breathing room |
| **F2** | No tier_boost change; control consumer-tech volume via region quota max | Lowest-risk path; revisit if 消费科技 zone overflows |
| **F3** | Email region display order: AI/前沿 → 市场/宏观 → POLITICS → CHINA → 公司/产业 → 消费科技 → ASIA-PAC → CANADA → ECONOMIST → SOCIETY | High-density first, geo-niche last |

---

## §4 New Architecture

### §4.1 Funnel — 4 Stages

```
┌──────────────────────────────────────────────────────────────┐
│ Stage 0: Raw pool  (~234 articles per fetch)                 │
└────────────────────┬─────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────────┐
│ Stage 1: Source Hard Lock   (~15-20% pass-through)           │
│   CBC Business / Globe & Mail              → CANADA          │
│   Economist Leaders/Finance/Business/Sci   → ECONOMIST       │
│   reason_code = "source_lock:hard:<region>"                  │
└────────────────────┬─────────────────────────────────────────┘
                     ↓ remaining ~80%
┌──────────────────────────────────────────────────────────────┐
│ Stage 2: Source Soft Lock + Escape Rule   (~30-40% pass)     │
│   Default region by source-class:                            │
│     界面 / 南方周末 / 中国财经要闻 / 中国科技/AI /            │
│     36氪 / 虎嗅 / 钛媒体 / IT之家 / 少数派     → CHINA-bias  │
│     SCMP HK / RTHK / HKFP / Straits Times /                  │
│     日经中文                                    → ASIA-bias   │
│   ESCAPE if title hits strong external geo keyword            │
│     (Trump|Putin|Ukraine|Washington|Brussels|...)            │
│     AND no own-geo keyword (中国|香港|...)                    │
│     → fall through to Stage 3                                │
│   reason_code = "source_lock:soft:<region>" or "soft_escape" │
└────────────────────┬─────────────────────────────────────────┘
                     ↓ remaining ~40-50%
┌──────────────────────────────────────────────────────────────┐
│ Stage 3: Geo Keyword Funnel   (~10-15% pass-through)         │
│   Title hits CANADA keyword (加拿大|Trudeau|Ottawa|Toronto|  │
│     Vancouver|Montreal|Quebec|Alberta|Carney)                │
│     → CANADA                                                 │
│   Title hits ASIA-PAC keyword (Hong Kong|Singapore|Japan|    │
│     Korea|印度|India|Australia|Taiwan|台湾|Mongolia|...)     │
│     → ASIA-PAC                                               │
│   reason_code = "geo_keyword:<region>"                       │
└────────────────────┬─────────────────────────────────────────┘
                     ↓ remaining ~30-40% (~60-100 articles/fetch)
┌──────────────────────────────────────────────────────────────┐
│ Stage 4: LLM Classification — 2-axis (topic, geo, subtopic)  │
│   Output: {topic, geo, subtopic_optional}                    │
│   Routing: §4.5 matrix                                       │
│   reason_code = "llm:<topic>:<geo>"                          │
└──────────────────────────────────────────────────────────────┘
```

### §4.2 Topic Taxonomy (5 labels)

| Label | Definition | Examples |
|-------|-----------|----------|
| `politics` | Government, military, diplomacy, war, elections, protests, civic, policy | Iran-Israel war, US elections, Brussels summit, Hong Kong protest |
| `business` | Companies, markets, economy, trade, finance | Fed rate, CATL earnings, IMF GDP forecast, M&A |
| `tech` | AI, software, hardware, semiconductors, science breakthroughs (NOT consumer reviews) | Claude 5 release, OpenAI research, NVIDIA chip launch, RNA discovery |
| `consumer_tech` | Gadget reviews, product launches, app updates, smart home | iPhone 17 review, AirPods spec, M3 chip benchmark |
| `society` | Culture, education, lifestyle, social issues, health | College admissions reform, food culture, public health |

### §4.3 Geo Taxonomy (6 labels)

| Label | Definition |
|-------|-----------|
| `china` | Primarily about mainland China (companies, government, society) |
| `canada` | Primarily about Canada (federal/provincial, companies, society) |
| `asia_other` | HK / SG / JP / KR / SEA / India / Australia / NZ / Taiwan / Mongolia |
| `us` | Primarily about USA |
| `europe` | Primarily about EU / UK |
| `global` | Multi-country, no specific geo, or N/A |

**Disambiguation rule**: LLM emits the **dominant** geo, not a list. For ambiguous cases (e.g., "China-Russia summit"), LLM picks the article's primary focus.

### §4.4 Sub-Topic Taxonomy (4 labels, optional)

Used to route within already-decided region (TECH/FINANCE split):

| Topic | Subtopic | Routes to | Examples |
|-------|----------|-----------|----------|
| `tech` | `tech_ai` | 🧠 AI/前沿 | AI research, Claude/ChatGPT updates (platform side), semiconductors |
| `tech` | `tech_consumer` | 📱 消费科技 | iPhone review, Vision Pro evaluation, AirPods specs |
| `business` | `business_macro` | 📈 市场/宏观 | Fed, GDP, inflation, indices, crypto market, currencies, trade war (macro) |
| `business` | `business_corp` | 🏢 公司/产业 | Earnings, M&A, IPO, individual company news, sector-level deals |

**Boundary edge cases** (decided in Q3):
- "AI consumer side" (e.g., ChatGPT new feature) → `tech_ai` (platform-level, not consumer-product review)
- Crypto (BTC/ETH price, ETF) → `business_macro` (no separate `crypto` label)
- OpenAI $100B funding → `tech_ai` (not `business_corp`; the company IS AI infrastructure)
- Tesla quarterly earnings → `business_corp` (individual company)
- Fed rate decision moving stocks → `business_macro` (macro event drives reaction)
- Apple Vision Pro review → `tech_consumer`
- Apple WWDC AI strategy → `tech_ai` (platform/strategy, not specific product)

### §4.5 Routing Matrix

Mutually-exclusive ordered checks; **first match wins, function returns immediately**:

```python
def route(source, title, topic, geo, subtopic) -> tuple[str, str]:
    """Returns (region, reason_code). Each article matches exactly one branch."""

    # Stage 1 — Hard source lock (deterministic, no LLM signal needed)
    if source in HARD_LOCK:
        return HARD_LOCK[source], f"source_lock:hard:{source}"

    # Stage 2 — Soft source lock with escape rule
    if source in SOFT_LOCK:
        if not has_escape_signal(title):  # external geo keyword AND no own-geo keyword
            return SOFT_LOCK[source], f"source_lock:soft:{source}"
        # else: fall through to LLM (escape)

    # Stage 3 — Geo keyword funnel (deterministic, before LLM)
    if has_strong_canada_keyword(title):
        return CANADA, "geo_keyword:canada"
    if has_strong_asia_pac_keyword(title):
        return ASIA_PAC, "geo_keyword:asia_pac"

    # Stage 4 — LLM-driven (topic, geo, subtopic) routing
    # 4a. Geo-priority for personal-context regions (Q1 B applied)
    if geo == "canada":
        return CANADA, "llm:geo:canada"
    if geo == "asia_other":
        return ASIA_PAC, "llm:geo:asia_other"
    if geo == "china":
        if topic in ("society", "business", "consumer_tech"):
            return CHINA, f"llm:china+{topic}"
        # china + tech (subtopic tech_ai) or politics → falls to 4b for global comparison

    # 4b. Topic-priority for global-comparison regions
    if topic == "tech":
        if subtopic == "tech_consumer":
            return CONSUMER_TECH, "llm:topic:tech_consumer"
        return AI_FRONTIER, "llm:topic:tech_ai"
    if topic == "consumer_tech":  # safety net if topic emitted directly (rare)
        return CONSUMER_TECH, "llm:topic:consumer_tech"
    if topic == "business":
        if subtopic == "business_corp":
            return CORP_INDUSTRY, "llm:topic:business_corp"
        return MACRO_MARKETS, "llm:topic:business_macro"
    if topic == "politics":
        return POLITICS, "llm:topic:politics"
    if topic == "society":
        # canada/china/asia_other already handled in 4a; this is us/europe/global only
        return SOCIETY, "llm:topic:society"

    # Fallback — LLM emitted unrecognized topic, or all upstream stages failed
    return source_default_region(source), "fallback:source_default"
```

**Why this form**: original spec had `IF` blocks that visually looked like fall-through but routing must be one-region-per-article. Pseudocode-as-function makes the early-return semantics explicit and audit-friendly.

### §4.6 Region Structure (10 zones, in display order per F3)

| # | Region | Emoji | Sources (default) | Quota proposal |
|---|--------|-------|-------------------|----------------|
| 1 | AI / 前沿 | 🧠 | TechCrunch, Hacker News, Ars Technica, BBC Tech, NYT Tech, Solidot, Verge, 中国科技/AI, 36氪, 钛媒体, IT之家, 少数派, 虎嗅 | min 12, max 20 |
| 2 | 市场 / 宏观 | 📈 | Bloomberg Econ, FT (macro), CNBC (macro), Bloomberg (markets) | min 12, max 20 |
| 3 | POLITICS | 🏛 | BBC World, NYT 中文, BBC 中文, Bloomberg Politics, Guardian World, SCMP (politics) | min 14, max 22 |
| 4 | CHINA | 🇨🇳 | 界面新闻, 南方周末, 中国财经要闻, + foreign sources reporting on China | min 14, max 22 |
| 5 | 公司 / 产业 | 🏢 | FT (corporate), Bloomberg (corporate), CNBC (corporate), NYT Business | min 10, max 16 |
| 6 | 消费科技 | 📱 | Verge, Ars (some), IT之家 (some), 少数派 (some), TechCrunch (consumer) | min 6, max 10 |
| 7 | ASIA-PAC | 🌏 | 日经中文, CNA, SCMP HK, RTHK, HKFP, Straits Times | min 8, max 14 |
| 8 | CANADA | 🇨🇦 | CBC Business, Globe & Mail (LOCKED) | min 6, max 12 |
| 9 | ECONOMIST | 📕 | Economist Leaders/Finance/Business/Science (LOCKED) | min 4, max 10 |
| 10 | SOCIETY | 🌐 | Guardian (society), BBC (society), occasional from any source | min 3, max 8 |

**Sum check**:
- min sum = 89 (well under max_total=150, allows region balance failure to gracefully degrade)
- max sum = 154 (≥ 150, max_total=150 acts as binding cap)

**Important**: Sources can appear in multiple region candidates (e.g., FT can land in 市场/宏观, 公司/产业, or POLITICS depending on article). Stage 4 LLM determines the actual region per article.

---

## §5 LLM Prompt Redesign

**Input**: Numbered list of titles (Stage 4 only, ~60-100 articles)

**Output**: JSON object `{"<idx>": {"topic": "...", "geo": "...", "subtopic": "..."}, ...}`

**Prompt template** (English, mixed CN/EN titles):

```
Classify each numbered news title with three labels: topic, geo, subtopic.
Titles may be in Chinese or English.

Topics (pick exactly one):
- "politics": government, military, diplomacy, war, elections, protests, civic, policy
- "business": companies, markets, economy, trade, finance
- "tech": AI, software, hardware, semiconductors, science breakthroughs (NOT consumer product reviews)
- "consumer_tech": gadget reviews, product launches/specs, app updates, smart home, lifestyle apps
- "society": culture, education, lifestyle, social issues, health

Geos (pick exactly one — the article's PRIMARY geographic focus):
- "china": primarily about mainland China
- "canada": primarily about Canada
- "asia_other": HK/SG/JP/KR/SEA/India/Australia/NZ/Taiwan/Mongolia
- "us": primarily about USA
- "europe": primarily about EU/UK
- "global": multi-country, no specific geo, or N/A

Subtopic (only required for topic in {tech, business}):
- For tech: "tech_ai" (research/platforms/infrastructure/AI products at platform level) OR "tech_consumer" (reviews/specs/gadgets)
- For business: "business_macro" (Fed, GDP, indices, crypto market, trade war, currencies) OR "business_corp" (earnings, M&A, individual company)
- For other topics: omit or set to null

Return JSON: {"1": {"topic":"...","geo":"...","subtopic":"..."}, "2": {...}, ...}

Examples (low-ambiguity):
- "Fed signals April hold" → {topic:"business", geo:"us", subtopic:"business_macro"}
- "Apple Q4 earnings beat" → {topic:"business", geo:"us", subtopic:"business_corp"}
- "宁德时代为什么赚这么多钱" → {topic:"business", geo:"china", subtopic:"business_corp"}
- "iPhone 17 review" → {topic:"consumer_tech", geo:"us"}
- "Anthropic releases Claude 5" → {topic:"tech", geo:"us", subtopic:"tech_ai"}
- "OpenAI raises $100B" → {topic:"tech", geo:"us", subtopic:"tech_ai"}
- "Iran-Israel ceasefire" → {topic:"politics", geo:"global"}
- "卢特尼克对加拿大说糟透了" → {topic:"politics", geo:"canada"}
- "Guardian: 美国得州禁书法案" → {topic:"society", geo:"us"}

Examples (high-ambiguity, explicitly resolved per routing matrix):
# china + tech_ai → topic region (NOT CHINA, per Q1 B exemption)
- "DeepSeek 训练成本下降 80%" → {topic:"tech", geo:"china", subtopic:"tech_ai"}
- "字节跳动 Sora 对标 OpenAI" → {topic:"tech", geo:"china", subtopic:"tech_ai"}

# china + consumer_tech → CHINA (Chinese consumer products belong with China)
- "小米 14 Ultra 评测" → {topic:"consumer_tech", geo:"china"}
- "华为 Mate60 销量超预期" → {topic:"consumer_tech", geo:"china"}

# china + politics → POLITICS (Q1 B: politics topic wins over geo for global comparison)
- "中国发改委发布产能控制新规" → {topic:"politics", geo:"china"}
- "习近平赴莫斯科出席峰会" → {topic:"politics", geo:"china"}

# asia_other + society → ASIA-PAC (geo dominates regardless of topic)
- "日本少子化政策深度报告" → {topic:"society", geo:"asia_other"}
- "韩国年轻人婚育率创新低" → {topic:"society", geo:"asia_other"}

# europe + business — distinguish macro vs corp
- "ECB 维持利率不变" → {topic:"business", geo:"europe", subtopic:"business_macro"}
- "BMW Q3 销量同比下滑 12%" → {topic:"business", geo:"europe", subtopic:"business_corp"}

# Multi-geo dominant — pick the article's primary geographic focus
- "中印边境冲突升级" → {topic:"politics", geo:"asia_other"} (India dominates non-China narrative)
- "中俄联合军演谴责美国" → {topic:"politics", geo:"global"} (multi-country, no single dominant)
- "台积电赴日设厂" → {topic:"business", geo:"asia_other", subtopic:"business_corp"} (TW + JP both asia_other)
- "拜登访问东京会晤岸田文雄" → {topic:"politics", geo:"asia_other"} (Japan is host, focus is JP-US relations)

Titles ({N} total):
1. ...
2. ...
```

**Token budget estimate**:
- Prompt boilerplate: ~600 tokens
- Per title: ~30-50 tokens
- Output JSON: ~30 tokens per title (3 labels)
- Total per send: ~600 + 100 × 50 + 100 × 30 = ~8600 tokens
- gpt-4.1-mini cost ≈ $0.0017 per send × 3 sends/day × 30 days = **~$0.15/month**
- Current cost (200 articles, 5-cat): ~$0.35/month
- **Net savings: ~50%** (matches earlier estimate)

---

## §6 Implementation Tasks

(Order matters — each task should leave the system functional)

**Naming convention** (per Codex review): the article-level provenance field is `reason_code` everywhere — fixture column, in-pipeline attribute, log keys, monitoring metric. No `region_reason` or `_reason_code` aliases.

### Task 1: Add subtopic + geo data structures (no behavior change)
- New constants: `TOPIC_LABELS`, `GEO_LABELS`, `SUBTOPIC_LABELS`
- New fixture column: `reason_code` (str)
- Tests: validate label vocabularies are well-formed

### Task 2: Implement Stage 1 (Hard Lock — no change in scope)
- Already done (`_LOCKED_SOURCES`)
- Add `reason_code = "source_lock:hard:<source>"`
- Tests: 6 sources skip LLM, end up in CANADA/ECONOMIST

### Task 3: Implement Stage 2 (Soft Lock + Escape)
- New constant `_SOFT_LOCKS` mapping source → default region
- New constants `_ESCAPE_EXTERNAL_GEO` (Trump|Washington|Putin|Brussels|...) and `_OWN_GEO_PER_SOFT_LOCK` (CHINA→{中国|大陆|北京}, ASIA→{香港|日本|韩国})
- Logic: if source in _SOFT_LOCKS → check escape rule → either route (with `reason_code = "source_lock:soft:..."`) or fall through (with `reason_code = "soft_escape:..."`)
- Tests: 界面 article about CATL stays in CHINA; 界面 article about "拜登签法案" escapes to LLM

### Task 4: Implement Stage 3 (Geo Keyword Funnel)
- New constants `_CANADA_KEYWORDS`, `_ASIA_PAC_KEYWORDS`
- Apply to articles that fell through Stages 1-2
- `reason_code = "geo_keyword:<region>"`
- Tests: SCMP article with "加拿大" → CANADA; Bloomberg article with "Hong Kong" → ASIA-PAC

### Task 5: Update LLM prompt + parsing (Stage 4)
- New prompt template (§5) including the high-ambiguity examples block
- Parse 3-label JSON output `{topic, geo, subtopic}`
- Backward compat: tolerate missing `subtopic` for non-tech/business topics
- `reason_code = "llm:<topic>:<geo>"` or `"llm:china+<topic>"` for the Q1B branches
- Tests: parse various JSON shapes, fallback on malformed; verify all 6 high-ambiguity examples in §5 yield expected (topic, geo, subtopic) when run against gpt-4.1-mini

### Task 6: Routing matrix + REGION_GROUPS expansion (atomic, replaces old Tasks 6+7)
**Why atomic** (Codex point #1): the routing function returns new region keys (AI/前沿, 市场/宏观, 公司/产业, 消费科技, SOCIETY) that don't exist in the 7-zone REGION_GROUPS. Doing routing first would create a window where rendering crashes on unknown region keys. Doing REGION_GROUPS first would create a window where new regions exist but receive no articles. Must land together.
- Replace `_CATEGORY_TO_REGION` dict with `_route(source, title, topic, geo, subtopic)` function (§4.5 pseudocode)
- Function returns `(region_key, reason_code)`
- Replace 7-zone REGION_GROUPS with 10-zone version (§4.6)
- Add new emoji + region titles in display order per F3
- Tests: 40 fixture cases covering all routing branches × all 10 region keys exist in REGION_GROUPS

### Task 7: Update digest-tuning.json
- New region_quotas (10 zones, weights per §4.6)
- max_total_articles = 150, target_article_count = 150
- AR Rule 3 in program.md: lock new keys + new max value
- Tests: validate JSON, sum bounds

### Task 8: Implement reason_code logging + display
- Each article carries `reason_code` field through pipeline (no underscore prefix)
- Optional: render "[reason]" tooltip in HTML email (debug mode only)
- Add reason_code distribution stats to send log: `Stage1: N (M%) | Stage2: N (M%) | Stage3: N (M%) | Stage4: N (M%) | Fallback: N (M%)`

### Task 9: Update docs.sinostor.com.cn page
- Section 9 (`#app-news`) reflect 10 zones
- Update region count cards, source allocation diagram
- Update "唯一可调旋钮" current values (max_total 120→150)

### Task 10: Dry-run on 5 fixtures + before/after diff
- Run new classifier on 5 recent fixtures (mock LLM with deterministic dict)
- Compare region distribution to current emails
- Flag any article whose region moved unexpectedly
- User reviews diff before deploying

### Task 11: Production deploy + monitoring
- Push to main; next 00:10 BJT cron picks up
- Add alert: if Stage 4 (LLM) rate > 70% of articles, email warning (deterministic stages should catch ≥30%)
- Watch first 3 sends, validate user feels less duplication and no empty/unknown sections

---

## §7 Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LLM output malformed (missing subtopic for tech/business) | Medium | Article routed wrong | Default subtopic to `tech_ai` / `business_macro` if missing |
| 消费科技 zone too sparse (no consumer-tech articles in many sends) | High | Empty section in email | Region quota min=6 with hide-if-empty rendering |
| SOCIETY zone too sparse (similar) | High | Empty section | Same hide-if-empty |
| Soft-lock escape too aggressive (界面 articles flowing to politics often) | Low-Med | Chinese sources scatter (back to current bug) | Escape requires NO own-geo keywords; conservative threshold |
| AR tries to revert quota changes | High | volume re-shrinks | Already locked via Rule 3 (existing) — extend lock to new keys in Task 7 |
| Cross-send dedup state file path mismatch (just fixed in `da7177b`) | Low | History resets | Already verified on 2026-04-19 |
| Token budget overshoot on big-fetch days | Low | LLM call timeout | Same fallback as today: keyword classifier |
| User-visible region change without warning | Med | Confusion | Send 1 manual email with diff annotation before regular cron picks up |

---

## §8 Test Plan

### Unit tests (Python, no LLM call)

- `test_stage1_hard_lock`: 6 sources route correctly
- `test_stage2_soft_lock_no_escape`: 9 Chinese-source articles stay in CHINA
- `test_stage2_soft_lock_with_escape`: Article with "Trump" but not "中国" → falls through
- `test_stage3_geo_keyword_canada`: Title with "加拿大" → CANADA
- `test_stage3_geo_keyword_asia_pac`: Title with "Hong Kong" → ASIA-PAC
- `test_stage4_routing_matrix`: 30 (topic, geo, subtopic) combinations → expected region
- `test_quota_bounds`: region_quotas sum within bounds, max_total respected

### Fixture-based dry-run (Python with mocked LLM)

Use 5 most recent fixtures (`2026-04-19-*.json`):
- Mock LLM with deterministic dictionary (record current LLM output, replay)
- Run new classifier, compare region distribution before/after
- Output: per-region article count + per-source distribution (like §2 example)
- Acceptance criteria:
  - 中国财经要闻 articles in GLOBAL FINANCE drop from 7 → ≤2
  - CANADA region gets at least 1-2 articles from non-CBC/Globe sources
  - 消费科技 region has ≥4 articles per fetch (else recategorize threshold)

### Integration test (with real LLM, 1 fetch)

- Run full pipeline on today's data
- Verify reason_code distribution: target ≥30% Stage 1+2+3 (deterministic), ≤70% Stage 4 (LLM)
- Send to `chengli1986@gmail.com` only with [DEV] subject prefix
- Visual check: 10 zones render correctly, no empty section error

### Production canary (1 send, manual)

- After unit + integration pass, run 1 manual production send to full recipients
- Watch user feedback in following hour
- If complaints, rollback (revert commit + cron continues with old code)

---

## §9 Migration & Rollback

### Forward migration

1. All commits land on `main` in dependency order (Tasks 1-11, was 1-12 before Task 6/7 merge)
2. Each task includes its own tests; CI must pass before next task
3. Final commit triggers next 00:10 BJT cron with new pipeline

### Rollback

The classification entry point is mutated by **Tasks 3 (Stage 2 soft lock), 4 (Stage 3 geo keyword), 5 (LLM prompt + 3-label parser), 6 (routing + REGION_GROUPS), 7 (digest-tuning + AR Rule 3)**. A partial revert that only touches Task 6/7 would leave the new soft-lock and geo-keyword paths active against an old routing dict that doesn't recognize their outputs — broken intermediate state. So rollback must operate on the **full Task 3-7 boundary**.

#### Primary: Option A — Atomic git revert (default rollback path)

```bash
cd ~/global-news
git revert --no-commit <task7-commit> <task6-commit> <task5-commit> \
                       <task4-commit> <task3-commit>
git commit -m "rollback: classifier redesign — full revert Task 3-7"
git push
```

After push, next cron picks up old 7-zone classifier. Tasks 1-2 (data structures + hard lock) are safe to keep — they don't change behavior.

**Why this is the primary path** (per user decision 2026-04-19):
1. **Symmetric with delivery**: Tasks 3-7 are inter-dependent and were merged at the commit-boundary level; rollback should travel the same boundary
2. **Auditability**: when investigating "why did this email change?", commit history (revert + push) is more legible than env-var state changes
3. **No long-term maintenance debt**: avoids carrying two complete classifier code paths in the codebase

#### Emergency-only: Option B — env-var kill switch (do not default to this)

Implement in Task 11 a top-of-`classify_articles()` check **with explicit constraints**:

```python
# Emergency kill switch — see §9 for usage policy.
# Setting NEWS_CLASSIFIER_VERSION=v1 disables the ENTIRE new classification chain
# (Stages 2/3/4 + new routing + 10-zone REGION_GROUPS) and falls back to the
# pre-redesign code path. Not for partial-stage skipping.
if os.environ.get("NEWS_CLASSIFIER_VERSION", "v2") == "v1":
    return self._classify_articles_v1_legacy(...)
```

**Strict usage constraints**:
1. **Only when**: cron is about to send wrong emails AND there is no time to complete the git revert + push cycle (<5 min before next cron fires)
2. **Switch semantics is all-or-nothing**: setting to `v1` MUST disable the full new classification chain (Stages 2/3/4 + routing + 10-zone REGION_GROUPS). It is NOT a flag for partial stage skipping; do not introduce per-stage toggles
3. **Use is followed by Option A**: once the immediate fire is out, complete the proper Option A revert + push within the same day. The kill switch is a fuse, not a config knob
4. **Sunset**: Option B code path removed 2 weeks after stable production (Task 11 includes a calendar reminder to delete `_classify_articles_v1_legacy` + the env-var check)

**Other rollback notes**:
- Task 8 (reason_code logging) and Task 9 (docs.sinostor) are display-only, safe to leave even after rollback — they degrade gracefully
- Sent-today state file path stays — no data loss
- AR Rule 3 lock value reverts together with digest-tuning.json (same commit in Task 7)

### Monitoring

- Send log: count `reason_code` per stage, alert if Stage 4 rate > 70%
- Send log: count empty-region renders, alert if 3+ empty zones
- AR cron: continue daily, will explore freshness/dedup/tier_boost within new quota constraints
- After 7 days: review user feedback, decide:
  - Keep current 10-zone structure
  - Merge SOCIETY back if sparse
  - Adjust quota mins if certain zones consistently empty

---

## §10 Open Follow-Up Questions (Post-Implementation)

These don't block the spec but should be revisited after 1-2 weeks of new pipeline:

1. **F2 reconsideration**: If 消费科技 zone overflows (>10 articles regularly), revisit topic_boost mechanism (Plan B in F2 discussion)
2. **POLITICS sub-split**: Politics zone may itself become noisy (war + elections + diplomacy). Consider sub-topics if user feedback warrants
3. **CHINA zone density**: If CHINA pulls all society/business + politics overflow, may need internal sub-section (财经/科技/社会 sub-headers within zone)
4. **ECONOMIST zone**: Currently tier=premium with 1.5× boost — may be over-represented. Revisit if Economist articles dominate
5. **AR autoresearch program.md**: Add Rule 4 specifying which keys (region_quotas inner shape, tier_boost) AR can/cannot tune after redesign
6. **Reason_code analytics**: Build dashboard panel on docs.sinostor showing per-stage classification rates as health metric

---

## §11 Implementation Trigger

This spec is ready for plan generation:
- [x] User reviews Q1-Q5 + F1-F3 decisions and any open issues
- [x] Task ordering reviewed (Codex review point #1: Task 6+7 merged into atomic Task 6 — see §6)
- [x] reason_code naming unified (Codex review point #2: single `reason_code` everywhere)
- [x] Rollback covers entry-point changes (Codex review point #3: §9 explicit Task 3-7 atomic revert; user chose Option A as default with Option B emergency-only)
- [x] §5 LLM prompt examples cover high-ambiguity cases (Codex review point #4: 12 new example outputs across 6 boundary categories)

**Status**: ✅ Sign-off complete 2026-04-19. Ready to generate implementation plan.

Next file: `docs/superpowers/plans/2026-04-19-classification-redesign.md` — breaks each Task 1-11 into concrete code changes + tests + commit message templates, executes task-by-task with checkpoints.

---

## Revision Log

- **2026-04-19 v1**: Initial spec (commit `7099359`)
- **2026-04-19 v2** (commit `d01658e`): Codex review fixes — Task 6+7 merged atomic; reason_code naming unified; §9 rollback covers Task 3-7 with optional kill-switch; §5 added 12 high-ambiguity examples across 6 boundary categories
- **2026-04-19 v3**: Rollback strategy finalized — Option A (atomic git revert) is the **default** path; Option B (env-var kill switch) demoted to emergency-only with strict usage constraints (must be all-or-nothing, must be followed by Option A revert same-day, sunsets 2 weeks after stable production)

---

**Spec status**: Complete, awaiting user review
**Next file**: implementation plan after sign-off
