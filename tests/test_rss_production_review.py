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


def test_plan_c_done_detects_sci_health(tmp_path):
    sp = str(tmp_path / "sender.py")
    with open(sp, "w") as f: f.write("REGION_SCI_HEALTH = 'x'\n")
    assert _mod._plan_c_done(sp) is True
    sp2 = str(tmp_path / "s2.py")
    with open(sp2, "w") as f: f.write("nothing special\n")
    assert _mod._plan_c_done(sp2) is False


def test_plan_c_reminder_shows_when_not_done(tmp_path):
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    sp = str(tmp_path / "sender.py")
    with open(sp, "w") as f: f.write("no new region constant\n")  # C 未做
    reg = _registry([_prod("STAT News", category="healthcare")])
    recs = [_rec(d, "STAT News", 3, 2) for d in range(1, 10)]
    html = _mod.plan_c_reminder_html(reg, recs, now, sender_path=sp)
    assert "方案 C 待办" in html
    assert "STAT News" in html


def test_plan_c_reminder_hidden_when_done(tmp_path):
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    sp = str(tmp_path / "sender.py")
    with open(sp, "w") as f: f.write("REGION_SCI_HEALTH = 'x'\n")  # C 已做
    reg = _registry([_prod("STAT News", category="healthcare")])
    recs = [_rec(d, "STAT News", 3, 2) for d in range(1, 10)]
    assert _mod.plan_c_reminder_html(reg, recs, now, sender_path=sp) == ""


def test_build_report_includes_plan_c():
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    html = _mod.build_report_html([], [], [], now, "<div>PLANC_MARKER</div>")
    assert "PLANC_MARKER" in html


def _prod_cat(name, category, trial=None):
    return {"name": name, "category": category, "status": "production", "trial": trial}


def test_rotation_flags_group_laggard():
    """组内入选率最低、且 < 组内中位一半、组>3 → 建议轮换。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    reg = _registry([_prod_cat(n, "europe") for n in ("A", "B", "C", "Lag")])
    recs = [_rec(d, n, 3, 2) for d in range(1, 11) for n in ("A", "B", "C")]  # 67%, active_days=10
    recs += [_rec(d, "Lag", 6, 1) for d in range(1, 8)]  # active_days=7, 17% < 中位 67% 的一半
    out = _mod.find_rotation_candidates(reg, recs, now)
    assert [x["name"] for x in out] == ["Lag"]


def test_rotation_small_group_exempt():
    """组内有数据源 <= 保底(3) → 整组豁免。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    reg = _registry([_prod_cat("A", "hk_sea"), _prod_cat("B", "hk_sea"), _prod_cat("Lag", "hk_sea")])
    recs = [_rec(d, "A", 3, 2) for d in range(1, 11)] + [_rec(d, "B", 3, 2) for d in range(1, 11)] \
        + [_rec(d, "Lag", 3, 0) for d in range(1, 11)]
    assert _mod.find_rotation_candidates(reg, recs, now) == []


def test_rotation_legacy_no_category_exempt():
    """legacy(无 category) 不参与轮换。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    srcs = [_prod("A"), _prod("B"), _prod("C"), _prod("Lag")]
    for s in srcs: s["category"] = None
    reg = _registry(srcs)
    recs = [_rec(d, n, 3, 2) for d in range(1, 11) for n in ("A", "B", "C")] \
        + [_rec(d, "Lag", 3, 0) for d in range(1, 11)]
    assert _mod.find_rotation_candidates(reg, recs, now) == []


def test_rotation_skips_absolute_zombie():
    """组内最低若 selected<=1（绝对僵尸，归 A）→ 不被轮换重复标记。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    srcs = [_prod_cat(n, "europe") for n in ("A", "B", "C", "Z")]
    reg = _registry(srcs)
    recs = [_rec(d, n, 3, 2) for d in range(1, 11) for n in ("A", "B", "C")] \
        + [_rec(d, "Z", 3, 0) for d in range(1, 11)]  # Z selected=0 → A 僵尸
    assert _mod.find_rotation_candidates(reg, recs, now) == []


