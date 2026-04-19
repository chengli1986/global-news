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

# --- Source-to-region mapping: simulates the production funnel's POST-Stage-2 view ---
# Matches the EFFECTIVE region after Stage 1 (hard lock) + Stage 2 (soft lock,
# non-escape) routing. The 9 Chinese soft-lock sources (中国科技/AI, 36氪, 虎嗅,
# 钛媒体, IT之家, 少数派) are mapped to CHINA here even though their source-
# default REGION_GROUPS region is AI/前沿 — because in production, Stage 2 always
# routes them to CHINA when the title has no external geo escape signal.
#
# This means evaluator quality scores reflect production routing for ~95% of
# articles (Stage 2 escape and Stage 3 geo-keyword routing are NOT simulated).
# CONSUMER_TECH and SOCIETY zones are LLM-fed only — no source defaults exist.

SOURCE_TO_REGION: dict[str, str] = {
    # 中国要闻 CHINA — 3 source-default + 9 Stage-2 soft-locked Chinese sources
    "界面新闻":      "中国要闻 CHINA",
    "南方周末":      "中国要闻 CHINA",
    "中国财经要闻":   "中国要闻 CHINA",
    "中国科技/AI":   "中国要闻 CHINA",   # soft-locked (REGION_GROUPS default: AI/前沿)
    "36氪":          "中国要闻 CHINA",   # soft-locked
    "虎嗅":          "中国要闻 CHINA",   # soft-locked
    "钛媒体":        "中国要闻 CHINA",   # soft-locked
    "IT之家":        "中国要闻 CHINA",   # soft-locked
    "少数派":        "中国要闻 CHINA",   # soft-locked
    # AI/前沿 AI FRONTIER (7 non-Chinese tech sources — Chinese tech sources moved to CHINA above)
    "TechCrunch": "AI/前沿 AI FRONTIER", "Hacker News": "AI/前沿 AI FRONTIER",
    "Ars Technica": "AI/前沿 AI FRONTIER", "The Verge": "AI/前沿 AI FRONTIER",
    "BBC Technology": "AI/前沿 AI FRONTIER", "NYT Technology": "AI/前沿 AI FRONTIER",
    "Solidot": "AI/前沿 AI FRONTIER",
    # 市场/宏观 MACRO & MARKETS (4 sources)
    "Bloomberg Econ": "市场/宏观 MACRO & MARKETS",
    "Bloomberg": "市场/宏观 MACRO & MARKETS",
    "FT": "市场/宏观 MACRO & MARKETS",
    "CNBC": "市场/宏观 MACRO & MARKETS",
    # 全球政治 GLOBAL POLITICS (6 sources)
    "BBC World": "全球政治 GLOBAL POLITICS",
    "纽约时报中文": "全球政治 GLOBAL POLITICS",
    "BBC中文": "全球政治 GLOBAL POLITICS",
    "Bloomberg Politics": "全球政治 GLOBAL POLITICS",
    "The Guardian World": "全球政治 GLOBAL POLITICS",
    "SCMP": "全球政治 GLOBAL POLITICS",
    # 公司/产业 CORPORATE & INDUSTRY (2 sources)
    "NYT Business": "公司/产业 CORPORATE & INDUSTRY",
    "BBC Business": "公司/产业 CORPORATE & INDUSTRY",
    # 亚太要闻 ASIA-PACIFIC (6 sources, soft-locked via Stage 2 — same as REGION_GROUPS default)
    "日经中文": "亚太要闻 ASIA-PACIFIC",
    "CNA": "亚太要闻 ASIA-PACIFIC",
    "RTHK中文": "亚太要闻 ASIA-PACIFIC",
    "Straits Times": "亚太要闻 ASIA-PACIFIC",
    "HKFP": "亚太要闻 ASIA-PACIFIC",
    "SCMP Hong Kong": "亚太要闻 ASIA-PACIFIC",
    # 加拿大 CANADA (2 sources, hard-locked via Stage 1)
    "CBC Business": "加拿大 CANADA",
    "Globe & Mail": "加拿大 CANADA",
    # 经济学人 THE ECONOMIST (4 sources, hard-locked via Stage 1)
    "Economist Leaders": "经济学人 THE ECONOMIST",
    "Economist Finance": "经济学人 THE ECONOMIST",
    "Economist Business": "经济学人 THE ECONOMIST",
    "Economist Science": "经济学人 THE ECONOMIST",
    # 消费科技 CONSUMER TECH and 社会观察 SOCIETY are LLM-fed only — no source defaults
}

ALL_REGIONS = sorted(set(SOURCE_TO_REGION.values()))
TOTAL_REGIONS = len(ALL_REGIONS)  # 8 source-default zones (CONSUMER_TECH + SOCIETY are LLM-fed)


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
