# RSS Source Discovery Pipeline — Design Spec

**Date**: 2026-04-06
**Status**: Draft
**Repo**: global-news

## Goal

Build an automated RSS source discovery system that uses Claude Code's AI + web search capabilities to find, validate, score, and recommend high-quality RSS feeds across 8 categories in English and Chinese. Modeled after GMIA's candidate fund discovery pipeline.

## Architecture

```
Stage 1: Discover  →  Stage 2: Validate  →  Stage 3: Score  →  Stage 4: Report
(dual-channel)        (HTTP fetch+parse)     (5-dim scoring)    (email + candidates file)
```

All 4 stages run inside a single Claude Code session triggered by cron daily.

## Discovery Dual-Channel

### Channel A: AI Web Search
For each of the 8 categories, Claude performs 1-2 targeted web searches to find RSS feed URLs. Example queries:
- "best RSS feeds for global finance news 2025 2026"
- "高质量中文科技新闻 RSS feed 推荐"
- "reliable healthcare biotech RSS feeds"

Claude extracts candidate feed URLs from search results, blog posts, and recommendation lists.

### Channel B: Curated Directory Crawl
Scrape known RSS aggregation sources for candidate feeds:
- GitHub awesome-rss-feeds repos
- feedspot.com category rankings
- AllTop, Feedly trending collections
- Chinese RSS aggregation sites (RSSHub docs, Chinese indie blog lists)

## Categories (8)

| Category | Language Focus | Example Existing Sources |
|----------|---------------|------------------------|
| 全球财经 | EN + CN | Bloomberg, FT, CNBC, 界面新闻 |
| 科技/AI | EN + CN | TechCrunch, Ars Technica, 36氪, IT之家 |
| 中国深度新闻 | CN | 南方周末, 虎嗅, 钛媒体, 少数派 |
| 香港/东南亚 | EN + CN | SCMP, HKFP, CNA, Straits Times, RTHK |
| 欧洲时政 | EN | BBC World, Guardian, DW, Reuters |
| 北美时政 | EN | NYT, Globe & Mail, CBC |
| 医药医疗前沿 | EN + CN | STAT News, Endpoints News, Fierce Pharma |
| 专题/垂直 | EN + CN | Economist, Solidot, The Verge, 纽约时报中文 |

## Validation (Stage 2)

For each candidate URL, perform HTTP fetch + XML parse:

1. **HTTP reachability**: GET with 15s timeout, follow redirects
2. **Parse success**: Valid RSS 2.0 / Atom XML structure
3. **Article count**: Number of `<item>` or `<entry>` elements
4. **Freshness**: Parse `pubDate` / `published`, compute newest article age
5. **Encoding**: UTF-8 decode success

Reject candidates that fail HTTP (non-200), fail XML parse, or return 0 articles.

Reuse parsing logic from `rss-health-check.py` (same `check_source()` pattern).

## Scoring Model (Stage 3)

5 dimensions, each 0.0–1.0, weighted sum → `final_score`:

### Dimensions

| Dimension | Weight | How Measured |
|-----------|--------|-------------|
| **reliability** | 0.25 | HTTP 200 + valid parse + article count > 0. Full marks for clean fetch; deduct for redirects, slow response, encoding issues. |
| **freshness** | 0.20 | Newest article age. 1.0 = within 6h, 0.5 = within 48h, 0.0 = older than 7 days. |
| **content_quality** | 0.20 | Articles have description/content fields, author tags, category tags. Pure title-only feeds score low. |
| **authority** | 0.20 | AI judgment: is the domain a recognized media outlet, institutional publisher, or established blog? Known domains score higher. |
| **uniqueness** | 0.15 | Content overlap with existing 39 sources. AI compares recent headlines against current feed pool. Lower overlap = higher score. |

### Weights File: `config/rss-scorer-weights.json`
```json
{
  "reliability": 0.25,
  "freshness": 0.20,
  "content_quality": 0.20,
  "authority": 0.20,
  "uniqueness": 0.15
}
```

Weights are tunable via autoresearch (future).

### Score Thresholds
- `SCORE_THRESHOLD = 0.60` — minimum to appear in report
- `SCORE_EXCELLENT = 0.80` — highlighted as strong recommendation

## Data Structure

### `config/discovered-rss.json` — Candidate Registry
```json
{
  "version": 1,
  "last_discovery": "2026-04-07T02:00+08:00",
  "candidates": [
    {
      "name": "Caixin Global",
      "url": "https://www.caixinglobal.com/rss.xml",
      "language": "en",
      "category": "全球财经",
      "status": "scored",
      "discovered_at": "2026-04-07T02:00+08:00",
      "discovered_via": "ai_search",
      "scores": {
        "reliability": 0.95,
        "freshness": 0.88,
        "content_quality": 0.82,
        "authority": 0.90,
        "uniqueness": 0.75,
        "final": 0.86
      },
      "validation": {
        "http_status": 200,
        "parse_ok": true,
        "article_count": 25,
        "newest_age_hours": 2.5,
        "has_descriptions": true,
        "has_authors": true
      },
      "promoted": false,
      "rejected": false,
      "reject_reason": null
    }
  ]
}
```

