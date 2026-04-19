#!/usr/bin/env python3
"""Dry-run classifier diff report — Task 10/11 of classification redesign.

Compares 'before' (parsed from actual sent emails per news-sender-YYYYMMDD.log)
vs 'after' (new 4-stage funnel + 10-zone REGION_GROUPS) on 5 recent fixtures.

Output sections (per user spec for Task 10):
  1. Per-fixture region distribution: before vs after
  2. Per-fixture provenance + handled-by stats (new pipeline)
  3. Chinese-source articles that drifted back from GLOBAL FINANCE → CHINA
  4. Foreign-source articles routed to CANADA / ASIA-PAC by Stage 3 geo keyword
  5. New zones (CONSUMER_TECH, SOCIETY, CORPORATE, MACRO_MARKETS) — population check
  6. Final verdict: ship-ready or quota tuning needed

Usage:
    python3 scripts/dry_run_classifier.py
    python3 scripts/dry_run_classifier.py --output report.md  # save markdown
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# --- Setup paths ---------------------------------------------------------

HOME = Path.home()
SENDER_PATH = HOME / "global-news/unified-global-news-sender.py"
FIXTURES_DIR = HOME / "global-news/fixtures"
LOGS_DIR = HOME / ".openclaw/workspace/logs"

# Load sender module (handles dashed filename)
spec = importlib.util.spec_from_file_location("sender_mod", SENDER_PATH)
sender_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender_mod)
UnifiedNewsSender = sender_mod.UnifiedNewsSender


# --- Old-distribution parser (from sent email log) -----------------------

REGION_HEADER_RE = re.compile(r'━{60,}\n  (\S[^\n]*?\(\d+\))\n━{60,}')


def parse_old_distribution(log_file: Path) -> dict[str, list[tuple[str, str]]]:
    """Parse news-sender log → {region_name: [(source, title), ...]}."""
    if not log_file.exists():
        return {}
    text = log_file.read_text(errors="replace")
    sections = REGION_HEADER_RE.split(text)
    # split result: [pre, header1, body1, header2, body2, ...]
    result: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for i in range(1, len(sections), 2):
        if i + 1 >= len(sections):
            break
        header = sections[i].strip()
        body = sections[i + 1]
        # Strip the trailing count "(N)"
        m = re.match(r'^(.+?)\s*\((\d+)\)$', header)
        if not m:
            continue
        region = m.group(1).strip()
        # Extract articles: numbered title block followed by "via <Source> [date]"
        for art_match in re.finditer(
            r'\d+\.\s+([^\n]+)(?:\n[^\n]*)*?\n\s*via\s+([^\[\n]+?)\s*\[\d',
            body,
        ):
            title = art_match.group(1).strip()
            source = art_match.group(2).strip()
            result[region].append((source, title))
    return dict(result)


# --- New-pipeline runner -------------------------------------------------

def reconstruct_news_data(fx_path: Path) -> dict[str, list[tuple]]:
    """Convert fixture JSON → news_data dict shape sender expects."""
    fx = json.loads(fx_path.read_text())
    sources = fx.get("sources", {})
    result: dict[str, list[tuple]] = {}
    for src, articles in sources.items():
        result[src] = []
        for art in articles:
            if not isinstance(art, dict):
                continue
            title = art.get("title", "")
            url = art.get("url", "")
            pub_dt_raw = art.get("pub_dt")
            pub_dt = None
            if pub_dt_raw:
                try:
                    pub_dt = datetime.fromisoformat(str(pub_dt_raw).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass
            result[src].append((title, url, pub_dt, None))
    return result


def run_new_pipeline(fx_path: Path) -> tuple[dict, dict, dict, int]:
    """Run Stages 1-3 of new pipeline (no LLM key → Stage 4 articles fall back
    to source-default region in this dry-run).

    Returns (region_dist, classifications, stage_counts, total_articles).
    stage_counts is keyed over ALL articles (not just _classifications) — articles
    that didn't get classified by Stages 1-3 are bucketed into 'Stage 4 skipped
    (would hit LLM)' so verdict denominators reflect total articles, not the
    classified subset (Codex review fix #1).
    """
    sender = UnifiedNewsSender()
    sender.news_data = reconstruct_news_data(fx_path)
    sender._llm_status = []
    sender._openai_key = None  # skip real LLM call (deterministic Stages 1-3 only)
    sender._gemini_key = None

    # Capture stdout to suppress sender's own print output
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sender.classify_articles()

    # Build region distribution + count total articles
    region_dist: dict[str, list[tuple[str, str]]] = defaultdict(list)
    total_articles = 0
    for src, articles in sender.news_data.items():
        for idx, art in enumerate(articles):
            total_articles += 1
            entry = sender._classifications.get((src, idx))
            if entry and entry.get("region"):
                region = entry["region"]
            else:
                region = sender._source_default_region(src)
            title = art[0] if isinstance(art, tuple) else art
            region_dist[region].append((src, title))

    # Stage labels
    def _stage_label(rc: str) -> str:
        if rc.startswith("source_lock:hard"): return "Stage 1 (hard lock)"
        if rc.startswith("source_lock:soft"): return "Stage 2 (soft lock)"
        if rc.startswith("soft_escape"):      return "Stage 2 (escape→LLM)"
        if rc.startswith("geo_keyword"):      return "Stage 3 (geo keyword)"
        if rc.startswith("llm:"):             return "Stage 4 (LLM)"
        if rc.startswith("fallback:"):        return "Fallback"
        return "Unknown"

    stage_counts = Counter(
        _stage_label(e["reason_code"]) for e in sender._classifications.values()
    )
    # Codex review fix #1: bucket unclassified articles into 'Stage 4 skipped'
    # so percentages compute over total articles, not classified subset
    classified_count = sum(stage_counts.values())
    skipped = total_articles - classified_count
    if skipped > 0:
        stage_counts["Stage 4 skipped (would hit LLM)"] = skipped

    return dict(region_dist), dict(sender._classifications), dict(stage_counts), total_articles


# --- Diff analysis -------------------------------------------------------

def emoji_strip(s: str) -> str:
    """Strip emoji prefix to match no-emoji region keys."""
    for char in s:
        if char.isalnum() or char in ' &':
            return s[s.index(char):].strip()
    return s


def find_chinese_source_drifters(old_dist: dict, new_dist: dict) -> list[dict]:
    """Find articles from Chinese sources that USED to be in GLOBAL FINANCE
    (or AI/前沿 etc.) and are now in CHINA per new pipeline.
    """
    chinese_sources = {"中国财经要闻", "中国科技/AI", "界面新闻", "南方周末",
                       "36氪", "虎嗅", "钛媒体", "IT之家", "少数派"}
    # Build (src, title) → region maps
    old_map = {}
    for region, articles in old_dist.items():
        for src, title in articles:
            old_map[(src, title)] = region
    new_map = {}
    for region, articles in new_dist.items():
        for src, title in articles:
            new_map[(src, title)] = region

    drifters = []
    for (src, title), new_region in new_map.items():
        if src not in chinese_sources:
            continue
        old_region = old_map.get((src, title), "(not in old send)")
        # Filter to those that moved AWAY from a non-CHINA region INTO CHINA
        new_no_emoji = emoji_strip(new_region)
        old_no_emoji = emoji_strip(old_region) if old_region != "(not in old send)" else ""
        if "中国要闻" in new_no_emoji and "中国要闻" not in old_no_emoji and old_region != "(not in old send)":
            drifters.append({
                "source": src,
                "title": title[:60],
                "old": emoji_strip(old_region),
                "new": new_no_emoji,
            })
    return drifters


def find_geo_keyword_recoveries(classifications: dict, news_data: dict) -> list[dict]:
    """Find articles routed to CANADA/ASIA-PAC by Stage 3 geo keyword (Codex
    point: foreign-source articles that recover their geographic relevance).
    """
    recoveries = []
    for (src, idx), entry in classifications.items():
        rc = entry["reason_code"]
        if not rc.startswith("geo_keyword:"):
            continue
        articles = news_data.get(src, [])
        if idx >= len(articles):
            continue
        title = articles[idx][0] if isinstance(articles[idx], tuple) else articles[idx]
        recoveries.append({
            "source": src,
            "title": title[:60],
            "region": emoji_strip(entry["region"]),
            "reason": rc.split(":", 1)[1],
        })
    return recoveries


# --- Report rendering ----------------------------------------------------

def render_report(fixtures: list[Path]) -> str:
    """Build the full markdown report."""
    out = []
    out.append("# News Classification Dry-Run Diff Report\n")
    out.append(f"**Generated**: {datetime.now().isoformat(timespec='minutes')}\n")
    out.append(f"**Fixtures examined**: {len(fixtures)} most recent\n")
    out.append(f"**Pipeline**: 4-stage funnel + 2-axis labels (Tasks 1-9, commits cef7630..1a9f3be)\n")
    out.append("**Mode**: Stages 1-3 deterministic + simulated Stage 4 (no LLM call).\n")
    out.append(
        "Articles that would normally hit Stage 4 LLM in production fall back to "
        "their source-default region in this dry-run. Provenance/handled-by stats "
        "treat them as 'Fallback' rather than LLM-classified.\n\n"
    )

    # Aggregate counters across all fixtures
    all_stage_counts: Counter = Counter()
    all_old_dist: dict = defaultdict(list)
    all_new_dist: dict = defaultdict(list)
    all_drifters: list = []
    all_recoveries: list = []
    # (sparse_zones_per_fixture removed in Codex review fix #2 — replaced by
    # explicit walk over expected 10 zones in §5)

    out.append("## §1 Per-fixture region distribution (before → after)\n")

    for fx in fixtures:
        fx_date = fx.stem  # e.g. "2026-04-19-08"
        date_only = fx_date[:10].replace("-", "")
        log_file = LOGS_DIR / f"news-sender-{date_only}.log"

        old_dist = parse_old_distribution(log_file)
        new_dist, classifications, stage_counts, total_articles = run_new_pipeline(fx)

        out.append(f"### Fixture `{fx_date}`\n")
        if not old_dist:
            out.append(f"*Old log {log_file.name} not found — skipping before-comparison for this fixture.*\n\n")
        else:
            old_counts = {emoji_strip(r): len(v) for r, v in old_dist.items()}
            new_counts = {emoji_strip(r): len(v) for r, v in new_dist.items()}
            all_regions = sorted(set(old_counts) | set(new_counts))

            out.append("| Region | Before | After | Δ |\n")
            out.append("|--------|-------:|------:|---:|\n")
            for region in all_regions:
                old_n = old_counts.get(region, 0)
                new_n = new_counts.get(region, 0)
                delta = new_n - old_n
                marker = "→" if delta == 0 else ("📈" if delta > 0 else "📉")
                out.append(f"| {region} | {old_n} | {new_n} | {marker} {delta:+d} |\n")
            out.append("\n")

        # Stage stats — denominator = total_articles (Codex review fix #1)
        # Now percentages reflect "% of all articles", not "% of classified subset"
        out.append(f"**Routing stats** ({total_articles} total articles in fixture):\n\n")
        out.append("| Stage | Count | % of total |\n|-------|------:|--:|\n")
        for stage in ["Stage 1 (hard lock)", "Stage 2 (soft lock)", "Stage 2 (escape→LLM)",
                      "Stage 3 (geo keyword)", "Stage 4 (LLM)",
                      "Stage 4 skipped (would hit LLM)", "Fallback"]:
            cnt = stage_counts.get(stage, 0)
            if cnt > 0:
                pct = 100 * cnt / max(total_articles, 1)
                out.append(f"| {stage} | {cnt} | {pct:.1f}% |\n")
        out.append("\n")

        # Handled-by view — denominator also total_articles
        det_prefixes = ("source_lock:hard", "source_lock:soft", "geo_keyword")
        llm_prefixes = ("soft_escape", "llm:")
        det = sum(1 for e in classifications.values()
                  if e["reason_code"].startswith(det_prefixes))
        llm_hit = sum(1 for e in classifications.values()
                      if e["reason_code"].startswith(llm_prefixes))
        skipped = stage_counts.get("Stage 4 skipped (would hit LLM)", 0)
        if total_articles > 0:
            out.append(f"**Handled-by (over {total_articles} total)**: "
                       f"Deterministic Stage 1-3 = {det} ({100*det/total_articles:.1f}%), "
                       f"Hit LLM (Stage 2 escape) = {llm_hit} ({100*llm_hit/total_articles:.1f}%), "
                       f"Stage 4 skipped in dry-run = {skipped} ({100*skipped/total_articles:.1f}%)\n\n")

        # Aggregate
        all_stage_counts.update(stage_counts)
        for r, items in old_dist.items():
            all_old_dist[emoji_strip(r)].extend((fx_date, src, title) for src, title in items)
        for r, items in new_dist.items():
            all_new_dist[emoji_strip(r)].extend((fx_date, src, title) for src, title in items)

        # Per-fixture drifters and recoveries
        all_drifters.extend({**d, "fixture": fx_date}
                            for d in find_chinese_source_drifters(old_dist, new_dist))
        all_recoveries.extend({**r, "fixture": fx_date}
                              for r in find_geo_keyword_recoveries(
                                  classifications, reconstruct_news_data(fx)))

    # §2 Aggregate stage stats — include 'Stage 4 skipped' so % sums to 100%
    total = sum(all_stage_counts.values())
    out.append("## §2 Aggregate routing stats (all fixtures, % over total articles)\n\n")
    out.append("| Stage | Total | % of all articles |\n|-------|------:|--:|\n")
    for stage in ["Stage 1 (hard lock)", "Stage 2 (soft lock)", "Stage 2 (escape→LLM)",
                  "Stage 3 (geo keyword)", "Stage 4 (LLM)",
                  "Stage 4 skipped (would hit LLM)", "Fallback"]:
        cnt = all_stage_counts.get(stage, 0)
        if cnt > 0:
            out.append(f"| {stage} | {cnt} | {100*cnt/total:.1f}% |\n")
    out.append("\n")

    # §3 Chinese-source drifters
    out.append("## §3 Chinese sources back to CHINA (drift from old GLOBAL FINANCE etc.)\n\n")
    if not all_drifters:
        out.append("*No drifters found in fixtures with available logs.*\n\n")
    else:
        out.append(f"**{len(all_drifters)} articles** moved to CHINA region under new pipeline.\n\n")
        out.append("| Fixture | Source | Title | Old | New |\n|---|---|---|---|---|\n")
        for d in all_drifters[:25]:
            out.append(f"| {d['fixture']} | {d['source']} | {d['title']} | {d['old']} | {d['new']} |\n")
        if len(all_drifters) > 25:
            out.append(f"\n*... and {len(all_drifters) - 25} more.*\n\n")
        else:
            out.append("\n")

    # §4 Geo keyword recoveries
    out.append("## §4 Foreign sources → CANADA / ASIA-PAC via Stage 3 geo keyword\n\n")
    if not all_recoveries:
        out.append("*No Stage 3 geo-keyword matches in these fixtures.*\n\n")
    else:
        out.append(f"**{len(all_recoveries)} articles** routed by geo keyword (would have gone elsewhere via topic LLM in old pipeline).\n\n")
        out.append("| Fixture | Source | Title | Region | Reason |\n|---|---|---|---|---|\n")
        for r in all_recoveries[:25]:
            out.append(f"| {r['fixture']} | {r['source']} | {r['title']} | {r['region']} | {r['reason']} |\n")
        if len(all_recoveries) > 25:
            out.append(f"\n*... and {len(all_recoveries) - 25} more.*\n\n")
        else:
            out.append("\n")

    # §5 New-zone population
    out.append("## §5 New 10-zone population check\n\n")
    new_zones = [
        ("AI/前沿 AI FRONTIER", 12, 20),
        ("市场/宏观 MACRO & MARKETS", 12, 20),
        ("全球政治 GLOBAL POLITICS", 14, 22),
        ("中国要闻 CHINA", 14, 22),
        ("公司/产业 CORPORATE & INDUSTRY", 10, 16),
        ("消费科技 CONSUMER TECH", 6, 10),
        ("亚太要闻 ASIA-PACIFIC", 8, 14),
        ("加拿大 CANADA", 6, 12),
        ("经济学人 THE ECONOMIST", 4, 10),
        ("社会观察 SOCIETY", 3, 8),
    ]
    # Codex review fix #2: walk all 10 expected zones explicitly (not just zones
    # that appear in new_dist), so a fully-empty zone correctly shows N/N sparse
    # rather than 0/N (which falsely implied "no fixture had it sparse").
    out.append("| Region | Quota min-max | Avg articles/fixture | Empty fixtures | Status |\n")
    out.append("|--------|---:|---:|---|---|\n")
    n_fx = len(fixtures)
    sparse_now: list[str] = []
    for region, qmin, qmax in new_zones:
        items = all_new_dist.get(region, [])
        avg = len(items) / n_fx if n_fx else 0
        # Count fixtures where this region had < 3 articles by walking per-fixture data
        fx_counts_for_region: dict[str, int] = defaultdict(int)
        for entry in items:
            fx_id = entry[0]  # (fx_date, src, title)
            fx_counts_for_region[fx_id] += 1
        empty_fxs = sum(1 for fx in fixtures if fx_counts_for_region.get(fx.stem, 0) < 3)
        if avg >= qmin:
            status = "✓ healthy"
        elif avg >= qmin / 2:
            status = "⚠️ below min"
        else:
            status = "🔴 sparse / empty"
            sparse_now.append(region)
        out.append(f"| {region} | {qmin}-{qmax} | {avg:.1f} | {empty_fxs}/{n_fx} | {status} |\n")
    out.append("\n")

    # §6 Verdict — denominators corrected per Codex review #1
    out.append("## §6 Final verdict\n\n")
    aggregate_total = sum(all_stage_counts.values())  # all articles across all fixtures
    det_articles = sum(
        all_stage_counts.get(s, 0)
        for s in ["Stage 1 (hard lock)", "Stage 2 (soft lock)", "Stage 3 (geo keyword)"]
    )
    deterministic_pct = 100 * det_articles / max(aggregate_total, 1)
    skipped_articles = all_stage_counts.get("Stage 4 skipped (would hit LLM)", 0)
    skipped_pct = 100 * skipped_articles / max(aggregate_total, 1)

    out.append(f"- **Total articles** (5 fixtures): {aggregate_total}\n")
    out.append(f"- **Deterministic routing share** (Stage 1+2+3 / total): "
               f"{deterministic_pct:.1f}% (target ≥30%, ideal ≥50%)\n")
    out.append(f"- **Stage 4 skipped in dry-run** (would be LLM-classified in production): "
               f"{skipped_articles} articles ({skipped_pct:.1f}%)\n")
    out.append(f"- **Chinese-source drifters to CHINA**: {len(all_drifters)} articles "
               f"(spec target: 中国财经要闻 in old GLOBAL FINANCE drops 7→≤2)\n")
    out.append(f"- **Geo-keyword recoveries**: {len(all_recoveries)} foreign-source articles "
               f"now in CANADA/ASIA-PAC\n")
    if sparse_now:
        out.append(f"- **Sparse zones in dry-run** (avg < qmin/2): "
                   f"{', '.join(sparse_now)} — these are LLM-fed only "
                   f"(no Stage 1-3 routing path), production Stage 4 LLM expected to fill them.\n")
    out.append("\n")

    if deterministic_pct >= 30 and len(all_drifters) > 0 and len(all_recoveries) > 0:
        out.append("### ✅ Recommendation: SHIP-READY for Task 11 deploy\n\n")
        out.append(
            "All three core acceptance criteria met:\n"
            "1. Deterministic stages route ≥30% of total articles (saves LLM cost)\n"
            "2. Chinese sources flow back to CHINA (resolves spec §2 anomaly)\n"
            "3. Foreign-source geographic articles reach proper geo regions\n\n"
            f"Sparse zones identified above ({', '.join(sparse_now) if sparse_now else 'none'}) "
            f"are LLM-fed only and will fill in production once Stage 4 LLM is live. "
            f"Recommend proceeding to Task 11 with monitoring of first 3 sends to "
            f"validate quota tuning.\n"
        )
    else:
        out.append("### ⚠️ Recommendation: TUNE QUOTAS / SOFT-LOCK BEFORE TASK 11\n\n")
        out.append(
            "One or more acceptance criteria not met. Review per-fixture detail "
            "above and adjust _SOFT_LOCKS / region_quotas before deploying.\n"
        )

    return "".join(out)


# --- CLI -----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", "-o", help="Save markdown to file (default: print to stdout)")
    ap.add_argument("--n-fixtures", type=int, default=5, help="How many recent fixtures to scan")
    args = ap.parse_args()

    fixtures = sorted(FIXTURES_DIR.glob("*.json"))[-args.n_fixtures:]
    if not fixtures:
        print(f"No fixtures found in {FIXTURES_DIR}", file=sys.stderr)
        sys.exit(1)

    report = render_report(fixtures)

    if args.output:
        Path(args.output).write_text(report)
        print(f"Report written to {args.output} ({len(report.splitlines())} lines)")
    else:
        print(report)


if __name__ == "__main__":
    main()
