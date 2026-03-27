# Global News Autoresearch: Digest Quality Optimization

## Goal

Build an autoresearch loop that automatically optimizes the global news digest by tuning deduplication, ranking, region quotas, and source parameters — improving the signal-to-noise ratio of the 3x daily news emails.

## Architecture

```
news-sources-config.json (33 sources — existing, read-only for autoresearch)
        +
digest-tuning.json (autoresearch edits THIS file only)
        ↓
unified-global-news-sender.py (loads both configs, applies dedup+rank+quota)
        ↓
evaluate_digest.py --fixture fixtures/YYYY-MM-DD.json
        ↓ replays cached RSS snapshot through current tuning params
        ↓ outputs: quality: 0.XXXX
        ↓
program.md (autoresearch loop: edit tuning → evaluate → keep/discard)
```

## Phase B Scope (Content Quality)

Phase B adds dedup, ranking, and quality scoring to the existing sender.
Phase A (source selection — add/remove/swap sources) is deferred.

---

## 1. digest-tuning.json — The ONE File Autoresearch Edits

```json
{
  "dedup_similarity_threshold": 0.55,
  "dedup_method": "jaccard_bigram",
  "max_total_articles": 60,
  "target_article_count": 60,
  "freshness_weight": 0.30,
  "tier_boost": {
    "premium": 1.5,
    "standard": 1.0,
    "commodity": 0.8
  },
  "source_tiers": {
    "premium": ["economist", "ft", "nytimes", "reuters"],
    "standard": ["bbc_world", "bbc_tech", "cnbc", "techcrunch", "36kr", "guardian"],
    "commodity": ["sina_finance", "sina_military"]
  },
  "region_quotas": {
    "China":      {"min": 5, "max": 18},
    "US":         {"min": 4, "max": 14},
    "Tech":       {"min": 4, "max": 14},
    "Europe":     {"min": 3, "max": 12},
    "Asia":       {"min": 2, "max": 10},
    "Middle East":{"min": 1, "max": 8},
    "Business":   {"min": 3, "max": 12},
    "General":    {"min": 2, "max": 8}
  }
}
```

**Constraints for autoresearch:**
- `dedup_similarity_threshold`: range [0.3, 0.9] — lower = more aggressive dedup
- `max_total_articles`: range [30, 100]
- `target_article_count`: should equal or be close to max_total_articles
- `freshness_weight`: range [0.1, 0.5]
- `tier_boost` values: range [0.5, 3.0]
- Region quota min: >= 1, max: <= 25, min < max
- Source tier assignments: can be reshuffled but all 33 sources must appear exactly once

---

## 2. Deduplication Algorithm

**Method: Jaccard similarity on character bigrams**

Chosen over TF-IDF cosine because news titles are short (10-30 chars), and Jaccard on bigrams is simpler, faster, and more stable for short text.

```python
def bigrams(text: str) -> set[str]:
    """Extract character bigrams. Works for both Chinese and English."""
    text = text.lower().strip()
    return {text[i:i+2] for i in range(len(text) - 1)} if len(text) >= 2 else {text}

def jaccard_similarity(a: str, b: str) -> float:
    bg_a, bg_b = bigrams(a), bigrams(b)
    if not bg_a or not bg_b:
        return 0.0
    return len(bg_a & bg_b) / len(bg_a | bg_b)

def deduplicate(articles: list[dict], threshold: float) -> list[dict]:
    """Remove near-duplicate articles, keeping the fresher one."""
    kept = []
    for article in sorted(articles, key=lambda a: a['pub_dt'], reverse=True):
        if not any(jaccard_similarity(article['title'], k['title']) > threshold for k in kept):
            kept.append(article)
    return kept
```

**Why Jaccard bigram:**
- No external dependencies (no jieba, no sklearn)
- Character bigrams handle Chinese natively (no word segmentation needed)
- O(n^2) on article count (~130 articles) is fast enough (<100ms)
- Threshold 0.55 catches near-duplicates while preserving articles on same topic with different angles

---

## 3. Ranking Algorithm

Each article gets a rank_score, used to select top articles per region:

```python
def rank_score(article, tuning, region_fill):
    hours_old = (now - article['pub_dt']).total_seconds() / 3600
    freshness = max(0, 1 - hours_old / 72)  # linear decay over 72h

    source_name = article['source']
    tier = 'commodity'  # default
    for t, sources in tuning['source_tiers'].items():
        if source_name in sources:
            tier = t
            break
    boost = tuning['tier_boost'].get(tier, 1.0)

    quota = tuning['region_quotas'].get(article['region'], {"min":2,"max":10})
    current = region_fill.get(article['region'], 0)
    need = max(0, quota['min'] - current) / max(quota['min'], 1)  # 0-1, higher if below min

    return freshness * tuning['freshness_weight'] + boost * 0.4 + need * 0.3
```

**Selection process:**
1. Fetch all articles from all sources
2. Deduplicate (Jaccard bigram)
3. Score each article
4. Greedily fill regions: process articles by score descending; accept if region not at max; stop at `max_total_articles`
5. Post-check: any region below min → force-include its top articles even if over max_total

---

## 4. Evaluation Metrics

```
digest_quality = 0.30 * freshness
              + 0.25 * uniqueness
              + 0.20 * coverage
              + 0.15 * balance
              + 0.10 * density
```

