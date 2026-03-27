# Global News Autoresearch Phase B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dedup, ranking, region quotas, fixture snapshots, and a quality evaluation loop to the global news digest — enabling autonomous optimization of `digest-tuning.json`.

**Architecture:** The sender loads `digest-tuning.json` alongside the existing `news-sources-config.json`. After fetching, a new pipeline stage deduplicates (Jaccard bigram), ranks (tier + freshness), and applies soft region quotas. `evaluate_digest.py` replays cached RSS fixtures through this pipeline and outputs a quality score. `program.md` drives the autoresearch loop. A cron wrapper runs it Tue/Thu/Sat.

**Tech Stack:** Python 3.12, bash, no new dependencies (stdlib only)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `~/.openclaw/workspace/digest-tuning.json` | CREATE | All tunable parameters |
| `~/.openclaw/workspace/digest_pipeline.py` | CREATE | Dedup + rank + quota logic (importable module) |
| `~/.openclaw/workspace/unified-global-news-sender.py` | MODIFY | Load tuning, call pipeline, save fixtures |
| `~/.openclaw/workspace/evaluate_digest.py` | CREATE | Replay fixtures, compute quality score |
| `~/.openclaw/workspace/fixtures/` | CREATE (dir) | RSS snapshot cache |
| `~/global-news/autoresearch/program.md` | CREATE | Autoresearch loop instructions |
| `~/global-news/autoresearch/results.tsv` | CREATE | Experiment log |
| `~/global-news/scripts/wrapper-autoresearch-news.sh` | CREATE | Cron wrapper |
| `~/global-news/tests/test_digest_pipeline.py` | CREATE | Unit tests for pipeline |

---

### Task 1: Create digest-tuning.json

**Files:**
- Create: `~/.openclaw/workspace/digest-tuning.json`

- [ ] **Step 1: Write the tuning config**

```json
{
  "dedup_similarity_threshold": 0.55,
  "max_total_articles": 60,
  "target_article_count": 60,
  "freshness_weight": 0.30,
  "source_tiers": {
    "premium": ["FT", "Economist Leaders", "Economist Finance", "Economist Business", "Economist Science", "Bloomberg", "NYT Business", "NYT Technology"],
    "standard": ["BBC World", "BBC Business", "BBC Technology", "CNBC", "TechCrunch", "36氪", "SCMP", "Hacker News", "界面新闻", "纽约时报中文", "BBC中文"],
    "commodity": ["中国科技/AI", "中国财经要闻", "虎嗅", "IT之家", "少数派", "Solidot", "钛媒体", "南方周末", "日经中文", "CNA", "Ars Technica", "The Verge", "CBC Business", "Globe & Mail"]
  },
  "tier_boost": {
    "premium": 1.5,
    "standard": 1.0,
    "commodity": 0.8
  },
  "region_quotas": {
    "AI & 科技前沿 TECH & AI":       {"min": 5, "max": 15},
    "全球财经 GLOBAL FINANCE":        {"min": 4, "max": 12},
    "全球政治 GLOBAL POLITICS":       {"min": 3, "max": 10},
    "中国要闻 CHINA":                 {"min": 3, "max": 10},
    "美国 & 欧洲 US & EUROPE":       {"min": 2, "max": 8},
    "亚太要闻 ASIA-PACIFIC":          {"min": 2, "max": 8},
    "加拿大 CANADA":                  {"min": 2, "max": 6},
    "经济学人 THE ECONOMIST":         {"min": 2, "max": 8}
  }
}
```

- [ ] **Step 2: Verify JSON is valid**

```bash
python3 -c "import json; json.load(open(os.path.expanduser('~/.openclaw/workspace/digest-tuning.json')))" && echo "valid"
```

- [ ] **Step 3: Commit**

```bash
cd ~/global-news && git add -f ~/.openclaw/workspace/digest-tuning.json
git commit -m "feat: add digest-tuning.json for autoresearch"
```

