#!/usr/bin/env python3
"""Tests for rss-production-review.py"""
import os
import json
import importlib.util
from datetime import datetime, timezone, timedelta

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rss_production_review", os.path.join(_repo, "rss-production-review.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

BJT = timezone(timedelta(hours=8))


def _write_log(tmp_path, lines: list) -> str:
    p = str(tmp_path / "prod-log.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return p


def _rec(day, source, fetched, selected, **meta):
    ts = f"2026-06-{day:02d}T08:00:00.000000+08:00"
    d = {"ts": ts, "source": source, "fetched": fetched, "selected": selected}
    d.update(meta)
    return d


def test_load_records_skips_bad_lines(tmp_path):
    p = str(tmp_path / "log.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(_rec(1, "A", 3, 2)) + "\n")
        f.write("not json\n")
        f.write("\n")
        f.write(json.dumps(_rec(2, "A", 1, 0)) + "\n")
    recs = _mod.load_records(p)
    assert len(recs) == 2
    assert recs[0]["source"] == "A"


def test_filter_window_keeps_only_recent(tmp_path):
    now = datetime(2026, 7, 2, 8, 0, tzinfo=BJT)
    recs = [_rec(1, "A", 3, 1), _rec(25, "A", 3, 1)]
    kept = _mod.filter_window(recs, now, 30)  # cutoff = 2026-06-02; June 1 falls outside
    assert len(kept) == 1
    assert kept[0]["ts"].startswith("2026-06-25")


def test_aggregate_by_source_sums_and_active_days(tmp_path):
    recs = [_rec(1, "A", 3, 2), _rec(1, "A", 2, 1), _rec(3, "A", 4, 0), _rec(5, "B", 0, 0)]
    agg = _mod.aggregate_by_source(recs)
    assert agg["A"]["fetched"] == 9
    assert agg["A"]["selected"] == 3
    assert agg["A"]["active_days"] == 2   # 06-01 and 06-03 had fetched>0
    assert agg["B"]["active_days"] == 0   # fetched=0 day doesn't count