### 4.1 freshness (0-1)
```
freshness = count(articles where age < 6h) / total_articles
```
Measures how "live" the digest is. Higher = more breaking news.

### 4.2 uniqueness (0-1)
```
max_sim = max pairwise Jaccard similarity in output
uniqueness = 1.0 if max_sim <= threshold else (threshold / max_sim)
```
Measures whether dedup worked. 1.0 = no near-dupes in output.

### 4.3 coverage (0-1)
```
coverage = regions_with_at_least_1_article / 8
```
Measures geographic/topic diversity. 1.0 = all 8 regions represented.

### 4.4 balance (0-1)
```
counts = [articles_in_region for each region with articles]
cv = std(counts) / mean(counts)  # coefficient of variation
balance = max(0, 1 - cv)
```
Measures evenness. 1.0 = all regions have equal articles. 0 = one region dominates.

### 4.5 density (0-1)
```
density = 1 - abs(article_count - target) / target
```
Measures whether output hits the target article count. Too many or too few both penalized.

---

## 5. Fixture Snapshot System

**Problem:** Live RSS fetches are non-deterministic. Article content changes every hour. Cannot distinguish "tuning improved" from "news happened to be better today."

**Solution:** Cache RSS fetch results as JSON fixtures. Evaluate replays a fixture.

### 5.1 Snapshot script

Added to the sender: after fetching all sources, save raw results to `fixtures/`:
```python
# In unified-global-news-sender.py, after fetch phase:
snapshot = {
    "date": datetime.utcnow().isoformat(),
    "sources": {
        source_name: [
            {"title": t, "url": u, "pub_dt": d.isoformat()}
            for t, u, d in articles
        ]
        for source_name, articles in all_results.items()
    }
}
snapshot_path = f"fixtures/{date_str}.json"
with open(snapshot_path, "w") as f:
    json.dump(snapshot, f, ensure_ascii=False)
```

### 5.2 Fixture accumulation

- One fixture per day, saved during the 08:15 BJT send (first of 3 daily)
- After 7 days, evaluate uses all 7 fixtures and averages the quality score
- Old fixtures (>30 days) auto-cleaned

### 5.3 Evaluate replay

```bash
python3 evaluate_digest.py                    # use all fixtures
python3 evaluate_digest.py --fixture latest   # use most recent only
python3 evaluate_digest.py --max-fixtures 5   # limit for speed
```

---

## 6. Autoresearch Loop (program.md)

Same pattern as CRA:

1. Read current `digest-tuning.json`
2. Read current baseline quality score
3. Make a targeted change (e.g., adjust dedup threshold, rebalance quotas)
4. `git add -A && git commit -m "experiment: <description>"`
5. Run `python3 evaluate_digest.py`
6. If quality improved → KEEP, log to results.tsv
7. If quality worsened → `git reset --hard HEAD~1`, log as DISCARD
8. Repeat 2-3 experiments per session

**Experiment ideas (ordered):**
1. Tune dedup_similarity_threshold (0.4 → 0.7 range)
2. Adjust region quota min/max ratios
3. Rebalance freshness_weight
4. Promote/demote sources between tiers
5. Adjust max_total_articles
6. Try different tier_boost ratios

---

## 7. File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `~/.openclaw/workspace/digest-tuning.json` | CREATE | All tunable parameters (autoresearch target) |
| `~/.openclaw/workspace/unified-global-news-sender.py` | MODIFY | Load tuning, add dedup + rank + quota logic |
| `~/.openclaw/workspace/evaluate_digest.py` | CREATE | Replay fixtures through tuning params, output quality score |
| `~/.openclaw/workspace/fixtures/` | CREATE (dir) | RSS snapshot cache, 1 per day |
| `~/global-news/autoresearch/program.md` | CREATE | Autoresearch loop instructions |
| `~/global-news/autoresearch/results.tsv` | CREATE | Experiment log |
| `~/global-news/scripts/wrapper-autoresearch-news.sh` | CREATE | Cron wrapper (same pattern as CRA) |

---

## 8. Cron Schedule

| Job | Schedule | Purpose |
|-----|----------|---------|
| Snapshot capture | 08:15 BJT (existing 1st send) | Save RSS fixture during normal send |
| Autoresearch loop | Tue/Thu/Sat 21:00 BJT | 2-3 experiments per run, 20 min timeout |
| Dashboard update | Daily 06:30 BJT (existing tracker) | Update autoresearch.html#app-news |

Autoresearch runs on Tue/Thu/Sat to avoid overlap with CRA autoresearch (Mon/Wed/Fri).

---

## 9. Success Criteria

- **Baseline**: Current sender with zero dedup/ranking → measure quality score
- **Target after 2 weeks**: quality score > baseline + 0.15
- **Concrete improvements**:
  - Article count: 130 → ~60 (remove noise)
  - Zero near-duplicate pairs in output
  - All 8 regions represented in every digest
  - Median article age < 6 hours

---

## 10. What This Does NOT Do (Phase A, deferred)

- Add or remove RSS sources
- Change source URLs or fallback URLs
- Modify RSS health check thresholds
- Add click tracking or user feedback
- Add LLM-based summarization or filtering