Note: The file lives in ~/.openclaw/workspace/ (symlinked from ~/global-news/). Use `git -C ~/global-news status` to verify it's tracked.

---

### Task 2: Create digest_pipeline.py (dedup + rank + quota)

**Files:**
- Create: `~/.openclaw/workspace/digest_pipeline.py`
- Create: `~/global-news/tests/test_digest_pipeline.py`

- [ ] **Step 1: Write tests**

```python
#!/usr/bin/env python3
"""Tests for digest_pipeline.py"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.openclaw/workspace"))

from datetime import datetime, timezone, timedelta
from digest_pipeline import bigrams, jaccard_similarity, deduplicate, rank_and_select

NOW = datetime(2026, 3, 27, 12, 0, 0, tzinfo=timezone.utc)


def test_bigrams_english():
    assert bigrams("hello") == {"he", "el", "ll", "lo"}


def test_bigrams_chinese():
    result = bigrams("中国新闻")
    assert "中国" in result
    assert "国新" in result
    assert len(result) == 3


def test_jaccard_identical():
    assert jaccard_similarity("breaking news today", "breaking news today") == 1.0


def test_jaccard_similar():
    sim = jaccard_similarity("Tesla stock surges 10%", "Tesla stock surges 12%")
    assert sim > 0.6


def test_jaccard_different():
    sim = jaccard_similarity("Apple launches new iPhone", "Russia Ukraine war update")
    assert sim < 0.2


def test_deduplicate_removes_near_dupes():
    articles = [
        {"title": "Tesla stock surges 10% on earnings", "pub_dt": NOW, "source": "CNBC", "url": "u1", "region": "Tech"},
        {"title": "Tesla stock surges 10% after earnings", "pub_dt": NOW - timedelta(hours=1), "source": "BBC", "url": "u2", "region": "Tech"},
        {"title": "Apple launches new MacBook Pro", "pub_dt": NOW, "source": "TechCrunch", "url": "u3", "region": "Tech"},
    ]
    result = deduplicate(articles, threshold=0.55)
    titles = [a["title"] for a in result]
    assert len(result) == 2
    assert "Tesla stock surges 10% on earnings" in titles  # fresher one kept
    assert "Apple launches new MacBook Pro" in titles


def test_deduplicate_keeps_all_when_different():
    articles = [
        {"title": "中国GDP增长5.2%", "pub_dt": NOW, "source": "A", "url": "u1", "region": "China"},
        {"title": "美联储维持利率不变", "pub_dt": NOW, "source": "B", "url": "u2", "region": "US"},
        {"title": "日本央行加息25bp", "pub_dt": NOW, "source": "C", "url": "u3", "region": "Asia"},
    ]
    result = deduplicate(articles, threshold=0.55)
    assert len(result) == 3


def test_rank_and_select_respects_max():
    tuning = {
        "max_total_articles": 3,
        "freshness_weight": 0.30,
        "source_tiers": {"premium": ["FT"], "standard": ["BBC"], "commodity": ["Sina"]},
        "tier_boost": {"premium": 1.5, "standard": 1.0, "commodity": 0.8},
        "region_quotas": {
            "Tech": {"min": 1, "max": 5},
            "Finance": {"min": 1, "max": 5},
        },
    }
    articles = [
        {"title": f"Article {i}", "pub_dt": NOW - timedelta(hours=i), "source": "FT", "url": f"u{i}", "region": "Tech"}
        for i in range(10)
    ]
    result = rank_and_select(articles, tuning, now=NOW)
    assert len(result) <= 3


def test_rank_and_select_enforces_region_min():
    tuning = {
        "max_total_articles": 10,
        "freshness_weight": 0.30,
        "source_tiers": {"premium": [], "standard": ["A", "B"], "commodity": []},
        "tier_boost": {"premium": 1.5, "standard": 1.0, "commodity": 0.8},
        "region_quotas": {
            "Tech": {"min": 2, "max": 8},
            "Finance": {"min": 2, "max": 8},
        },
    }
    articles = [
        {"title": f"Tech {i}", "pub_dt": NOW, "source": "A", "url": f"t{i}", "region": "Tech"}
        for i in range(8)
    ] + [
        {"title": f"Finance {i}", "pub_dt": NOW - timedelta(hours=5), "source": "B", "url": f"f{i}", "region": "Finance"}
        for i in range(3)
    ]
    result = rank_and_select(articles, tuning, now=NOW)
    finance_count = sum(1 for a in result if a["region"] == "Finance")
    assert finance_count >= 2  # min enforced
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/global-news && python3 -m pytest tests/test_digest_pipeline.py -v
```
Expected: ImportError — `digest_pipeline` module not found

