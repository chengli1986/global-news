#!/usr/bin/env python3
"""Deduplication, ranking, and region quota logic for news digest.
All functions are pure (no I/O) for easy testing."""
from datetime import datetime, timezone


def bigrams(text: str) -> set[str]:
    text = text.lower().strip()
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i+2] for i in range(len(text) - 1)}


def jaccard_similarity(a: str, b: str) -> float:
    bg_a, bg_b = bigrams(a), bigrams(b)
    if not bg_a or not bg_b:
        return 0.0
    return len(bg_a & bg_b) / len(bg_a | bg_b)


def deduplicate(articles: list[dict], threshold: float) -> list[dict]:
    sorted_articles = sorted(articles, key=lambda a: a.get("pub_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    kept: list[dict] = []
    for article in sorted_articles:
        title = article.get("title", "")
        if not any(jaccard_similarity(title, k["title"]) > threshold for k in kept):
            kept.append(article)
    return kept


def _get_tier(source_name: str, source_tiers: dict) -> str:
    for tier, sources in source_tiers.items():
        if source_name in sources:
            return tier
    return "commodity"


def _rank_score(article: dict, tuning: dict, region_fill: dict, now: datetime) -> float:
    pub_dt = article.get("pub_dt")
    hours_old = max(0, (now - pub_dt).total_seconds() / 3600) if pub_dt else 72
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
    if now is None:
        now = datetime.now(timezone.utc)
    max_total = tuning.get("max_total_articles", 60)
    region_quotas = tuning.get("region_quotas", {})
    region_fill: dict[str, int] = {}
    remaining = list(articles)
    selected: list[dict] = []
    while remaining:
        if len(selected) >= max_total:
            break
        article = max(remaining, key=lambda a: _rank_score(a, tuning, region_fill, now))
        remaining.remove(article)
        region = article.get("region", "")
        quota = region_quotas.get(region, {"min": 0, "max": 100})
        current = region_fill.get(region, 0)
        if current >= quota["max"]:
            continue
        selected.append(article)
        region_fill[region] = current + 1
    # Phase 2: enforce minimums
    selected_set = set(id(a) for a in selected)
    for region, quota in region_quotas.items():
        current = region_fill.get(region, 0)
        if current >= quota["min"]:
            continue
        candidates = [a for a in remaining if a.get("region") == region and id(a) not in selected_set]
        needed = quota["min"] - current
        for article in candidates[:needed]:
            selected.append(article)
            selected_set.add(id(article))
            region_fill[region] = region_fill.get(region, 0) + 1
    return selected