### Status Flow
```
discovered → validated → scored → (promoted | rejected)
```
- `promoted: true` — added to `news-sources-config.json` by user
- `rejected: true` — user declined or feed degraded; not re-recommended

## Deduplication

1. **URL dedup**: Normalize URL (strip trailing slash, lowercase domain), match against existing 39 sources and all prior candidates
2. **Domain dedup**: Same domain as existing source → flag but don't auto-reject (different feeds from same publisher can be valuable, e.g., Bloomberg markets vs politics)
3. **Rejected skip**: `rejected: true` candidates are not re-recommended

## Report (Stage 4)

### Daily Email Report
HTML email sent after each discovery run:
- Subject: `[RSS Discovery] YYYY-MM-DD: N new candidates found`
- Body sections:
  - **Top candidates** (final_score ≥ 0.60, sorted by score, max 15)
  - Per candidate: name, URL, category, language, score breakdown, article count, newest age
  - **Excellent picks** (≥ 0.80) highlighted with green badge
  - **Category coverage summary**: how many candidates per category
  - **Existing pool health**: current 39 sources status (from latest health check)
- Footer: "Reply to approve candidates, or ignore to skip"

### Promotion Flow (Semi-automatic)
User reviews email → decides which candidates to add → runs:
```bash
python3 rss-promote-candidate.py --name "Caixin Global" --limit 3
```
This script:
1. Reads candidate from `discovered-rss.json`
2. Adds entry to `news-sources-config.json` with appropriate `limit` and `max_age_hours`
3. Sets `promoted: true` in candidates file
4. Commits change to git

## File Layout

```
global-news/
├── scripts/
│   └── rss-source-discovery.sh       # Cron wrapper → Claude Code session
├── rss-source-discovery.py            # Main pipeline (4 stages)
├── rss-promote-candidate.py           # Semi-auto promotion tool
├── config/
│   ├── discovered-rss.json            # Candidate registry (persistent)
│   ├── rss-scorer-weights.json        # Scoring weights
│   └── rss-discovery-categories.json  # Category definitions + search queries
├── autoresearch/
│   ├── program.md                     # (existing) digest tuning
│   └── rss-discovery-results.tsv      # Discovery run metrics (future)
└── docs/superpowers/specs/
    └── 2026-04-06-rss-source-discovery-design.md  # This file
```

## Implementation Approach

### Option: Claude Code Session (Chosen)

The discovery script `rss-source-discovery.sh` triggers a Claude Code CLI session via `claude -p`. The prompt instructs Claude to:

1. Read current sources from `news-sources-config.json`
2. Read prior candidates from `config/discovered-rss.json`
3. For each of 8 categories: web search for RSS feeds, extract URLs
4. Scrape 3-5 curated directories for additional candidates
5. Validate each candidate (HTTP fetch + parse) via bash/python
6. Score each candidate (5 dimensions)
7. Write results to `config/discovered-rss.json`
8. Generate and send HTML email report

**Why this approach**:
- Zero additional API cost (Claude Code Max plan)
- Built-in web search capability
- Strong judgment for authority and uniqueness scoring
- Consistent with GMIA autoresearch pattern

### Python Helper: `rss-source-discovery.py`

Despite Claude driving the session, a Python helper handles the mechanical parts:
- HTTP validation (parallel, with timeouts)
- XML parsing and article extraction
- Score computation from raw metrics
- JSON read/write with atomic updates
- HTML email generation
- Dedup logic against existing sources

Claude calls this helper during the session rather than doing everything inline.

## Cron Schedule

```
# RSS source discovery — daily 04:15 BJT (20:15 UTC)
15 20 * * *  ~/cron-wrapper.sh rss-discovery 2400 ~/global-news/scripts/rss-source-discovery.sh
```

- Daily at 04:15 BJT, 40min timeout
- After RSS health check (23:55) and before morning news send (08:00)
- Wrapped by `cron-wrapper.sh` for timeout/lock/alert/JSONL logging

## Constraints

- No new API keys required — uses Claude Code built-in capabilities
- No pip dependencies beyond stdlib (consistent with global-news design)
- Atomic file writes (temp + `os.replace()`)
- Does NOT auto-modify `news-sources-config.json` — semi-automatic promotion only
- Respects EC2 network constraints (rsshub.app returns 403 → use rsshub.rssforever.com)

## Success Criteria

1. Daily run discovers 5-15 new candidate feeds across 8 categories
2. Scoring correctly ranks known high-quality feeds (BBC, Reuters, FT) above low-quality ones
3. Dedup prevents recommending feeds already in the pool or previously rejected
4. Email report is actionable — user can decide in <2 minutes which candidates to promote
5. Promoted feeds pass subsequent RSS health checks (reliability ≥ 0.8)
