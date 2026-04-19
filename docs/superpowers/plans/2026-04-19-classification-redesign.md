# News Classification Redesign — Implementation Plan

**Date**: 2026-04-19
**Spec**: `docs/superpowers/specs/2026-04-19-classification-redesign.md` (v3, commit `a6cd56f`)
**Status**: Ready to execute task-by-task with checkpoints
**Author**: Claude Opus 4.7 + user discussion

---

## §0 Plan Summary

11 tasks delivered in dependency order. Each task is its own commit; user reviews diff + test output before next task starts.

| # | Task | Risk | Est. LOC | Tests added |
|---|------|------|----------|-------------|
| 1 | Add label vocabularies (no behavior) | Low | ~30 | 3 |
| 2 | Stage 1 hard lock — annotate reason_code | Low | ~10 | 2 |
| 3 | Stage 2 soft lock + escape | Med | ~80 | 8 |
| 4 | Stage 3 geo keyword funnel | Low | ~50 | 6 |
| 5 | LLM prompt + 3-label parser | Med | ~120 | 7 |
| 6 | Routing matrix + REGION_GROUPS expansion (atomic) | **High** | ~150 | 12 |
| 7 | digest-tuning.json + AR Rule 3 update | Low | ~30 | 2 |
| 8 | reason_code logging + send-log stats | Low | ~40 | 3 |
| 9 | docs.sinostor news section update | Low | ~60 | 0 (manual verify) |
| 10 | Dry-run on 5 fixtures + diff report | — | ~80 (test code) | 1 (the dry-run itself) |
| 11 | Production deploy + monitoring + Option B kill switch | Med | ~50 | 2 |

**Total**: ~700 LOC code + ~46 unit tests + 1 dry-run script.

**Test infrastructure**: `~/global-news/tests/` with conftest.py setup. New tests follow existing convention (class-grouped pytest, mocked LLM via `unittest.mock`).

**Checkpoint protocol**:
- After each task: I commit + push + run tests + show diff/output → wait for your "next" before starting the next task
- Any task that touches `unified-global-news-sender.py` is followed by `python3 -m py_compile` + targeted pytest run
- Task 6 (the high-risk atomic one) gets a longer pause — I'll show the full routing function + all 12 routing tests

---

## §1 Task 1 — Add label vocabularies (no behavior change)

**Goal**: Define `TOPIC_LABELS`, `GEO_LABELS`, `SUBTOPIC_LABELS` constants. Future tasks reference these. No routing changes.

**Files touched**:
- `unified-global-news-sender.py` (add constants near line 38, after `JACCARD_SIMILARITY_THRESHOLD`)

**Changes**:
```python
# After line 39 (HEADERS = {...}):

TOPIC_LABELS = frozenset({"politics", "business", "tech", "consumer_tech", "society"})
GEO_LABELS   = frozenset({"china", "canada", "asia_other", "us", "europe", "global"})
SUBTOPIC_LABELS = {
    "tech": frozenset({"tech_ai", "tech_consumer"}),
    "business": frozenset({"business_macro", "business_corp"}),
}
# Subtopic is optional for politics/consumer_tech/society (single-label topics)

# reason_code prefixes (for type-checking + monitoring grouping)
REASON_PREFIXES = frozenset({
    "source_lock:hard", "source_lock:soft", "soft_escape",
    "geo_keyword:canada", "geo_keyword:asia_pac",
    "llm:china", "llm:topic", "llm:geo",
    "fallback:source_default",
})
```

**Tests** (in `tests/test_classification.py` — NEW file):
- `test_topic_labels_well_formed`: 5 unique strings, all lowercase
- `test_geo_labels_well_formed`: 6 unique strings
- `test_subtopic_labels_only_for_tech_business`: dict has exactly 2 keys, values are frozensets

**Acceptance**:
- `python3 -m py_compile unified-global-news-sender.py` passes
- `pytest tests/test_classification.py -v` shows 3 passed
- `git diff` <50 lines

**Commit message**:
```
feat(classifier): add topic/geo/subtopic label vocabularies (Task 1/11)

Defines TOPIC_LABELS (5), GEO_LABELS (6), SUBTOPIC_LABELS (per-topic),
REASON_PREFIXES (validation set). No behavior change yet — future tasks
reference these constants.

Spec: 2026-04-19-classification-redesign.md §4.2-§4.4
```

---

## §2 Task 2 — Stage 1 hard lock annotate reason_code

**Goal**: Existing `_LOCKED_SOURCES` already short-circuits LLM. Add `reason_code` annotation to articles routed by hard lock so monitoring works (Task 8).

**Files touched**:
- `unified-global-news-sender.py:559-561` (the `if src in self._LOCKED_SOURCES: continue` block in `classify_articles`)
- `unified-global-news-sender.py:_collect_region_articles` (where reason_code lives on the per-article dict)

**Changes**:
- Introduce `_classifications` to hold `(source, idx) → {"region": str, "reason_code": str, "topic": str|None, "geo": str|None, "subtopic": str|None}` (richer than current `(source, idx) → region_title`)
- For hard-lock sources, populate with `region=<source default>, reason_code="source_lock:hard:<source>"`