def test_rotation_low_freq_protected():
    """组内最低但 active_days<7（低频）→ 不轮换。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    srcs = [_prod_cat(n, "europe") for n in ("A", "B", "C", "Lo")]
    reg = _registry(srcs)
    recs = [_rec(d, n, 3, 2) for d in range(1, 11) for n in ("A", "B", "C")] \
        + [_rec(d, "Lo", 3, 1) for d in (2, 9)]  # 仅 2 天有内容 → active_days=2<7
    assert _mod.find_rotation_candidates(reg, recs, now) == []


def test_build_report_includes_rotation_section():
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    rotation = [{"name": "Lag News", "category": "europe", "selected": 7,
                 "fetched": 42, "rate": 0.1667, "group_rate_median": 0.67,
                 "group_median": 20, "group_size": 4, "tenure_days": 90}]
    html = _mod.build_report_html([], [], [], now, "", rotation)
    assert "建议轮换" in html
    assert "Lag News" in html
    assert "rss-demote-source.py" in html


def test_build_report_no_rotation_section_when_empty():
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    html = _mod.build_report_html([], [], [], now, "", [])
    assert "建议轮换" not in html


def test_rotation_uses_selection_rate_not_absolute_count():
    """入选率口径：limit 小的源不应因绝对入选数低被误判垫底。

    A/B/C limit=6（每天 fetched 6 / selected 3 = 50%），Cand limit=3
    （每天 fetched 3 / selected 1 = 33%）。绝对数 Cand=10 < 组中位 30 的一半，
    旧口径会误标；按入选率 33% > 组中位 50% 的一半，不该标。
    """
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    reg = _registry([_prod_cat(n, "tech_ai") for n in ("A", "B", "C", "Cand")])
    recs = [_rec(d, n, 6, 3) for d in range(1, 11) for n in ("A", "B", "C")]
    recs += [_rec(d, "Cand", 3, 1) for d in range(1, 11)]
    assert _mod.find_rotation_candidates(reg, recs, now) == []


def test_rotation_flags_laggard_by_selection_rate():
    """同 limit 下入选率不足组内中位一半 → 仍标记，并带上率字段。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    reg = _registry([_prod_cat(n, "hk_sea") for n in ("A", "B", "C", "Lag")])
    recs = [_rec(d, n, 6, 3) for d in range(1, 11) for n in ("A", "B", "C")]
    recs += [_rec(d, "Lag", 6, 1) for d in range(1, 11)]  # 17% vs 中位 50%
    out = _mod.find_rotation_candidates(reg, recs, now)
    assert [x["name"] for x in out] == ["Lag"]
    assert round(out[0]["rate"], 2) == 0.17
    assert out[0]["group_rate_median"] == 0.5
    assert out[0]["fetched"] == 60


def test_build_report_rotation_shows_selection_rate():
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    rotation = [{"name": "Lag News", "category": "europe", "selected": 10,
                 "fetched": 60, "rate": 0.1667, "group_rate_median": 0.5,
                 "group_median": 30, "group_size": 4, "tenure_days": 90}]
    html = _mod.build_report_html([], [], [], now, "", rotation)
    assert "入选率" in html
    assert "17%" in html
    assert "50%" in html


# ── 已下线源复活探测 ──────────────────────────────────────────────

def _rejected(name, reason, *, production=True, url="https://example.com/feed"):
    """构造一条 rejected registry 条目。production=True 表示它曾进过生产。"""
    return {
        "name": name,
        "url": url,
        "status": "rejected",
        "reject_reason": reason,
        "production": {"keywords": [], "limit": 8} if production else None,
    }


def test_revival_candidate_includes_technical_rejection():
    """WAF/403/timeout 这类技术性下线的曾生产源要被探测。"""
    reg = _registry([
        _rejected("36氪", "waf-block-upstream-and-fallback-route-503"),
        _rejected("Endpoints News", "persistent-403-removed-from-sources-2026-05-25"),
        _rejected("Nikkei Asia via rsshub", "persistent-timeout-removed-from-sources-2026-05-21"),
    ])
    names = [s["name"] for s in _mod.find_revival_candidates(reg)]
    assert names == ["36氪", "Endpoints News", "Nikkei Asia via rsshub"]