- [ ] **Step 3: Implement digest_pipeline.py**

```python
#!/usr/bin/env python3
"""Deduplication, ranking, and region quota logic for news digest.

This module is imported by unified-global-news-sender.py and evaluate_digest.py.
All functions are pure (no I/O, no side effects) for easy testing.
"""
from datetime import datetime, timezone


def bigrams(text: str) -> set[str]:
    """Extract character bigrams. Works for Chinese and English."""
    text = text.lower().strip()
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i+2] for i in range(len(text) - 1)}


def jaccard_similarity(a: str, b: str) -> float:
    """Jaccard similarity on character bigrams."""
    bg_a, bg_b = bigrams(a), bigrams(b)
    if not bg_a or not bg_b:
        return 0.0
    intersection = len(bg_a & bg_b)
    union = len(bg_a | bg_b)
    return intersection / union if union else 0.0


def deduplicate(articles: list[dict], threshold: float) -> list[dict]:
    """Remove near-duplicate articles by title similarity. Keeps the fresher one."""
    # Sort by pub_dt descending (freshest first)
    sorted_articles = sorted(articles, key=lambda a: a.get("pub_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    kept: list[dict] = []
    for article in sorted_articles:
        title = article.get("title", "")
        if not any(jaccard_similarity(title, k["title"]) > threshold for k in kept):
            kept.append(article)
    return kept


def _get_tier(source_name: str, source_tiers: dict) -> str:
    """Look up which tier a source belongs to."""
    for tier, sources in source_tiers.items():
        if source_name in sources:
            return tier
    return "commodity"


def _rank_score(article: dict, tuning: dict, region_fill: dict, now: datetime) -> float:
    """Compute rank score for a single article."""
    pub_dt = article.get("pub_dt")
    if pub_dt:
        hours_old = max(0, (now - pub_dt).total_seconds() / 3600)
    else:
        hours_old = 72  # unknown age = assume old
    freshness = max(0.0, 1.0 - hours_old / 72.0)

    tier = _get_tier(article.get("source", ""), tuning.get("source_tiers", {}))
    boost = tuning.get("tier_boost", {}).get(tier, 1.0)

    region = article.get("region", "")
    quota = tuning.get("region_quotas", {}).get(region, {"min": 2, "max": 10})
    current = region_fill.get(region, 0)
    need = max(0.0, (quota["min"] - current) / max(quota["min"], 1))

    fw = tuning.get("freshness_weight", 0.3)
    return freshness * fw + boost * 0.4 + need * 0.3


def rank_and_select(articles: list[dict], tuning: dict, now: datetime | None = None) -> list[dict]:
    """Rank articles and select top N respecting region quotas."""
    if now is None:
        now = datetime.now(timezone.utc)

    max_total = tuning.get("max_total_articles", 60)
    region_quotas = tuning.get("region_quotas", {})

    # Phase 1: greedy fill by score
    region_fill: dict[str, int] = {}
    scored = sorted(articles, key=lambda a: _rank_score(a, tuning, region_fill, now), reverse=True)

    selected: list[dict] = []
    for article in scored:
        if len(selected) >= max_total:
            break
        region = article.get("region", "")
        quota = region_quotas.get(region, {"min": 0, "max": 100})
        current = region_fill.get(region, 0)
        if current >= quota["max"]:
            continue
        selected.append(article)
        region_fill[region] = current + 1

    # Phase 2: enforce minimums — force-include top articles from under-served regions
    selected_set = set(id(a) for a in selected)
    for region, quota in region_quotas.items():
        current = region_fill.get(region, 0)
        if current >= quota["min"]:
            continue
        # Find unselected articles for this region
        candidates = [a for a in scored if a.get("region") == region and id(a) not in selected_set]
        needed = quota["min"] - current
        for article in candidates[:needed]:
            selected.append(article)
            selected_set.add(id(article))
            region_fill[region] = region_fill.get(region, 0) + 1

    return selected
```