**Tests** (extend `tests/test_classification.py`):
- `test_stage1_hard_lock_cbc_business`: CBC Business article → region=CANADA, reason_code starts with "source_lock:hard"
- `test_stage1_hard_lock_economist_finance`: Economist Finance → ECONOMIST, same prefix

**Acceptance**:
- All 6 hard-lock sources pass; non-locked sources unchanged
- Compile + targeted pytest passes
- Existing `tests/test_unified_sender.py` still passes (no regression)

**Commit message**:
```
feat(classifier): annotate reason_code on Stage 1 hard-lock route (Task 2/11)

Stage 1 (hard lock for CBC/Globe/Economist×4) was already short-circuiting
LLM, but lacked the reason_code annotation needed by monitoring. Add
structured _classifications dict carrying {region, reason_code, topic,
geo, subtopic} per article.

Spec: 2026-04-19-classification-redesign.md §4.1 Stage 1
```

---

## §3 Task 3 — Stage 2 soft lock + escape

**Goal**: Soft-lock 9 Chinese sources to CHINA region by default. Escape to LLM if title hits external geo keywords AND no own-geo keyword. Same pattern for ASIA-bias sources.

**Files touched**:
- `unified-global-news-sender.py` (new constants + new helper + extend `classify_articles`)

**Changes**:
```python
# New constants near line 50:
_SOFT_LOCKS = {
    # Source → default region key
    "界面新闻": "🇨🇳 中国要闻 CHINA",
    "南方周末": "🇨🇳 中国要闻 CHINA",
    "中国财经要闻": "🇨🇳 中国要闻 CHINA",
    "中国科技/AI": "🇨🇳 中国要闻 CHINA",
    "36氪": "🇨🇳 中国要闻 CHINA",
    "虎嗅": "🇨🇳 中国要闻 CHINA",
    "钛媒体": "🇨🇳 中国要闻 CHINA",
    "IT之家": "🇨🇳 中国要闻 CHINA",
    "少数派": "🇨🇳 中国要闻 CHINA",
    "SCMP Hong Kong": "🌏 亚太要闻 ASIA-PACIFIC",
    "RTHK中文": "🌏 亚太要闻 ASIA-PACIFIC",
    "HKFP": "🌏 亚太要闻 ASIA-PACIFIC",
    "Straits Times": "🌏 亚太要闻 ASIA-PACIFIC",
    "日经中文": "🌏 亚太要闻 ASIA-PACIFIC",
}

# External-geo keywords that suggest the article's focus is OUTSIDE the source's natural region
_ESCAPE_EXTERNAL_GEO = re.compile(
    r"(Trump|Biden|Putin|Zelensky|Macron|Merkel|Modi|Sunak|Carney|Trudeau|"
    r"Washington|Brussels|Moscow|Kyiv|Berlin|Paris|London|Ottawa|"
    r"美国|华盛顿|联邦|白宫|拜登|特朗普|"
    r"俄罗斯|乌克兰|普京|"
    r"欧盟|英国|德国|法国|"
    r"加拿大|渥太华|多伦多|温哥华)",
    re.IGNORECASE,
)

# Own-geo keywords per soft-lock region (presence cancels escape — article is genuinely about own region)
_OWN_GEO_PER_REGION = {
    "🇨🇳 中国要闻 CHINA":     re.compile(r"(中国|大陆|北京|上海|深圳|习近平|央行|国务院|人民币|A股|港股|沪深)"),
    "🌏 亚太要闻 ASIA-PACIFIC": re.compile(r"(香港|新加坡|日本|韩国|越南|泰国|印尼|马来|缅甸|台湾|印度|澳大利亚|新西兰|首尔|东京|HK|Hong Kong)"),
}

def _stage2_check(self, source: str, title: str) -> tuple[str, str] | None:
    """Returns (region, reason_code) if soft-lock applies and not escaped; else None."""
    if source not in _SOFT_LOCKS:
        return None
    region = _SOFT_LOCKS[source]
    has_external = bool(_ESCAPE_EXTERNAL_GEO.search(title))
    has_own = bool(_OWN_GEO_PER_REGION[region].search(title))
    if has_external and not has_own:
        return None  # escape — fall through to LLM
    return region, f"source_lock:soft:{source}"
```