def test_revival_candidate_excludes_quality_rejections():
    """质量类下线的源不探测——它们恢复了也不该回来。"""
    reg = _registry([
        _rejected("HKFP", "rotation-group-laggard: 8% selection rate"),
        _rejected("少数派", "zombie-30d-no-selected"),
        _rejected("端传媒", "duplicate-feed: theinitium.com/feed/ == /rss/"),
        _rejected("SomeCandidate", "pool-cap"),
        _rejected("Other", "auto-removed"),
    ])
    assert _mod.find_revival_candidates(reg) == []


def test_revival_candidate_excludes_never_production():
    """没有 production 字段 = 从未进过生产（如 pool-cap 淘汰的候选），不探测。"""
    reg = _registry([_rejected("NeverLive", "unreachable", production=False)])
    assert _mod.find_revival_candidates(reg) == []


def test_revival_candidate_excludes_empty_reason():
    """reject_reason 为空无从判断下线原因，宁可不探。"""
    reg = _registry([
        _rejected("NoReason", ""),
        _rejected("NullReason", None),
    ])
    assert _mod.find_revival_candidates(reg) == []


def test_revival_candidate_excludes_non_rejected():
    """production/trialing/discovered 状态的源不在探测范围。"""
    reg = _registry([
        {"name": "Live", "url": "u", "status": "production", "reject_reason": None,
         "production": {"limit": 8}},
        {"name": "Cand", "url": "u", "status": "discovered", "reject_reason": None,
         "production": None},
    ])
    assert _mod.find_revival_candidates(reg) == []


def test_revival_candidate_marker_match_is_case_insensitive():
    """质量类关键词匹配不分大小写。"""
    reg = _registry([_rejected("X", "POOL-CAP"), _rejected("Y", "Zombie-30d")])
    assert _mod.find_revival_candidates(reg) == []


def _prober_from(table):
    """假 prober：按 url 查表返回 status_dict；表里没有的当作抓不到。"""
    def _p(name, url):
        return table.get(url, {"ok": False, "error": "unreachable: TimeoutError",
                               "article_count": 0, "newest_age_hours": None})
    return _p


def test_probe_revival_waf_page_is_not_revived():
    """★ 36氪 回归：HTTP 200 但解析不出文章（WAF 挑战页）不算恢复。"""
    reg = _registry([_rejected("36氪", "waf-block", url="https://36kr.com/feed")])
    prober = _prober_from({"https://36kr.com/feed": {
        "ok": False, "error": "XML parse error",
        "article_count": 0, "newest_age_hours": None}})
    assert _mod.probe_revivals(reg, prober) == []


def test_probe_revival_real_feed_is_revived():
    """解析出 >=1 篇文章就算恢复。"""
    reg = _registry([_rejected("36氪", "waf-block", url="https://36kr.com/feed")])
    prober = _prober_from({"https://36kr.com/feed": {
        "ok": True, "error": None, "article_count": 20, "newest_age_hours": 3.5}})
    out = _mod.probe_revivals(reg, prober)
    assert len(out) == 1
    assert out[0]["name"] == "36氪"
    assert out[0]["url"] == "https://36kr.com/feed"
    assert out[0]["article_count"] == 20
    assert out[0]["newest_age_hours"] == 3.5


def test_probe_revival_http_503_is_not_revived():
    """镜像路由 503 不算恢复（36氪 的 rsshub 路由就是这个形态）。"""
    reg = _registry([_rejected("36氪", "waf-block", url="https://rsshub.example.com/36kr/news")])
    prober = _prober_from({"https://rsshub.example.com/36kr/news": {
        "ok": False, "error": "unreachable: HTTPError",
        "article_count": 0, "newest_age_hours": None}})
    assert _mod.probe_revivals(reg, prober) == []


def test_probe_revival_stale_but_parsable_is_revived():
    """★ 判据是 article_count 不是 ok —— 刚恢复的源文章可能很旧，那不算没恢复。"""
    reg = _registry([_rejected("X", "timeout", url="https://x.com/feed")])
    prober = _prober_from({"https://x.com/feed": {
        "ok": False, "error": "stale feed (newest 900h, max 72h)",
        "article_count": 12, "newest_age_hours": 900.0}})
    out = _mod.probe_revivals(reg, prober)
    assert len(out) == 1
    assert out[0]["article_count"] == 12


