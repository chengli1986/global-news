#!/usr/bin/env python3
"""Replay RSS fixture snapshots through the digest pipeline and output a 5-dimension quality score.

Usage:
    python3 evaluate_digest.py --fixture fixtures/2026-03-27.json
    python3 evaluate_digest.py                          # runs all fixtures
    python3 evaluate_digest.py --max-fixtures 5         # runs up to 5 fixtures
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Resolve script directory (works through symlinks)
SCRIPT_DIR = Path(os.path.realpath(__file__)).parent
sys.path.insert(0, str(SCRIPT_DIR))

from digest_pipeline import deduplicate, jaccard_similarity, rank_and_select

# --- Source-to-region mapping (matches REGION_GROUPS in sender) ---

SOURCE_TO_REGION: dict[str, str] = {
    "中国科技/AI": "AI & 科技前沿 TECH & AI", "虎嗅": "AI & 科技前沿 TECH & AI",
    "IT之家": "AI & 科技前沿 TECH & AI", "少数派": "AI & 科技前沿 TECH & AI",
    "Solidot": "AI & 科技前沿 TECH & AI", "钛媒体": "AI & 科技前沿 TECH & AI",
    "36氪": "AI & 科技前沿 TECH & AI", "TechCrunch": "AI & 科技前沿 TECH & AI",
    "Hacker News": "AI & 科技前沿 TECH & AI", "Ars Technica": "AI & 科技前沿 TECH & AI",
    "The Verge": "AI & 科技前沿 TECH & AI", "BBC Technology": "AI & 科技前沿 TECH & AI",
    "NYT Technology": "AI & 科技前沿 TECH & AI",
    "中国财经要闻": "全球财经 GLOBAL FINANCE", "CNBC": "全球财经 GLOBAL FINANCE",
    "Bloomberg": "全球财经 GLOBAL FINANCE", "Bloomberg Econ": "全球财经 GLOBAL FINANCE",
    "BBC Business": "全球财经 GLOBAL FINANCE", "FT": "全球财经 GLOBAL FINANCE",
    "NYT Business": "全球财经 GLOBAL FINANCE",  # matches REGION_GROUPS; "美国&欧洲" quota removed
    "纽约时报中文": "全球政治 GLOBAL POLITICS", "BBC中文": "全球政治 GLOBAL POLITICS",
    "BBC World": "全球政治 GLOBAL POLITICS", "SCMP": "全球政治 GLOBAL POLITICS",
    "Bloomberg Politics": "全球政治 GLOBAL POLITICS",
    "界面新闻": "中国要闻 CHINA", "南方周末": "中国要闻 CHINA",
    "日经中文": "亚太要闻 ASIA-PACIFIC", "CNA": "亚太要闻 ASIA-PACIFIC",
    "RTHK中文": "亚太要闻 ASIA-PACIFIC", "Straits Times": "亚太要闻 ASIA-PACIFIC",
    "HKFP": "亚太要闻 ASIA-PACIFIC", "SCMP Hong Kong": "亚太要闻 ASIA-PACIFIC",
    "CBC Business": "加拿大 CANADA", "Globe & Mail": "加拿大 CANADA",
    "Economist Leaders": "经济学人 THE ECONOMIST", "Economist Finance": "经济学人 THE ECONOMIST",
    "Economist Business": "经济学人 THE ECONOMIST", "Economist Science": "经济学人 THE ECONOMIST",
}

ALL_REGIONS = sorted(set(SOURCE_TO_REGION.values()))
TOTAL_REGIONS = len(ALL_REGIONS)  # 7


# --- Loaders ---

def load_tuning(path: Path | None = None) -> dict:
    """Load digest-tuning.json from script directory or explicit path."""
    if path is None:
        path = SCRIPT_DIR / "digest-tuning.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_dt(s: str | None) -> datetime | None:
    """Parse ISO datetime string to timezone-aware datetime, or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def load_fixture(path: str | Path) -> list[dict]:
    """Parse fixture JSON into flat article list with region tags.

    Fixture format:
        {"date": "ISO", "sources": {"source_name": [{"title": ..., "url": ..., "pub_dt": ...}]}}

    Returns list of dicts with keys: title, url, pub_dt (datetime|None), source, region.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    articles: list[dict] = []
    sources = data.get("sources", {})
    for source_name, items in sources.items():
        region = SOURCE_TO_REGION.get(source_name, "")
        for item in items:
            pub_dt = _parse_dt(item.get("pub_dt"))
            articles.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "pub_dt": pub_dt,
                "source": source_name,
                "region": region,
            })
    return articles


# --- Quality metrics ---

def compute_quality(articles: list[dict], selected: list[dict], tuning: dict) -> dict[str, float]:
    """Compute 5-dimension quality score.

    Args:
        articles: all articles from fixture (with pub_dt, region)
        selected: articles chosen by rank_and_select
        tuning: digest-tuning.json contents

    Returns dict with freshness, uniqueness, coverage, balance, density, quality.
    """
    # Use max pub_dt in fixture as "now" (historical replay)
    all_dts = [a["pub_dt"] for a in articles if a.get("pub_dt")]
    now = max(all_dts) if all_dts else datetime.now(timezone.utc)

    total = len(selected)
    if total == 0:
        return {
            "freshness": 0.0, "uniqueness": 0.0, "coverage": 0.0,
            "balance": 0.0, "density": 0.0, "quality": 0.0,
        }

    # --- Freshness: articles_under_6h / total ---
    six_hours = 6 * 3600
    under_6h = sum(
        1 for a in selected
        if a.get("pub_dt") and (now - a["pub_dt"]).total_seconds() <= six_hours
    )
    freshness = under_6h / total

    # --- Uniqueness: 1.0 if max pairwise sim <= threshold, else threshold/max_sim ---
    threshold = tuning.get("dedup_similarity_threshold", 0.55)
    max_sim = 0.0
    titles = [a.get("title", "") for a in selected]
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            sim = jaccard_similarity(titles[i], titles[j])
            if sim > max_sim:
                max_sim = sim
    uniqueness = 1.0 if max_sim <= threshold else threshold / max_sim

    # --- Coverage: regions_with_articles / total_regions ---
    regions_present = set(a.get("region", "") for a in selected if a.get("region"))
    coverage = len(regions_present) / TOTAL_REGIONS

    # --- Balance: max(0, 1 - CV(articles_per_region)) ---
    region_counts: dict[str, int] = {}
    for a in selected:
        r = a.get("region", "")
        if r:
            region_counts[r] = region_counts.get(r, 0) + 1
    if region_counts:
        counts = list(region_counts.values())
        mean = sum(counts) / len(counts)
        if mean > 0:
            variance = sum((c - mean) ** 2 for c in counts) / len(counts)
            cv = math.sqrt(variance) / mean
        else:
            cv = 0.0
    else:
        cv = 0.0
    balance = max(0.0, 1.0 - cv)

    # --- Density: max(0, 1 - abs(count - target) / target) ---
    target = tuning.get("target_article_count", tuning.get("max_total_articles", 60))
    density = max(0.0, 1.0 - abs(total - target) / target) if target > 0 else 0.0

    # --- Weighted quality ---
    quality = (
        0.30 * freshness
        + 0.25 * uniqueness
        + 0.20 * coverage
        + 0.15 * balance
        + 0.10 * density
    )

    return {
        "freshness": round(freshness, 4),
        "uniqueness": round(uniqueness, 4),
        "coverage": round(coverage, 4),
        "balance": round(balance, 4),
        "density": round(density, 4),
        "quality": round(quality, 4),
    }


# --- Main ---

def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate digest quality from RSS fixture snapshots")
    parser.add_argument("--fixture", type=str, help="Path to a single fixture JSON file")
    parser.add_argument("--max-fixtures", type=int, default=0, help="Max number of fixtures to evaluate (0 = all)")
    args = parser.parse_args()

    tuning = load_tuning()
    threshold = tuning.get("dedup_similarity_threshold", 0.55)

    # Collect fixture paths
    if args.fixture == "latest":
        fixtures_dir = SCRIPT_DIR / "fixtures"
        fixture_paths = sorted(fixtures_dir.glob("*.json"))[-1:] if fixtures_dir.is_dir() else []
    elif args.fixture:
        fixture_paths = [Path(args.fixture)]
    else:
        fixtures_dir = SCRIPT_DIR / "fixtures"
        if not fixtures_dir.is_dir():
            print(f"No fixtures directory at {fixtures_dir}", file=sys.stderr)
            sys.exit(1)
        fixture_paths = sorted(fixtures_dir.glob("*.json"))
        if not fixture_paths:
            print(f"No fixture files found in {fixtures_dir}", file=sys.stderr)
            sys.exit(1)

    if args.max_fixtures > 0:
        fixture_paths = fixture_paths[:args.max_fixtures]

    all_scores: list[float] = []

    for fp in fixture_paths:
        print(f"\n--- {fp.name} ---")
        articles = load_fixture(fp)
        print(f"  articles loaded: {len(articles)}")

        # Run pipeline: deduplicate then rank_and_select
        deduped = deduplicate(articles, threshold)
        print(f"  after dedup:     {len(deduped)}")

        # Use max pub_dt as "now" for rank_and_select
        all_dts = [a["pub_dt"] for a in articles if a.get("pub_dt")]
        now = max(all_dts) if all_dts else datetime.now(timezone.utc)

        selected = rank_and_select(deduped, tuning, now=now)
        print(f"  selected:        {len(selected)}")

        scores = compute_quality(articles, selected, tuning)
        for dim, val in scores.items():
            print(f"  {dim:12s}: {val:.4f}")

        all_scores.append(scores["quality"])

    # Summary
    if len(all_scores) > 1:
        avg = sum(all_scores) / len(all_scores)
        print(f"\n=== Average over {len(all_scores)} fixtures ===")
        print(f"quality: {avg:.4f}")
    elif all_scores:
        print(f"\nquality: {all_scores[0]:.4f}")


if __name__ == "__main__":
    main()
