#!/usr/bin/env python3
"""Tests for rss-production-review.py"""
import os
import json
import importlib.util
from datetime import datetime, timezone, timedelta

import sys
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _repo)
import rss_registry as _reg
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


def test_degraded_desc_collapse_flagged():
    """pct_with_desc 基线>0.8、近期<0.3 → 描述变空预警。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    base = [_rec(d, "Decayed", 3, 2, avg_desc_len=200, pct_with_desc=1.0, pct_with_author=0.9)
            for d in range(1, 16)]              # baseline (older than recent 7d)
    recent = [_rec(d, "Decayed", 3, 2, avg_desc_len=180, pct_with_desc=0.1, pct_with_author=0.9)
              for d in range(24, 31)]            # last 7d: desc collapsed
    reg = _registry([_prod("Decayed")])
    d = _mod.find_degraded(reg, base + recent, now)
    assert any(x["name"] == "Decayed" and "desc" in x["signal"] for x in d)


def test_degraded_desc_len_shrink_flagged():
    """avg_desc_len 近期 < 基线*0.4 → 标题党预警。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    base = [_rec(d, "Shrink", 3, 2, avg_desc_len=200, pct_with_desc=1.0, pct_with_author=0.9)
            for d in range(1, 16)]
    recent = [_rec(d, "Shrink", 3, 2, avg_desc_len=50, pct_with_desc=1.0, pct_with_author=0.9)
              for d in range(24, 31)]
    reg = _registry([_prod("Shrink")])
    d = _mod.find_degraded(reg, base + recent, now)
    assert any(x["name"] == "Shrink" and "len" in x["signal"] for x in d)


def test_natively_short_source_not_flagged():
    """基线本就短摘要(150)、近期也短(140) → 不误判。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = ([_rec(d, "FP", 3, 2, avg_desc_len=150, pct_with_desc=1.0, pct_with_author=0.9) for d in range(1, 16)]
            + [_rec(d, "FP", 3, 2, avg_desc_len=140, pct_with_desc=1.0, pct_with_author=0.9) for d in range(24, 31)])
    reg = _registry([_prod("FP")])
    assert _mod.find_degraded(reg, recs, now) == []


def test_degraded_insufficient_sample_skipped():
    """基线/近期样本不足 → 跳过。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = [_rec(d, "Tiny", 3, 2, avg_desc_len=200, pct_with_desc=1.0, pct_with_author=0.9) for d in (24, 25)]
    reg = _registry([_prod("Tiny")])
    assert _mod.find_degraded(reg, recs, now) == []


def test_snapshot_rows_cover_all_production():
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = [_rec(5, "A", 6, 4), _rec(6, "B", 3, 0)]
    reg = _registry([_prod("A"), _prod("B")])
    rows = _mod.snapshot_rows(reg, recs, now)
    by = {r["name"]: r for r in rows}
    assert by["A"]["selected"] == 4 and by["B"]["selected"] == 0


def test_build_report_html_has_sections_and_command():
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    zombies = [{"name": "Z & Co", "category": "x", "fetched": 80, "selected": 0, "tenure_days": 90}]
    degraded = [{"name": "D", "signal": "avg_desc_len:desc-len-shrink", "baseline": 200, "recent": 40,
                 "detail": "avg_desc_len 200.00 → 40.00"}]
    snapshot = [{"name": "A", "category": "x", "fetched": 6, "selected": 4}]
    html = _mod.build_report_html(zombies, degraded, snapshot, now)
    assert "rss-demote-source.py" in html        # 可粘贴命令
    assert "Z &amp; Co" in html                  # HTML escape
    assert "desc-len-shrink" in html


def test_cmd_run_builds_and_sends(tmp_path, monkeypatch):
    """端到端：tmp registry+log → cmd_run 调 send 一次，邮件含 MIME 头 + 僵尸命令。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    reg_path = str(tmp_path / "registry.json")
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(_registry([_prod("Zombie", trial=None)]), f)
    log_path = _write_log(tmp_path, [_rec(d, "Zombie", 3, 0) for d in range(1, 29)])

    captured = {}
    def fake_send(html, subject, env_path=_mod.ENV_FILE):
        captured["html"] = html
        captured["subject"] = subject
        return True
    monkeypatch.setattr(_mod, "send_report_email", fake_send)
    monkeypatch.setattr(_reg, "REGISTRY_FILE", reg_path)

    rc = _mod.cmd_run(registry_path=reg_path, log_path=log_path, now=now, send=True)
    assert rc == 0
    assert "rss-demote-source.py" in captured["html"]
    assert "1 僵尸" in captured["subject"]


def test_send_report_email_builds_mime(monkeypatch):
    """send_report_email 拼出含 MIME-Version 的信封并调 curl 一次。"""
    calls = {}
    class R:  # fake CompletedProcess
        returncode = 0
        stderr = ""
    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        path = cmd[cmd.index("--upload-file") + 1]
        with open(path, encoding="utf-8") as f:
            calls["content"] = f.read()
        return R()
    monkeypatch.setattr(_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(_mod, "_load_env", lambda p=_mod.ENV_FILE: {
        "MAIL_TO": "to@x.com", "SMTP_USER": "u@163.com", "SMTP_PASS": "pw"})
    ok = _mod.send_report_email("<p>body</p>", "Subj", env_path="/dev/null")
    assert ok is True
    assert "MIME-Version: 1.0" in calls["content"]
    assert "curl" in calls["cmd"][0]


def test_degraded_ancient_baseline_excluded():
    """baseline 在 60 天窗口外 → 被排除，样本不足 → 不告警（#1: 不被远古历史污染）。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    ancient = [{"ts": f"2026-03-{d:02d}T08:00:00.000000+08:00", "source": "Old",
                "fetched": 3, "selected": 2, "avg_desc_len": 200,
                "pct_with_desc": 1.0, "pct_with_author": 0.9} for d in range(1, 16)]
    recent = [_rec(d, "Old", 3, 2, avg_desc_len=180, pct_with_desc=0.1, pct_with_author=0.9)
              for d in range(24, 31)]
    reg = _registry([_prod("Old")])
    assert _mod.find_degraded(reg, ancient + recent, now) == []