def test_probe_revival_prober_exception_is_fail_closed():
    """★ 探测抛异常记为未恢复，绝不向上抛——周报不能被附加功能搞挂。"""
    reg = _registry([_rejected("X", "timeout", url="https://x.com/feed")])

    def _boom(name, url):
        raise RuntimeError("network exploded")

    assert _mod.probe_revivals(reg, _boom) == []


def test_probe_revival_skips_quality_rejections():
    """质量类下线的源根本不会被探测（prober 不会被调用）。"""
    reg = _registry([_rejected("少数派", "zombie-30d-no-selected",
                               url="https://sspai.com/feed")])
    called = []

    def _p(name, url):
        called.append(url)
        return {"ok": True, "error": None, "article_count": 50, "newest_age_hours": 1.0}

    assert _mod.probe_revivals(reg, _p) == []
    assert called == []


def test_probe_revival_tries_fallback_url_too():
    """原址不通但镜像通，也算恢复，且报告的是活的那个 URL。"""
    reg = _registry([_rejected("虎嗅", "waf-block", url="https://huxiu.com/feed")])
    fallbacks = {"虎嗅": "https://rsshub.example.com/huxiu/article"}
    prober = _prober_from({"https://rsshub.example.com/huxiu/article": {
        "ok": True, "error": None, "article_count": 30, "newest_age_hours": 2.0}})
    out = _mod.probe_revivals(reg, prober, fallbacks=fallbacks)
    assert len(out) == 1
    assert out[0]["url"] == "https://rsshub.example.com/huxiu/article"


def test_probe_revival_prefers_primary_url_when_both_alive():
    """原址和镜像都活时报原址，且不再探镜像。"""
    reg = _registry([_rejected("虎嗅", "waf-block", url="https://huxiu.com/feed")])
    fallbacks = {"虎嗅": "https://rsshub.example.com/huxiu/article"}
    called = []

    def _p(name, url):
        called.append(url)
        return {"ok": True, "error": None, "article_count": 9, "newest_age_hours": 1.0}

    out = _mod.probe_revivals(reg, _p, fallbacks=fallbacks)
    assert out[0]["url"] == "https://huxiu.com/feed"
    assert called == ["https://huxiu.com/feed"]


def test_probe_revival_non_dict_prober_return_is_fail_closed():
    """★ 回归（review Important #1）：prober 违反契约返回非 dict（str/int/list）
    也不能让异常逃出 probe_revivals —— 之前 `(status or {}).get(...)` 只在
    prober() 调用本身包了 try，取值那行在 try 外，非 dict 真值会在这里炸
    AttributeError，正好撞上「周报不能被这个附加功能搞挂」的硬约束。
    """
    reg = _registry([
        _rejected("StrReturn", "timeout", url="https://a.example.com/feed"),
        _rejected("IntReturn", "timeout", url="https://b.example.com/feed"),
        _rejected("ListReturn", "timeout", url="https://c.example.com/feed"),
    ])

    def _bad_prober(name, url):
        return {"https://a.example.com/feed": "not a dict",
                "https://b.example.com/feed": 200,
                "https://c.example.com/feed": ["article_count", 5]}[url]

    assert _mod.probe_revivals(reg, _bad_prober) == []


def test_probe_revival_malformed_registry_is_fail_closed():
    """Minor（review）：外层 find_revival_candidates(registry) 若因畸形 registry
    抛异常，也不能让 probe_revivals 向上传播——同一条 fail-closed 口径。
    get_sources() 内部是 `registry.get("sources", [])`，传个没有 .get 的对象
    就会在那里炸 AttributeError。
    """
    def _p(name, url):
        return {"ok": True, "error": None, "article_count": 1, "newest_age_hours": 1.0}

    assert _mod.probe_revivals(object(), _p) == []


def test_probe_revival_caches_health_module_load():
    """review Important #2：默认 prober 每探一个 URL 都要 _load_health_module()，
    不缓存就是 N 个源最坏 2N+1 次重新 parse+exec rss-health-check.py。加了
    functools.lru_cache 后，多次调用应该只真正加载一次。
    """
    from unittest import mock

    _mod._load_health_module.cache_clear()
    with mock.patch.object(importlib.util, "spec_from_file_location",
                            wraps=importlib.util.spec_from_file_location) as spy:
        _mod._load_health_module()
        _mod._load_health_module()
        _mod._load_health_module()
        assert spy.call_count == 1
    _mod._load_health_module.cache_clear()