- [ ] **Step 4: Run tests**

```bash
cd ~/global-news && python3 -m pytest tests/test_digest_pipeline.py -v
```
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/global-news && git add ~/.openclaw/workspace/digest_pipeline.py tests/
git commit -m "feat: digest pipeline — dedup (Jaccard bigram) + rank + region quotas"
```

---

### Task 3: Create evaluate_digest.py

**Files:**
- Create: `~/.openclaw/workspace/evaluate_digest.py`

- [ ] **Step 1: Write the evaluation script**

```python
#!/usr/bin/env python3
"""Evaluate news digest quality by replaying fixtures through the pipeline.

Metric: quality = 0.30*freshness + 0.25*uniqueness + 0.20*coverage + 0.15*balance + 0.10*density

Usage:
    python3 evaluate_digest.py                    # all fixtures
    python3 evaluate_digest.py --fixture latest   # most recent only
    python3 evaluate_digest.py --max-fixtures 5   # limit for speed
"""
import argparse
import json
import os
import glob
import math
from datetime import datetime, timezone, timedelta

WORKSPACE = os.path.expanduser("~/.openclaw/workspace")
TUNING_PATH = os.path.join(WORKSPACE, "digest-tuning.json")
FIXTURE_DIR = os.path.join(WORKSPACE, "fixtures")
REGION_GROUPS_FILE = os.path.join(WORKSPACE, "news-sources-config.json")

# Region mapping: source_name -> region_key (matches digest-tuning.json keys)
# Extracted from REGION_GROUPS in unified-global-news-sender.py
SOURCE_TO_REGION = {
    "中国科技/AI": "AI & 科技前沿 TECH & AI", "虎嗅": "AI & 科技前沿 TECH & AI",
    "IT之家": "AI & 科技前沿 TECH & AI", "少数派": "AI & 科技前沿 TECH & AI",
    "Solidot": "AI & 科技前沿 TECH & AI", "钛媒体": "AI & 科技前沿 TECH & AI",
    "36氪": "AI & 科技前沿 TECH & AI", "TechCrunch": "AI & 科技前沿 TECH & AI",
    "Hacker News": "AI & 科技前沿 TECH & AI", "Ars Technica": "AI & 科技前沿 TECH & AI",
    "The Verge": "AI & 科技前沿 TECH & AI", "BBC Technology": "AI & 科技前沿 TECH & AI",
    "NYT Technology": "AI & 科技前沿 TECH & AI",
    "中国财经要闻": "全球财经 GLOBAL FINANCE", "CNBC": "全球财经 GLOBAL FINANCE",
    "Bloomberg": "全球财经 GLOBAL FINANCE", "BBC Business": "全球财经 GLOBAL FINANCE",
    "FT": "全球财经 GLOBAL FINANCE",
    "纽约时报中文": "全球政治 GLOBAL POLITICS", "BBC中文": "全球政治 GLOBAL POLITICS",
    "BBC World": "全球政治 GLOBAL POLITICS", "SCMP": "全球政治 GLOBAL POLITICS",
    "界面新闻": "中国要闻 CHINA", "南方周末": "中国要闻 CHINA",
    "NYT Business": "美国 & 欧洲 US & EUROPE",
    "日经中文": "亚太要闻 ASIA-PACIFIC", "CNA": "亚太要闻 ASIA-PACIFIC",
    "CBC Business": "加拿大 CANADA", "Globe & Mail": "加拿大 CANADA",
    "Economist Leaders": "经济学人 THE ECONOMIST", "Economist Finance": "经济学人 THE ECONOMIST",
    "Economist Business": "经济学人 THE ECONOMIST", "Economist Science": "经济学人 THE ECONOMIST",
}


