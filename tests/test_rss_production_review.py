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


def test_graduation_date_from_trial_end():
    src = {"name": "Wired", "trial": {"outcome": "auto-graduated", "end_date": "2026-05-15"}}
    assert _mod.graduation_date(src).isoformat() == "2026-05-15"


def test_graduation_date_legacy_is_none():
    assert _mod.graduation_date({"name": "BBC World", "trial": None}) is None
    assert _mod.graduation_date({"name": "X"}) is None


def test_tenure_days_legacy_is_none():
    now = datetime(2026, 6, 13, 8, 0, tzinfo=BJT)
    assert _mod.tenure_days({"trial": None}, now) is None


def test_tenure_days_counts_from_graduation():
    now = datetime(2026, 6, 13, 8, 0, tzinfo=BJT)
    src = {"trial": {"outcome": "graduated", "end_date": "2026-05-14"}}
    assert _mod.tenure_days(src, now) == 30


def _registry(sources):
    return {"version": 1, "sources": sources}


def _prod(name, category="x", trial=None):
    return {"name": name, "category": category, "status": "production", "trial": trial}


def test_zombie_high_freq_no_selected_is_flagged():
    """30 天天天出文(active_days>=7)、selected<=1、在岗>=30天 → 僵尸。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = [_rec(d, "Zombie", 3, 0) for d in range(1, 29)]  # 28 active days, 0 selected
    reg = _registry([_prod("Zombie", trial={"outcome": "auto-graduated", "end_date": "2026-04-01"})])
    z = _mod.find_zombies(reg, recs, now)
    assert [x["name"] for x in z] == ["Zombie"]
    assert z[0]["selected"] == 0


def test_low_freq_high_quality_not_zombie():
    """低频但有 selected(>1) → 不是僵尸。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = [_rec(d, "Weekly", 2, 1) for d in (2, 9, 16, 23, 25, 27, 28, 29)]  # 8 days, selected=8
    reg = _registry([_prod("Weekly", trial={"outcome": "graduated", "end_date": "2026-04-01"})])
    assert _mod.find_zombies(reg, recs, now) == []


def test_insufficient_sample_skipped():
    """active_days < 7 → 样本不足，跳过(不判僵尸)。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = [_rec(d, "Sparse", 2, 0) for d in (2, 9, 16, 23)]  # only 4 active days
    reg = _registry([_prod("Sparse", trial={"outcome": "graduated", "end_date": "2026-04-01"})])
    assert _mod.find_zombies(reg, recs, now) == []


def test_grace_period_new_source_skipped():
    """在岗 < 30 天 → 宽限期，跳过。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = [_rec(d, "Newbie", 3, 0) for d in range(1, 29)]
    reg = _registry([_prod("Newbie", trial={"outcome": "auto-graduated", "end_date": "2026-06-15"})])  # 15d tenure
    assert _mod.find_zombies(reg, recs, now) == []


def test_legacy_source_passes_grace():
    """legacy(trial=None) 视为早已在岗 → 不被宽限跳过。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = [_rec(d, "OldZombie", 3, 0) for d in range(1, 29)]
    reg = _registry([_prod("OldZombie", trial=None)])
    assert [x["name"] for x in _mod.find_zombies(reg, recs, now)] == ["OldZombie"]


def test_dead_feed_fetched_zero_not_zombie():
    """fetched 全 0(源没出文) → 不算僵尸(归 health-check)。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = [_rec(d, "Dead", 0, 0) for d in range(1, 29)]
    reg = _registry([_prod("Dead", trial=None)])
    assert _mod.find_zombies(reg, recs, now) == []