Then in `classify_articles`, before the `to_classify` loop, check Stage 2:
- If `_stage2_check` returns `(region, reason)`, set `_classifications[(src, idx)] = {region, reason, ...}` and skip from `to_classify`
- Else (escape), proceed to LLM as before but mark `reason_code = "soft_escape:<source>"` (set by Task 5's LLM result)

**Tests** (extend `tests/test_classification.py`):
- `test_soft_lock_jiemian_china_topic_stays`: 界面 + "宁德时代财报" → CHINA, soft
- `test_soft_lock_jiemian_us_topic_escapes`: 界面 + "拜登签署对台法案" → None (escape)
- `test_soft_lock_jiemian_mixed_keeps`: 界面 + "中国回应特朗普关税" → CHINA (own keyword present)
- `test_soft_lock_scmp_hk_asian_topic_stays`: SCMP HK + "Singapore housing surge" → ASIA-PAC, soft
- `test_soft_lock_scmp_hk_us_topic_escapes`: SCMP HK + "Putin meets Trump" → None (escape)
- `test_soft_lock_huxiu_pure_tech_stays`: 虎嗅 + "国产 GPU 厂商融资" → CHINA, soft
- `test_soft_lock_non_softlock_source_returns_none`: BBC World article → None (not in soft lock)
- `test_soft_lock_36kr_with_chinese_company`: 36氪 + "字节跳动 Sora 对标 OpenAI" → CHINA (own keyword 字节)

Wait — 字节 is not in my _OWN_GEO_PER_REGION list. Add it.

Better, let me revise: own-geo keywords should include common Chinese company name patterns? Probably overengineered. Let me keep keyword list focused on **explicit geo names** and accept that articles like "字节跳动 vs OpenAI" will escape if there's no other Chinese keyword. Most such titles will mention 中国 / 国产 / 美国 anyway.

Actually for this case the title is "字节跳动 Sora 对标 OpenAI" — has "OpenAI" but no external geo per my list. Wait, OpenAI doesn't match `_ESCAPE_EXTERNAL_GEO`. Let me trace: external regex doesn't include "OpenAI" as it's a company not a geo. So `has_external=False`, no escape, stays CHINA. ✓

OK keep keyword lists narrow.

**Acceptance**:
- 8 new tests pass
- Compile passes
- No regression in existing tests

**Commit message**:
```
feat(classifier): Stage 2 soft lock + escape rule (Task 3/11)

Soft-lock 9 Chinese sources to CHINA + 5 Asia-Pac sources to ASIA-PAC.
Escape rule: if title hits external geo keyword (Trump/Putin/EU/etc.)
AND no own-geo keyword (中国/大陆/etc. for CHINA, 香港/日本/etc. for
ASIA), fall through to LLM. Otherwise route to source-default region.

Spec: 2026-04-19-classification-redesign.md §4.1 Stage 2
```

---

## §4 Task 4 — Stage 3 geo keyword funnel

**Goal**: Articles that pass Stage 1+2 (i.e., source not locked) get scanned for strong CANADA/ASIA-PAC geo keywords. Match → route to that region without LLM.

**Files touched**:
- `unified-global-news-sender.py` (new constants + extend `classify_articles`)

**Changes**:
```python
# New constants:
_CANADA_KEYWORDS = re.compile(
    r"(Canad[ai]|Toronto|Vancouver|Ottawa|Montreal|Calgary|Quebec|Alberta|"
    r"Trudeau|Carney|加拿大|多伦多|温哥华|渥太华|蒙特利尔|魁北克)",
    re.IGNORECASE,
)
_ASIA_PAC_KEYWORDS = re.compile(
    r"(Hong Kong|Singapore|Japan|Japanese|Tokyo|Korea|Korean|Seoul|"
    r"Vietnam|Thailand|Indonesia|Malaysia|Philippines|Myanmar|"
    r"India|Indian|Mumbai|Delhi|"
    r"Australia|Australian|Sydney|Melbourne|New Zealand|"
    r"Taiwan|Taipei|Mongolia|"
    r"香港|新加坡|日本|东京|韩国|首尔|"
    r"越南|泰国|印度|印尼|马来西亚|菲律宾|缅甸|"
    r"澳大利亚|新西兰|台湾|台北|蒙古|"
    r"日经|台积电)",
    re.IGNORECASE,
)

def _stage3_check(self, title: str) -> tuple[str, str] | None:
    """Returns (region, reason_code) on geo keyword match; else None."""
    if _CANADA_KEYWORDS.search(title):
        return "🇨🇦 加拿大 CANADA", "geo_keyword:canada"
    if _ASIA_PAC_KEYWORDS.search(title):
        return "🌏 亚太要闻 ASIA-PACIFIC", "geo_keyword:asia_pac"
    return None
```

Apply in classify_articles **after** Stage 2 escape: articles falling through Stage 1+2 are checked here before being added to LLM batch.

**Tests** (extend `tests/test_classification.py`):
- `test_stage3_canada_chinese_keyword`: SCMP + "加拿大男子 Kenneth Law 案" → CANADA
- `test_stage3_canada_english_keyword`: FT + "Trudeau announces budget" → CANADA
- `test_stage3_asia_japan`: Bloomberg + "Japan inflation hits 4%" → ASIA-PAC
- `test_stage3_asia_india`: NYT + "India IPO market booms" → ASIA-PAC
- `test_stage3_asia_taiwan`: Bloomberg + "TSMC quarterly results" → ASIA-PAC (matches 台积电 OR Taiwan)
- `test_stage3_no_match_returns_none`: BBC + "Federal Reserve hikes rates" → None (no geo keyword)

**Acceptance**:
- 6 new tests pass
- Critical: Stage 3 must NOT match for already-locked sources (those stop at Stage 1 or 2 — order matters in `classify_articles`)

**Commit message**:
```
feat(classifier): Stage 3 geo keyword funnel for CANADA + ASIA-PAC (Task 4/11)

After source-lock stages, scan title for strong geographic keywords.
Match → route to CANADA / ASIA-PAC without LLM. Catches the cases that
the spec's §2 example data showed as scattered today (FT writing about
加拿大 → currently lands in FINANCE, will route to CANADA).

Spec: 2026-04-19-classification-redesign.md §4.1 Stage 3
```

---

## §5 Task 5 — LLM prompt + 3-label parser

**Goal**: Replace single-label prompt with 3-label (topic, geo, subtopic). Parse new JSON shape. Backward-compat for malformed LLM output.

**Files touched**:
- `unified-global-news-sender.py:572-625` (the prompt + parsing block in `classify_articles`)

**Changes**:
- Replace prompt with new template per spec §5 (5 topics + 6 geos + subtopics + 9 low-ambig + 12 high-ambig examples)
- Parse expects `{"<idx>": {"topic": str, "geo": str, "subtopic": str|None}}`
- Validation: drop articles with topic ∉ TOPIC_LABELS or geo ∉ GEO_LABELS
- Missing subtopic for tech/business → log warning + default to `tech_ai` / `business_macro`
- Articles that escaped Stage 2 keep their `reason_code = "soft_escape:<source>"` (Stage 5 LLM annotates `topic/geo/subtopic` but reason_code remains soft_escape)
- Articles that reached Stage 4 organically get `reason_code = "llm:topic:<topic>"` or `"llm:geo:<geo>"` based on routing matrix outcome (set in Task 6)

**Note on prompt size**: with 21 examples (9 low + 12 high), prompt grows to ~1500 tokens. Per-send cost still ~$0.0017, comparable to current.

**Tests** (extend `tests/test_classification.py`):
- `test_parse_well_formed_3label`: `{"1": {"topic": "tech", "geo": "us", "subtopic": "tech_ai"}}` → expected dict
- `test_parse_missing_subtopic_for_tech_defaults`: missing subtopic on `tech` → defaults to `tech_ai`, logs warning
- `test_parse_missing_subtopic_for_politics_ok`: missing subtopic on `politics` → no warning (subtopic not required)
- `test_parse_invalid_topic_drops`: topic="news" → drops article from classifications, falls back to source default
- `test_parse_invalid_geo_drops`: geo="mars" → drops article
- `test_parse_nested_classifications_shape`: `{"classifications": {"1": {...}}}` → handles legacy nested shape
- `test_parse_malformed_json_returns_empty`: invalid JSON → returns empty dict, classify_articles falls back to keyword-based reclassifier

**Acceptance**:
- 7 new tests pass
- Mock LLM call returns 3-label JSON; parse extracts correctly
- Fallback (LLM API error) still works (existing `_keyword_reclassify` path untouched)

**Commit message**:
```
feat(classifier): 3-label LLM prompt + parser (Task 5/11)

Replace single-label classification with {topic, geo, subtopic} triple.
New prompt includes 9 low-ambiguity + 12 high-ambiguity examples
(china+tech_ai → topic region per Q1B exemption, china+consumer_tech →
CHINA, asia_other+society → ASIA-PAC, europe+macro/corp distinction,
multi-geo dominant judgment).

Parser is permissive on missing subtopic for tech/business (defaults
to tech_ai/business_macro with warning); strict on invalid topic/geo
(drops article, falls back to source default region).

Spec: 2026-04-19-classification-redesign.md §5
```

---

## §6 Task 6 — Routing matrix + REGION_GROUPS expansion (atomic)

**Goal** (highest-risk task; merged per Codex review): introduce `_route()` function returning `(region_key, reason_code)` AND expand `REGION_GROUPS` from 7 to 10 zones in the same commit. Atomicity prevents intermediate broken state.

**Files touched**:
- `unified-global-news-sender.py:493-521` (replace REGION_GROUPS)
- `unified-global-news-sender.py:530-536` (replace _CATEGORY_TO_REGION with _route function)
- `unified-global-news-sender.py:_collect_region_articles` (call _route instead of dict lookup)

**Changes**:

### 10-zone REGION_GROUPS (display order per F3)

```python
# Region keys (constants for cross-reference)
REGION_AI_FRONTIER   = "🧠 AI/前沿 AI FRONTIER"
REGION_MACRO_MARKETS = "📈 市场/宏观 MACRO & MARKETS"
REGION_POLITICS      = "🏛 全球政治 GLOBAL POLITICS"
REGION_CHINA         = "🇨🇳 中国要闻 CHINA"
REGION_CORP_INDUSTRY = "🏢 公司/产业 CORPORATE & INDUSTRY"
REGION_CONSUMER_TECH = "📱 消费科技 CONSUMER TECH"
REGION_ASIA_PAC      = "🌏 亚太要闻 ASIA-PACIFIC"
REGION_CANADA        = "🇨🇦 加拿大 CANADA"
REGION_ECONOMIST     = "📕 经济学人 THE ECONOMIST"
REGION_SOCIETY       = "🌐 社会观察 SOCIETY"

REGION_GROUPS = [
    (REGION_AI_FRONTIER,   ["中国科技/AI", "TechCrunch", "Hacker News", "Ars Technica",
                             "BBC Technology", "NYT Technology", "Solidot", "The Verge",
                             "36氪", "钛媒体", "IT之家", "少数派", "虎嗅"]),
    (REGION_MACRO_MARKETS, ["Bloomberg Econ", "Bloomberg", "FT", "CNBC"]),
    (REGION_POLITICS,      ["BBC World", "纽约时报中文", "BBC中文", "Bloomberg Politics",
                             "The Guardian World", "SCMP"]),
    (REGION_CHINA,         ["界面新闻", "南方周末", "中国财经要闻"]),
    (REGION_CORP_INDUSTRY, ["NYT Business"]),  # most fed by LLM routing
    (REGION_CONSUMER_TECH, []),  # populated entirely by LLM routing
    (REGION_ASIA_PAC,      ["日经中文", "CNA", "SCMP Hong Kong", "RTHK中文",
                             "HKFP", "Straits Times"]),
    (REGION_CANADA,        ["CBC Business", "Globe & Mail"]),  # LOCKED
    (REGION_ECONOMIST,     ["Economist Leaders", "Economist Finance",
                             "Economist Business", "Economist Science"]),  # LOCKED
    (REGION_SOCIETY,       []),  # populated entirely by LLM routing
]
```

### _route() function

```python
def _route(self, source: str, title: str, topic: str | None,
           geo: str | None, subtopic: str | None) -> tuple[str, str]:
    """Map article signals → (region_key, reason_code). Early-return.

    See spec §4.5 for the canonical decision matrix.
    """
    # Stage 1 + 2 + 3 are pre-classified before LLM; this function is called
    # AFTER Stage 4 (LLM) for articles that reached LLM. Stages 1-3 should
    # have already returned via _stage1/2/3_check before this is invoked.
    # Defense in depth: if called with no topic/geo, fall back to source default.

    if topic is None or geo is None:
        return self._source_default_region(source), "fallback:source_default"

    # 4a. Geo-priority for personal-context regions
    if geo == "canada":
        return REGION_CANADA, "llm:geo:canada"
    if geo == "asia_other":
        return REGION_ASIA_PAC, "llm:geo:asia_other"
    if geo == "china":
        if topic in ("society", "business", "consumer_tech"):
            return REGION_CHINA, f"llm:china+{topic}"
        # china + tech (subtopic tech_ai) or politics → falls to topic routing

    # 4b. Topic-priority for global-comparison regions
    if topic == "tech":
        if subtopic == "tech_consumer":
            return REGION_CONSUMER_TECH, "llm:topic:tech_consumer"
        return REGION_AI_FRONTIER, "llm:topic:tech_ai"
    if topic == "consumer_tech":
        return REGION_CONSUMER_TECH, "llm:topic:consumer_tech"
    if topic == "business":
        if subtopic == "business_corp":
            return REGION_CORP_INDUSTRY, "llm:topic:business_corp"
        return REGION_MACRO_MARKETS, "llm:topic:business_macro"
    if topic == "politics":
        return REGION_POLITICS, "llm:topic:politics"
    if topic == "society":
        return REGION_SOCIETY, "llm:topic:society"

    return self._source_default_region(source), "fallback:source_default"

def _source_default_region(self, source: str) -> str:
    """Find the region that lists `source` in REGION_GROUPS; fallback to first region."""
    for region, sources in self.REGION_GROUPS:
        if source in sources:
            return region
    return self.REGION_GROUPS[0][0]  # fallback (should rarely hit)
```

### Integration in `_collect_region_articles`

Replace the existing reclassification logic (lines 665-697) to:
1. Walk articles, check `_classifications[(src, idx)]` for pre-set region
2. If not pre-set, call `_route(src, title, topic, geo, subtopic)` using LLM output
3. Insert into `region_buckets[region_key]`
4. Carry `reason_code` to article tuple metadata for Task 8

**Tests** (extend `tests/test_classification.py`):
- 12 routing matrix cases (one per spec §5 example):
  - `test_route_china_business_corp_to_china`: topic=business, geo=china, subtopic=business_corp → REGION_CHINA, reason="llm:china+business"
  - `test_route_china_tech_ai_to_ai_frontier`: topic=tech, geo=china, subtopic=tech_ai → REGION_AI_FRONTIER, reason="llm:topic:tech_ai"
  - `test_route_china_consumer_tech_to_china`: topic=consumer_tech, geo=china → REGION_CHINA, reason="llm:china+consumer_tech"
  - `test_route_china_politics_to_politics`: topic=politics, geo=china → REGION_POLITICS, reason="llm:topic:politics"
  - `test_route_canada_society_to_canada`: topic=society, geo=canada → REGION_CANADA (geo wins via 4a)
  - `test_route_asia_other_society_to_asia_pac`: topic=society, geo=asia_other → REGION_ASIA_PAC
  - `test_route_us_business_macro_to_macro`: topic=business, geo=us, subtopic=business_macro → REGION_MACRO_MARKETS
  - `test_route_us_business_corp_to_corp`: topic=business, geo=us, subtopic=business_corp → REGION_CORP_INDUSTRY
  - `test_route_us_society_to_society`: topic=society, geo=us → REGION_SOCIETY
  - `test_route_global_politics_to_politics`: topic=politics, geo=global → REGION_POLITICS
  - `test_route_missing_topic_fallback`: topic=None, geo=us → fallback:source_default region
  - `test_route_invalid_topic_fallback`: topic="news", geo=us → fallback:source_default region (not in matrix)
- `test_region_groups_has_10_zones`: assert len(REGION_GROUPS) == 10
- `test_all_routed_regions_in_region_groups`: every possible _route output region is in REGION_GROUPS keys

**Acceptance**:
- 14 new tests pass
- Compile passes
- `console --pipeline` smoke test runs without error and renders 10 sections (some may be empty since LLM not called)
- No existing test fails

**Commit message**:
```
feat(classifier): atomic routing matrix + 10-zone REGION_GROUPS (Task 6/11)

Critical atomic change per Codex review: introduces _route() function
returning (region_key, reason_code) AND expands REGION_GROUPS from 7 to
10 zones in the SAME commit. Splitting these would create an interim
state where routing returns region keys not in REGION_GROUPS (render
crashes) or region keys exist with no articles (empty sections).

New regions: 🧠 AI/前沿, 📱 消费科技 (TECH split), 📈 市场/宏观,
🏢 公司/产业 (FINANCE split), 🌐 社会观察 (SOCIETY new for Q4).
Display order per spec F3: AI → Markets → POLITICS → CHINA → Corp →
Consumer → ASIA-PAC → CANADA → ECONOMIST → SOCIETY.

Routing function uses early-return on each match (function form, not
IF-stack). Geo-priority for canada/asia_other/china+(society/business/
consumer_tech); topic-priority for everything else.

Spec: 2026-04-19-classification-redesign.md §4.5, §4.6
```

---

## §7 Task 7 — digest-tuning.json + AR Rule 3 update

**Goal**: New region_quotas covering 10 zones. max_total/target → 150. AR Rule 3 lock list updated.

**Files touched**:
- `digest-tuning.json` (region_quotas overhaul + max_total/target)
- `autoresearch/program.md` (Rule 3 — extend lock to new keys + new max value)

**Changes**:

### digest-tuning.json
```json
{
  "dedup_similarity_threshold": 0.70,
  "max_total_articles": 150,
  "target_article_count": 150,
  "freshness_weight": 1.0,
  "source_tiers": { ...unchanged... },
  "tier_boost":   { ...unchanged... },
  "region_quotas": {
    "🧠 AI/前沿 AI FRONTIER":             {"min": 12, "max": 20},
    "📈 市场/宏观 MACRO & MARKETS":         {"min": 12, "max": 20},
    "🏛 全球政治 GLOBAL POLITICS":          {"min": 14, "max": 22},
    "🇨🇳 中国要闻 CHINA":                  {"min": 14, "max": 22},
    "🏢 公司/产业 CORPORATE & INDUSTRY":   {"min": 10, "max": 16},
    "📱 消费科技 CONSUMER TECH":            {"min":  6, "max": 10},
    "🌏 亚太要闻 ASIA-PACIFIC":             {"min":  8, "max": 14},
    "🇨🇦 加拿大 CANADA":                  {"min":  6, "max": 12},
    "📕 经济学人 THE ECONOMIST":            {"min":  4, "max": 10},
    "🌐 社会观察 SOCIETY":                  {"min":  3, "max":  8}
  }
}
```
sum_max = 154 (≥ 150 max_total → max_total binds), sum_min = 89.

### program.md Rule 3 update
```
3. **NEVER edit** these keys in `digest-tuning.json` (user-set policy):
   - `max_total_articles` — locked at 150
   - `target_article_count` — locked at 150 (must match max_total)
   - `region_quotas` — locked at current 10-zone shape (sum_max ≈ 154)
   You may still tune `freshness_weight`, `dedup_similarity_threshold`,
   `tier_boost`, and `source_tiers`.
```

**Tests**:
- `test_digest_tuning_sum_bounds`: load JSON, sum_max ≤ 200, sum_min ≥ 50, max_total in [100, 200]
- `test_digest_tuning_all_regions_in_REGION_GROUPS`: every region_quotas key matches REGION_GROUPS keys (catches typos)

**Acceptance**:
- JSON validates
- Both tests pass
- `~/.openclaw/workspace/evaluate_digest.py` runs successfully against new config (will produce a new baseline quality, recorded for AR)

**Commit message**:
```
config(digest): 10-zone region_quotas + max_total 120→150 (Task 7/11)

Quotas distributed per spec §4.6: high-density regions (CHINA, POLITICS,
AI/前沿, Markets) get 14-22 max; medium (Corp, ASIA-PAC) 8-16; sparse
(SOCIETY, CONSUMER_TECH, ECONOMIST, CANADA) 3-12. sum_max=154 keeps
max_total=150 as the binding cap.

AR program.md Rule 3 updated: lock list includes new max value (150);
AR can still tune freshness_weight, dedup, tier_boost, source_tiers.

Spec: 2026-04-19-classification-redesign.md §4.6, F1
```

---

## §8 Task 8 — reason_code logging + send-log stats

**Goal**: Each article carries `reason_code` through pipeline. Send log gets per-stage distribution stats (the monitoring hook for §9 alert).

**Files touched**:
- `unified-global-news-sender.py:_collect_region_articles` (carry reason_code on article tuple)
- `unified-global-news-sender.py:run` or end-of-classify (print per-stage counts)

**Changes**:
- Article tuple grows from `(title, url, src, pub_dt, orig_title)` to `(title, url, src, pub_dt, orig_title, reason_code)`
  - All downstream consumers tolerate extra trailing element via `art[5] if len(art) > 5 else None`
- After classify_articles completes, print:
  ```
  📊 Routing distribution:
     Stage 1 (hard lock):   24 (12.0%)
     Stage 2 (soft lock):   62 (31.0%)
     Stage 2 (escape):       8  (4.0%)
     Stage 3 (geo keyword): 18  (9.0%)
     Stage 4 (LLM):         85 (42.5%)
     Fallback:               3  (1.5%)
     Total: 200 articles
  ```
- These stats already grep-able from existing log path

**Tests**:
- `test_article_tuple_has_reason_code`: collect → article[5] ∈ REASON_PREFIXES
- `test_routing_stats_format`: stats string matches expected pattern
- `test_routing_stats_sums_to_total`: each stage count sums to total

**Acceptance**:
- 3 new tests pass
- Manual: run console mode, see "📊 Routing distribution" block with non-zero counts

**Commit message**:
```
feat(classifier): reason_code through pipeline + per-stage stats log (Task 8/11)

Each article carries reason_code (set by Stage 1/2/3/4 or fallback)
through the rendering pipeline. After classification, print routing
distribution stats so we can monitor Stage 4 (LLM) rate and alert if
deterministic stages fail to catch ≥30% of articles.

Spec: 2026-04-19-classification-redesign.md §6 Task 8, §9 monitoring
```

---

## §9 Task 9 — docs.sinostor.com.cn news section update

**Goal**: Section 9 of `~/docs-site/pages/autoresearch.html` reflects the new 10-zone structure.

**Files touched**:
- `~/docs-site/pages/autoresearch.html` (section 9 starting at line ~614)
- Publish via `PAGE_LIST=autoresearch.html bash ~/docs-site/scripts/publish.sh`

**Changes**:
- Region count: 7 → 10
- Add 3 new region cards: 📈 市场/宏观, 🏢 公司/产业, 🌐 社会观察 (and TECH split visualization)
- Update "唯一可调旋钮" current values: max_total_articles 120 → 150; target_article_count 120 → 150
- Update density description (target 120 → 150)
- Add note: "分类系统重构 2026-04-19，4 阶段漏斗 + topic/geo/subtopic 三标签"
- Section timestamp → 2026-04-19 (or whatever day Task 9 lands)
- pill remains PLATEAU until AR re-stabilizes after redesign

**Tests**: manual visual verification on https://docs.sinostor.com.cn/autoresearch.html#app-news

**Acceptance**:
- Page loads, 10 zones visible
- All numbers in §9 match digest-tuning.json values
- No broken HTML (verify with `wget -O /dev/null https://docs.sinostor.com.cn/autoresearch.html`)

**Commit message**:
```
docs(autoresearch): reflect 10-zone classification redesign (Task 9/11)

Section 9 of autoresearch.html updated: region count 7→10 (TECH split,
FINANCE split, +SOCIETY); max_total 120→150; target 120→150; density
target updated. Note added about 2026-04-19 4-stage funnel + 3-label
classification redesign.

Spec: 2026-04-19-classification-redesign.md §6 Task 9
```

---

## §10 Task 10 — Dry-run on 5 fixtures + before/after diff

**Goal**: Run new classifier on 5 recent fixtures (mocked LLM with deterministic dict) and produce a diff vs current classification.

**Files touched**:
- `~/global-news/scripts/dry_run_classifier.py` (NEW one-shot script)

**Changes**:
- Script loads 5 most recent fixtures (`2026-04-18-08`, `-16`, `2026-04-19-00`, `-08`, `-16` — adjust based on availability)
- For each fixture article:
  - Run new pipeline (Stages 1-4) — mocked LLM (e.g. fixed dict mapping known titles → labels, or call real LLM if `--real-llm` flag passed)
  - Record (region_old, region_new, reason_code) per article
- Output:
  - Per-region article count diff: `OLD_REGION → NEW_REGION` matrix
  - Top movers: which articles changed region and why (reason_code)
  - Acceptance criteria check:
    - `中国财经要闻` articles in old GLOBAL FINANCE → expect ≤2 in new MACRO/MARKETS
    - At least 1-2 articles from non-CBC/Globe sources reach new CANADA
    - 消费科技 region has ≥4 articles per fetch (else flag as concern)

**Test**: the script itself acts as the integration test. User reviews output.

**Acceptance**:
- Script runs end-to-end without crash
- Produces structured diff output
- User reviews top movers and confirms or asks for tuning before Task 11

**Commit message**:
```
test(classifier): dry-run script for 5-fixture before/after diff (Task 10/11)

One-shot script that runs the new 4-stage funnel against 5 recent
fixtures and prints per-region count diff + top movers (article-level)
with reason_code. Validates the spec's §8 acceptance criteria:
- 中国财经要闻 in MARKETS drops from 7 → ≤2
- CANADA gets ≥1-2 articles from non-Canadian sources
- 消费科技 ≥4 articles per fetch

Mock LLM by default; --real-llm flag for production validation.

Spec: 2026-04-19-classification-redesign.md §6 Task 10, §8 Test Plan
```

---

## §11 Task 11 — Production deploy + monitoring + Option B kill switch

**Goal**: Land all prior tasks on main, watch the next 3 cron sends, install Option B kill switch as emergency-only safety belt.

**Files touched**:
- `unified-global-news-sender.py` (top-of-classify_articles env-var check + `_classify_articles_v1_legacy` stub holding backup of pre-Task-3 logic)
- `~/global-news/global-news-cron-wrapper.sh` (export `NEWS_CLASSIFIER_VERSION=v2`)
- `~/global-news/scripts/wrapper-autoresearch-news.sh` (similar export, so AR sees same version)

**Changes**:
- Add `_classify_articles_v1_legacy` method holding the pre-redesign `classify_articles` body as-is (preserved at Task 3 start; resurrected here)
- Top of `classify_articles`:
  ```python
  if os.environ.get("NEWS_CLASSIFIER_VERSION", "v2") == "v1":
      # Emergency rollback per spec §9 Option B (use only when atomic
      # git revert is impractical). This path will be REMOVED 2 weeks
      # after stable production — see calendar reminder.
      return self._classify_articles_v1_legacy()
  ```
- cron wrapper: `export NEWS_CLASSIFIER_VERSION=v2` near other env exports
- Calendar reminder: file `~/global-news/.kill-switch-sunset` containing date `2026-05-03` (2 weeks out); add a check in cron wrapper that emails warning if file exists and date passed (sunset trigger)

**Monitoring**:
- After deploy, watch the next 3 cron sends
- For each: tail `~/.openclaw/workspace/logs/news-sender-YYYYMMDD.log` for "📊 Routing distribution"
- Verify:
  - Stage 1+2+3 catch ≥30% of articles
  - Stage 4 ≤70%
  - No "fallback" rate > 5%
  - No region renders empty 3+ times in a row

**Tests**:
- `test_kill_switch_v2_default`: env unset → uses new pipeline
- `test_kill_switch_v1_falls_back`: env=v1 → calls _classify_articles_v1_legacy (mock)

**Acceptance**:
- Both tests pass
- 1 manual email sent to chengli1986@gmail.com only with `[DEV]` prefix and reason_code annotations (ahead of next cron)
- User reviews; if OK, normal cron continues
- 3 subsequent cron sends monitored — alert any anomaly

**Commit message**:
```
deploy(classifier): production rollout + Option B kill switch (Task 11/11)

Final task: lands the redesign in production. Adds NEWS_CLASSIFIER_VERSION
env-var kill switch per spec §9 Option B (emergency-only, all-or-nothing,
must follow with Option A revert same-day, sunsets 2026-05-03).

cron wrappers (global-news + autoresearch) export NEWS_CLASSIFIER_VERSION=v2
explicitly so the env is deterministic across both regular sends and AR.

Sunset reminder file `.kill-switch-sunset` triggers email warning if the
v1 code path remains past 2026-05-03.

Spec: 2026-04-19-classification-redesign.md §6 Task 11, §9 Option B
```

---

## §12 Execution Protocol

After each task commit + push:

1. **I run** locally:
   - `python3 -m py_compile unified-global-news-sender.py`
   - `pytest tests/test_classification.py -v` (new tests for this task)
   - `pytest tests/test_unified_sender.py tests/test_digest_pipeline.py -q` (regression check)
2. **I show you**:
   - The commit hash + git diff stat
   - Test output (pass/fail counts, any new warnings)
   - For Task 6 specifically: full _route function + 14-test output
   - For Task 10 specifically: full dry-run diff report
3. **You respond** with one of:
   - "next" / "继续" → I start the next task
   - "stop" / "等等" → I pause, you tell me what to revisit
   - Any specific feedback → I address before continuing
4. **At Task 11 conclusion**: I send 1 [DEV] email, you review, confirm rollout, normal cron picks up

**Estimated total time**: 11 tasks × ~10 min each = ~2 hours of focused work. Can split across days at any task boundary.

---

## §13 Out of Scope (deferred to spec §10 follow-ups)

- POLITICS sub-split (war + elections + diplomacy too coarse — revisit after 2 weeks)
- CHINA internal sub-headers (财经/科技/社会 within zone)
- F2 reconsideration (topic_boost mechanism if 消费科技 overflows)
- AR exploration of region_quotas inner shape after Rule 3 lock
- Reason_code analytics dashboard panel on docs.sinostor

---

**Plan status**: Drafted, ready to execute on user "go".
