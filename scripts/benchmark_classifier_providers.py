#!/usr/bin/env python3
"""Benchmark Stage 4 LLM classifier: gpt-4.1-mini vs gemini-2.5-flash-lite.

Runs classify_articles() twice on the same fixture with each provider forced,
then diffs the per-article (region, topic, geo, subtopic) outputs.

Usage:
    python3 scripts/benchmark_classifier_providers.py [fixture_path]

Default fixture: most recent file in fixtures/ that classifier deems usable.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HOME = Path.home()
SENDER_PATH = HOME / "global-news/unified-global-news-sender.py"
FIXTURES_DIR = HOME / "global-news/fixtures"

spec = importlib.util.spec_from_file_location("sender_mod", SENDER_PATH)
sender_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender_mod)
UnifiedNewsSender = sender_mod.UnifiedNewsSender


def load_fixture(path: Path) -> dict:
    """Load a fixture file → news_data dict {source: [(title,url,pub_dt,None)...]}."""
    raw = json.loads(path.read_text())
    news_data: dict[str, list[tuple]] = {}
    for src, items in raw.get("sources", {}).items():
        out = []
        for it in items:
            pub_dt = None
            if it.get("pub_dt"):
                try:
                    pub_dt = datetime.fromisoformat(it["pub_dt"].replace("Z", "+00:00"))
                except Exception:
                    pub_dt = None
            out.append((it["title"], it.get("url", ""), pub_dt, None))
        news_data[src] = out
    return news_data


def run_with_openai(news_data: dict) -> tuple[dict, str]:
    """Run classify_articles() forcing OpenAI gpt-4.1-mini."""
    sender = UnifiedNewsSender()
    sender.news_data = copy.deepcopy(news_data)
    sender._gemini_key = ""  # disable gemini fallback so OpenAI is the only path
    if not sender._openai_key:
        raise RuntimeError("OPENAI_API_KEY not set in environment")
    sender.classify_articles()
    return sender._classifications, getattr(sender, "_last_provider", "OpenAI")


def run_with_flash_lite(news_data: dict) -> tuple[dict, str]:
    """Run classify_articles() forcing gemini-2.5-flash-lite."""
    sender = UnifiedNewsSender()
    sender.news_data = copy.deepcopy(news_data)
    sender._openai_key = ""  # bypass OpenAI

    if not sender._gemini_key:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY not set")

    gemini_key = sender._gemini_key

    def lite_only(self, payload, timeout=120, max_retries=3):
        base = {k: v for k, v in payload.items() if k != "response_format"}
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        result = self._api_call_with_retry(
            url=url, api_key=gemini_key,
            payload=dict(base, model="gemini-2.5-flash-lite"),
            timeout=timeout, max_retries=max_retries,
            provider="Gemini(gemini-2.5-flash-lite)",
        )
        self._last_provider = "gemini-2.5-flash-lite"
        return result

    sender._llm_api_call = lite_only.__get__(sender, UnifiedNewsSender)
    sender.classify_articles()
    return sender._classifications, "gemini-2.5-flash-lite"


def diff_classifications(a: dict, b: dict, news_data: dict) -> None:
    """Print a structured diff between two _classifications dicts."""
    keys = sorted(set(a.keys()) | set(b.keys()))
    total = len(keys)
    fields = ("region", "topic", "geo", "subtopic")

    field_diffs = Counter()
    full_match = 0
    region_a = Counter()
    region_b = Counter()
    diff_examples: list[tuple] = []

    for k in keys:
        ea = a.get(k) or {}
        eb = b.get(k) or {}
        all_same = True
        for f in fields:
            va = ea.get(f) or "(default)"
            vb = eb.get(f) or "(default)"
            if va != vb:
                field_diffs[f] += 1
                all_same = False
        ra = ea.get("region") or "(default)"
        rb = eb.get("region") or "(default)"
        region_a[ra] += 1
        region_b[rb] += 1
        if all_same:
            full_match += 1
        else:
            src, idx = k
            articles = news_data.get(src, [])
            title = articles[idx][0] if idx < len(articles) else "?"
            src_label = f"{src[1]} ({src[0].replace('.json','')})" if isinstance(src, tuple) else src
            diff_examples.append((src_label, title, ea, eb))

    print(f"\n{'='*70}")
    print(f"BENCHMARK SUMMARY")
    print(f"{'='*70}")
    print(f"Total articles classified: {total}")
    print(f"Identical on all 4 fields: {full_match} ({100*full_match/total:.1f}%)")
    print(f"At least one field differs: {total-full_match} ({100*(total-full_match)/total:.1f}%)")
    print()
    print(f"Per-field disagreement count (out of {total}):")
    for f in fields:
        n = field_diffs[f]
        print(f"  {f:<10}: {n:>3}  ({100*n/total:5.1f}%)")

    print(f"\n{'-'*70}")
    print(f"Region distribution comparison (A=4.1-mini  B=flash-lite):")
    print(f"{'-'*70}")
    all_regions = sorted(set(region_a) | set(region_b))
    print(f"  {'region':<25} {'A':>6} {'B':>6} {'Δ':>6}")
    for r in all_regions:
        ra, rb = region_a[r], region_b[r]
        print(f"  {r:<25} {ra:>6} {rb:>6} {rb-ra:+d}")

    print(f"\n{'-'*70}")
    print(f"Sample disagreements (up to 20):")
    print(f"{'-'*70}")
    for src, title, ea, eb in diff_examples[:20]:
        print(f"\n  [{src}] {title[:70]}")
        for f in fields:
            va = ea.get(f) or "(default)"
            vb = eb.get(f) or "(default)"
            mark = " " if va == vb else "≠"
            print(f"    {f:<10} {mark}  A: {va!s:<25} B: {vb!s}")

    if len(diff_examples) > 20:
        print(f"\n  ... and {len(diff_examples) - 20} more disagreements")


def main():
    fx_paths: list[Path]
    if len(sys.argv) > 1:
        fx_paths = [Path(p) for p in sys.argv[1:]]
    else:
        fixtures = sorted(FIXTURES_DIR.glob("2026-*.json"))
        if not fixtures:
            print("No fixtures found")
            sys.exit(1)
        fx_paths = fixtures[-5:]

    # Aggregate diff state across fixtures
    agg_a: dict = {}
    agg_b: dict = {}
    agg_news: dict = {}

    for i, fx_path in enumerate(fx_paths, 1):
        print(f"\n{'#'*70}")
        print(f"Fixture {i}/{len(fx_paths)}: {fx_path.name}")
        print(f"{'#'*70}")
        news_data = load_fixture(fx_path)
        total_articles = sum(len(v) for v in news_data.values())
        print(f"Sources: {len(news_data)}, Articles: {total_articles}")

        print("  [1/2] OpenAI gpt-4.1-mini ...")
        cls_a, prov_a = run_with_openai(news_data)
        print(f"    ✓ classified {len(cls_a)} via {prov_a}")

        print("  [2/2] gemini-2.5-flash-lite ...")
        cls_b, prov_b = run_with_flash_lite(news_data)
        print(f"    ✓ classified {len(cls_b)} via {prov_b}")

        # Namespace each fixture's keys so they don't collide
        for k, v in cls_a.items():
            agg_a[(fx_path.name, k)] = v
        for k, v in cls_b.items():
            agg_b[(fx_path.name, k)] = v
        for src, items in news_data.items():
            agg_news[(fx_path.name, src)] = items

    # Build a flat news_data view for diff_classifications (it indexes by (src, idx))
    # We'll wrap by namespacing src too.
    flat_news: dict = {}
    for (fx_name, src), items in agg_news.items():
        flat_news[(fx_name, src)] = items

    # Rebuild keys in agg_a/agg_b to use ((fx_name, src), idx) form for diff fn
    flat_a = {((fx, src), idx): v for (fx, (src, idx)), v in agg_a.items()}
    flat_b = {((fx, src), idx): v for (fx, (src, idx)), v in agg_b.items()}

    diff_classifications(flat_a, flat_b, flat_news)


if __name__ == "__main__":
    main()
