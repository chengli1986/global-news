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

Priority order (top match wins):

```
IF source ∈ HARD_LOCK:                   → that region (Stage 1)
IF source ∈ SOFT_LOCK AND not escaped:   → that region (Stage 2)
IF geo_keyword ∈ {canada, asia_other}:   → that region (Stage 3)

# After Stage 4 LLM (topic, geo, subtopic):

# Geo-priority for non-global-comparison regions
IF geo == "canada":                      → 🇨🇦 CANADA
IF geo == "asia_other":                  → 🌏 ASIA-PAC
IF geo == "china":
    IF topic IN {"society", "business"}: → 🇨🇳 CHINA          # Q1 B
    ELIF topic == "consumer_tech":       → 🇨🇳 CHINA          # Chinese consumer products belong w/ China
    # else (china + tech/politics) falls through to topic routing

# Topic-priority for global-comparison regions
IF topic == "tech":
    IF subtopic == "tech_consumer":      → 📱 消费科技
    ELSE:                                → 🧠 AI/前沿
IF topic == "business":
    IF subtopic == "business_corp":      → 🏢 公司/产业
    ELSE:                                → 📈 市场/宏观
IF topic == "politics":                  → 🏛 POLITICS
IF topic == "society":
    IF geo IN {"china", "canada", "asia_other"}: → that geo region
    ELSE:                                        → 🌐 SOCIETY  # Q4 C

# Fallback (LLM failure or no match)
ELSE:                                    → source default region (REGION_GROUPS)
```

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

Examples:
- "Fed signals April hold" → {topic:"business", geo:"us", subtopic:"business_macro"}
- "Apple Q4 earnings beat" → {topic:"business", geo:"us", subtopic:"business_corp"}
- "宁德时代为什么赚这么多钱" → {topic:"business", geo:"china", subtopic:"business_corp"}
- "iPhone 17 review" → {topic:"consumer_tech", geo:"us"}
- "Anthropic releases Claude 5" → {topic:"tech", geo:"us", subtopic:"tech_ai"}
- "OpenAI raises $100B" → {topic:"tech", geo:"us", subtopic:"tech_ai"}
- "Iran-Israel ceasefire" → {topic:"politics", geo:"global"}
- "卢特尼克对加拿大说糟透了" → {topic:"politics", geo:"canada"}
- "Guardian: 美国得州禁书法案" → {topic:"society", geo:"us"}

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

### Task 1: Add subtopic + geo data structures (no behavior change)
- New constants: `TOPIC_LABELS`, `GEO_LABELS`, `SUBTOPIC_LABELS`
- New fixture column: `region_reason` (str)
- Tests: validate label vocabularies are well-formed

### Task 2: Implement Stage 1 (Hard Lock — no change in scope)
- Already done (`_LOCKED_SOURCES`)
- Add `reason_code = "source_lock:hard"`
- Tests: 6 sources skip LLM, end up in CANADA/ECONOMIST

### Task 3: Implement Stage 2 (Soft Lock + Escape)
- New constant `_SOFT_LOCKS` mapping source → default region
- New constant `_ESCAPE_KEYWORDS` for external geo (Trump|Washington|Putin|Brussels|...)
- New constant `_OWN_GEO_KEYWORDS` for the soft-lock's own geo (中国|大陆|...)
- Logic: if source in _SOFT_LOCKS → check escape rule → either route or fall through
- Tests: 界面 article about CATL stays in CHINA; 界面 article about "拜登签法案" escapes to LLM

### Task 4: Implement Stage 3 (Geo Keyword Funnel)
- New constants `_CANADA_KEYWORDS`, `_ASIA_PAC_KEYWORDS`
- Apply to articles that fell through Stages 1-2
- Tests: SCMP article with "加拿大" → CANADA; Bloomberg article with "Hong Kong" → ASIA-PAC

### Task 5: Update LLM prompt + parsing (Stage 4)
- New prompt template (§5)
- Parse 3-label JSON output
- Backward compat: handle old single-label response gracefully (during deploy)
- Tests: parse various JSON shapes, fallback on malformed

### Task 6: Implement routing matrix (§4.5)
- Replace `_CATEGORY_TO_REGION` with new `_ROUTING_MATRIX` function
- Returns (region, reason_code) tuple
- Tests: 30 fixture cases covering all routing branches

### Task 7: Update REGION_GROUPS for 10 zones
- Replace 7-zone REGION_GROUPS with 10-zone version
- Add new emoji + region titles
- Update display order per F3
- Tests: render test with 10 sections, all source assignments

### Task 8: Update digest-tuning.json
- New region_quotas (10 zones, weights per §4.6)
- max_total_articles = 150, target_article_count = 150
- AR Rule 3 in program.md: lock new keys + new max value
- Tests: validate JSON, sum bounds

### Task 9: Implement reason_code logging + display
- Each article carries `_reason_code` field through pipeline
- Optional: render "[reason]" tooltip in HTML email (debug mode)
- Add reason_code distribution stats to send log

### Task 10: Update docs.sinostor.com.cn page
- Section 9 (`#app-news`) reflect 10 zones
- Update region count cards, source allocation diagram
- Update "唯一可调旋钮" current values

### Task 11: Dry-run on 5 fixtures + before/after diff
- Run new classifier on 5 recent fixtures
- Compare region distribution to current emails
- Flag any article that moved unexpectedly
- User reviews diff before deploying

### Task 12: Production deploy + monitoring
- Push to main; next 00:10 BJT cron picks up
- Add alert: if reclassification rate > 30% (vs target <15%), email warning
- Watch first 3 sends, validate user feels less duplication

---

## §7 Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LLM output malformed (missing subtopic for tech/business) | Medium | Article routed wrong | Default subtopic to `tech_ai` / `business_macro` if missing |
| 消费科技 zone too sparse (no consumer-tech articles in many sends) | High | Empty section in email | Region quota min=6 with hide-if-empty rendering |
| SOCIETY zone too sparse (similar) | High | Empty section | Same hide-if-empty |
| Soft-lock escape too aggressive (界面 articles flowing to politics often) | Low-Med | Chinese sources scatter (back to current bug) | Escape requires NO own-geo keywords; conservative threshold |
| AR tries to revert quota changes | High | volume re-shrinks | Already locked via Rule 3 (existing) — extend lock to new keys in Task 8 |
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

1. All commits land on `main` in dependency order (Tasks 1-12)
2. Each task includes its own tests; CI must pass before next task
3. Final commit triggers next 00:10 BJT cron with new pipeline

### Rollback

- `git revert <task6-commit>` reverts routing matrix change (most likely revert target)
- `git revert <task7-commit>` reverts REGION_GROUPS to 7 zones
- `git revert <task8-commit>` reverts digest-tuning.json
- digest-tuning.json reverts also need AR program.md Rule 3 lock value rollback
- Sent-today state file path stays — no data loss

### Monitoring

- Send log: count `region_reason` per stage, alert if Stage 4 rate > 70%
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

This spec is ready for plan generation when:
- [ ] User reviews Q1-Q5 + F1-F3 decisions and any open issues
- [ ] User confirms Task 1-12 ordering OK
- [ ] User confirms region_quotas in §4.6 are reasonable starting points
- [ ] User confirms LLM prompt examples in §5 cover their key edge cases

After confirmation: I'll generate the implementation plan (`docs/superpowers/plans/2026-04-19-classification-redesign.md`) breaking each Task into concrete code changes + tests, then execute task-by-task with checkpoints.

---

**Spec status**: Complete, awaiting user review
**Next file**: implementation plan after sign-off