def load_tuning() -> dict:
    with open(TUNING_PATH) as f:
        return json.load(f)


def load_fixture(path: str) -> list[dict]:
    """Load a fixture file and return flat list of articles with region tags."""
    with open(path) as f:
        snapshot = json.load(f)

    articles = []
    for source_name, items in snapshot.get("sources", {}).items():
        region = SOURCE_TO_REGION.get(source_name, "")
        for item in items:
            pub_dt = None
            if item.get("pub_dt"):
                try:
                    pub_dt = datetime.fromisoformat(item["pub_dt"])
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pass
            articles.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "source": source_name,
                "region": region,
                "pub_dt": pub_dt,
            })
    return articles


def compute_quality(articles: list[dict], selected: list[dict], tuning: dict) -> dict:
    """Compute the 5-dimension quality score."""
    from digest_pipeline import jaccard_similarity

    total = len(selected)
    if total == 0:
        return {"quality": 0.0, "freshness": 0.0, "uniqueness": 0.0, "coverage": 0.0, "balance": 0.0, "density": 0.0}

    # 1. Freshness: % of articles < 6h old
    now = max((a["pub_dt"] for a in articles if a.get("pub_dt")), default=datetime.now(timezone.utc))
    fresh_count = sum(1 for a in selected if a.get("pub_dt") and (now - a["pub_dt"]).total_seconds() < 6 * 3600)
    freshness = fresh_count / total

    # 2. Uniqueness: max pairwise similarity in output
    threshold = tuning.get("dedup_similarity_threshold", 0.55)
    max_sim = 0.0
    titles = [a["title"] for a in selected]
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            sim = jaccard_similarity(titles[i], titles[j])
            if sim > max_sim:
                max_sim = sim
    uniqueness = 1.0 if max_sim <= threshold else threshold / max_sim

    # 3. Coverage: regions with >= 1 article
    all_regions = set(tuning.get("region_quotas", {}).keys())
    covered = set(a["region"] for a in selected if a["region"])
    coverage = len(covered & all_regions) / max(len(all_regions), 1)

    # 4. Balance: 1 - coefficient of variation
    region_counts = {}
    for a in selected:
        r = a.get("region", "")
        if r:
            region_counts[r] = region_counts.get(r, 0) + 1
    if region_counts:
        counts = list(region_counts.values())
        mean_c = sum(counts) / len(counts)
        std_c = math.sqrt(sum((c - mean_c) ** 2 for c in counts) / len(counts)) if len(counts) > 1 else 0
        cv = std_c / mean_c if mean_c > 0 else 0
        balance = max(0.0, 1.0 - cv)
    else:
        balance = 0.0

    # 5. Density: closeness to target article count
    target = tuning.get("target_article_count", 60)
    density = max(0.0, 1.0 - abs(total - target) / target)

    quality = 0.30 * freshness + 0.25 * uniqueness + 0.20 * coverage + 0.15 * balance + 0.10 * density
    return {
        "quality": round(quality, 4),
        "freshness": round(freshness, 4),
        "uniqueness": round(uniqueness, 4),
        "coverage": round(coverage, 4),
        "balance": round(balance, 4),
        "density": round(density, 4),
        "total_fetched": len(articles),
        "total_selected": total,
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate news digest quality")
    parser.add_argument("--fixture", default=None, help="Specific fixture or 'latest'")
    parser.add_argument("--max-fixtures", type=int, default=0, help="Max fixtures (0=all)")
    args = parser.parse_args()

    tuning = load_tuning()

    # Find fixtures
    if args.fixture == "latest":
        paths = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.json")))
        paths = paths[-1:] if paths else []
    elif args.fixture:
        paths = [args.fixture]
    else:
        paths = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.json")))

    if args.max_fixtures and len(paths) > args.max_fixtures:
        paths = paths[-args.max_fixtures:]

    if not paths:
        print("ERROR: No fixtures found. Run the sender once to create a snapshot.")
        raise SystemExit(1)

    print(f"Tuning: {TUNING_PATH}")
    print(f"Fixtures: {len(paths)}")

    from digest_pipeline import deduplicate, rank_and_select

    all_qualities = []
    for i, fp in enumerate(paths):
        date = os.path.basename(fp).replace(".json", "")
        articles = load_fixture(fp)

        deduped = deduplicate(articles, tuning.get("dedup_similarity_threshold", 0.55))
        selected = rank_and_select(deduped, tuning)
        metrics = compute_quality(articles, selected, tuning)

        print(f"  [{i+1}/{len(paths)}] {date}: {metrics['total_fetched']} fetched → {metrics['total_selected']} selected, quality={metrics['quality']:.4f}")
        all_qualities.append(metrics["quality"])

    avg_quality = sum(all_qualities) / len(all_qualities)

    print(f"\n{'='*50}")
    print(f"RESULTS")
    print(f"{'='*50}")
    print(f"Fixtures evaluated: {len(paths)}")
    print(f"Average quality:    {avg_quality:.4f}")
    print(f"{'='*50}")
    print(f"\nquality: {avg_quality:.4f}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create fixtures directory**

```bash
mkdir -p ~/.openclaw/workspace/fixtures
```

- [ ] **Step 3: Commit**

```bash
cd ~/global-news && git add ~/.openclaw/workspace/evaluate_digest.py
git commit -m "feat: evaluate_digest.py — replay fixtures, 5-dim quality score"
```

---

### Task 4: Add fixture snapshot to unified-global-news-sender.py

**Files:**
- Modify: `~/.openclaw/workspace/unified-global-news-sender.py`

- [ ] **Step 1: Add fixture save after fetch phase**

After `self.fetch_all_news()` completes (around line 679), add snapshot logic. Insert into the `run()` method after the fetch call and before the zero-article check:

```python
# Save RSS snapshot as fixture for autoresearch evaluation
self._save_fixture()
```

Add the `_save_fixture` method to the class:

```python
def _save_fixture(self):
    """Save current fetch results as a fixture for autoresearch evaluation."""
    import json as _json
    fixture_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    os.makedirs(fixture_dir, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fixture_path = os.path.join(fixture_dir, f"{date_str}.json")

    if os.path.exists(fixture_path):
        return  # one fixture per day

    snapshot = {"date": datetime.now(timezone.utc).isoformat(), "sources": {}}
    for source_name, articles in self.news_data.items():
        snapshot["sources"][source_name] = [
            {
                "title": title,
                "url": url,
                "pub_dt": pub_dt.isoformat() if pub_dt else None,
            }
            for title, url, pub_dt in articles
        ]

    try:
        with open(fixture_path, "w") as f:
            _json.dump(snapshot, f, ensure_ascii=False)
    except Exception:
        pass  # non-critical, don't break the sender
```

- [ ] **Step 2: Add dedup/rank pipeline call**

In `generate_html()` method, after building region_articles but before rendering, apply pipeline. Insert the pipeline import at the top of the file and integrate into the flow.

At the top of the file (after existing imports):
```python
# Digest pipeline (dedup + rank + quota)
try:
    from digest_pipeline import deduplicate, rank_and_select
    _HAS_PIPELINE = True
except ImportError:
    _HAS_PIPELINE = False
```

In `generate_html()`, after collecting all articles across regions but before rendering, add a pipeline stage. The simplest integration point: modify the region iteration loop to use the pipeline when available.

Add a method to the class that applies the pipeline:
```python
def _apply_pipeline(self, all_region_articles):
    """Apply dedup + rank + quota if digest-tuning.json exists."""
    if not _HAS_PIPELINE:
        return all_region_articles

    tuning_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digest-tuning.json")
    if not os.path.exists(tuning_path):
        return all_region_articles

    import json as _json
    with open(tuning_path) as f:
        tuning = _json.load(f)

    # Flatten all articles with region tags
    flat = []
    for region_title, articles in all_region_articles:
        # Strip emoji prefix from region title for matching tuning keys
        region_key = re.sub(r'^[^\w]*', '', region_title).strip()
        for title, url, src, pub_dt in articles:
            flat.append({
                "title": title, "url": url, "source": src,
                "pub_dt": pub_dt, "region": region_key,
                "region_title": region_title,
            })

    if not flat:
        return all_region_articles

    deduped = deduplicate(flat, tuning.get("dedup_similarity_threshold", 0.55))
    selected = rank_and_select(deduped, tuning)

    # Rebuild region groups
    rebuilt = {}
    for article in selected:
        rt = article["region_title"]
        if rt not in rebuilt:
            rebuilt[rt] = []
        rebuilt[rt].append((article["title"], article["url"], article["source"], article["pub_dt"]))

    # Preserve original region order
    result = []
    for region_title, _ in all_region_articles:
        if region_title in rebuilt:
            result.append((region_title, rebuilt[region_title]))
    return result
```

- [ ] **Step 3: Test the sender still works**

```bash
cd ~/.openclaw/workspace && python3 unified-global-news-sender.py --mode console 2>&1 | head -20
```
Expected: Console output with news articles, no errors

- [ ] **Step 4: Commit**

```bash
cd ~/global-news && git add ~/.openclaw/workspace/unified-global-news-sender.py
git commit -m "feat: integrate digest pipeline (dedup+rank+quota) + fixture snapshots"
```

---

### Task 5: Create autoresearch program.md and results.tsv

**Files:**
- Create: `~/global-news/autoresearch/program.md`
- Create: `~/global-news/autoresearch/results.tsv`

- [ ] **Step 1: Write program.md**

```markdown
# News Digest Autoresearch: Quality Optimization

## Goal
Maximize the **quality** of the news digest — a composite of freshness, uniqueness,
coverage, balance, and density. Higher quality = better signal-to-noise ratio.

## The ONE file you can edit
`~/.openclaw/workspace/digest-tuning.json` — all tunable parameters.

## The metric
Run: `cd ~/.openclaw/workspace && python3 evaluate_digest.py`
Read the last line of output: `quality: 0.XXXX`
Higher is better.

## Rules
1. **NEVER edit** any file except `digest-tuning.json`
2. **NEVER edit** evaluate_digest.py, digest_pipeline.py, or the sender
3. Before EACH experiment: `cd ~/global-news && git add -A && git commit -m "experiment: <description>"`
4. Run the evaluate command and read the quality score
5. If quality **improved**: keep the commit, log to results.tsv
6. If quality **worsened or stayed the same**: `git reset --hard HEAD~1`
7. Log EVERY experiment to `autoresearch/results.tsv` (even failures)
8. **NEVER STOP** — keep running experiments until told to stop

## results.tsv format
Append one line per experiment (tab-separated):
```
commit_hash	quality	status	description
```

## Experiment ideas (try in this order)
1. Adjust dedup_similarity_threshold (0.4 → 0.7 range)
2. Rebalance region quota min/max
3. Change freshness_weight (0.15 → 0.45)
4. Promote/demote sources between tiers
5. Adjust tier_boost ratios
6. Change max_total_articles (40 → 80)
7. Targeted region quota adjustments based on coverage gaps

## Constraints
- digest-tuning.json must remain valid JSON
- All 33 sources must appear in exactly one tier
- Region quota min must be >= 1, max must be > min
- dedup_similarity_threshold must be in [0.3, 0.9]
- max_total_articles must be in [30, 100]
```

- [ ] **Step 2: Create results.tsv with header**

```
commit_hash	quality	status	description
```

- [ ] **Step 3: Commit**

```bash
cd ~/global-news && mkdir -p autoresearch
git add autoresearch/program.md autoresearch/results.tsv
git commit -m "feat: autoresearch program.md + results.tsv for news digest"
```

---

### Task 6: Create wrapper script and cron entry

**Files:**
- Create: `~/global-news/scripts/wrapper-autoresearch-news.sh`

- [ ] **Step 1: Write the wrapper**

```bash
#!/bin/bash
# Cron wrapper for news digest autoresearch
# Runs 2-3 experiments per session, 20-minute timeout
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

cleanup() {
    local pids
    pids=$(jobs -p 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "$LOG_PREFIX Cleaning up child processes..."
        kill $pids 2>/dev/null || true
        sleep 2
        kill -9 $pids 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "$LOG_PREFIX Starting news autoresearch session..."

unset ANTHROPIC_API_KEY
unset CLAUDECODE

cd "$REPO_DIR"

PROGRAM_MD="$REPO_DIR/autoresearch/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$LOG_PREFIX ERROR: $PROGRAM_MD not found"
    exit 1
fi

PROMPT="$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- You have a MAXIMUM of 20 minutes for this session
- Run 2-3 experiments only, then stop
- After all experiments, if any commits were kept, run: cd ~/global-news && git push
"

timeout --kill-after=30 1200 claude -p --model sonnet "$PROMPT" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    echo "$LOG_PREFIX News autoresearch TIMED OUT after 20 minutes"
elif [ $EXIT_CODE -ne 0 ]; then
    echo "$LOG_PREFIX News autoresearch failed (exit code: $EXIT_CODE)"
else
    echo "$LOG_PREFIX News autoresearch finished successfully"
fi

cd "$REPO_DIR"
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")
LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
if [ -n "$REMOTE_HEAD" ] && [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
    echo "$LOG_PREFIX Pushing new commits..."
    git push 2>&1 || echo "$LOG_PREFIX WARNING: git push failed"
else
    echo "$LOG_PREFIX No new commits to push"
fi
```

- [ ] **Step 2: Make executable and verify**

```bash
chmod +x ~/global-news/scripts/wrapper-autoresearch-news.sh
bash -n ~/global-news/scripts/wrapper-autoresearch-news.sh
```

- [ ] **Step 3: Add cron entry**

```bash
# News autoresearch (Tue/Thu/Sat 21:00 BJT = 13:00 UTC)
0 13 * * 2,4,6 ~/cron-wrapper.sh --name news-autoresearch --timeout 1260 --lock -- /home/ubuntu/global-news/scripts/wrapper-autoresearch-news.sh >> ~/logs/news-autoresearch.log 2>&1
```

```bash
touch ~/logs/news-autoresearch.log
```

- [ ] **Step 4: Commit and push**

```bash
cd ~/global-news && git add scripts/ autoresearch/
git commit -m "feat: autoresearch wrapper + cron for news digest optimization"
git push origin main
```

---

### Task 7: Generate baseline fixture and measure baseline quality

- [ ] **Step 1: Run sender once to generate today's fixture**

```bash
cd ~/.openclaw/workspace && python3 unified-global-news-sender.py --mode console 2>&1 | tail -5
ls -la fixtures/
```
Expected: `fixtures/2026-03-27.json` created

- [ ] **Step 2: Run evaluate to get baseline**

```bash
cd ~/.openclaw/workspace && python3 evaluate_digest.py --fixture latest
```
Expected: Output with quality score (baseline measurement)

- [ ] **Step 3: Log baseline to results.tsv**

Append to `~/global-news/autoresearch/results.tsv`:
```
<commit_hash>	<quality>	BASELINE	initial tuning, first fixture
```

- [ ] **Step 4: Commit baseline**

```bash
cd ~/global-news && git add autoresearch/results.tsv ~/.openclaw/workspace/fixtures/
git commit -m "data: baseline fixture + quality measurement"
git push origin main
```
