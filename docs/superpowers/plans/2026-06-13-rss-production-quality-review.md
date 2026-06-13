# RSS Production 源在岗质量复查 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 `rss-production-review.py`——周期性读 `production-source-log.jsonl`，识别 A 僵尸源（建议 demote）+ B 内容变质（预警），发邮件报告；demote 永远人工确认。

**Architecture:** 单文件、stdlib only（与 repo 一致）。纯函数做判定（可独立测试、注入 `now` 避免时间不确定性），路径全部参数化（default 指生产、测试传 `tmp_path`，杜绝测试污染生产）。复用现有 `rss_registry` 模块、`rss-demote-source.py` 执行工具、discovery 的 curl SMTP 模式。

**Tech Stack:** Python 3.12 stdlib（json/datetime/statistics/subprocess/urllib 无关）、pytest、`~/cron-wrapper.sh`。

**Spec:** `docs/superpowers/specs/2026-06-13-rss-production-quality-review-design.md`

---

## File Structure

- **Create** `rss-production-review.py` — 评估器主脚本。职责：读 telemetry+registry → 判 A/B → 建 HTML → 发邮件。
- **Create** `tests/test_rss_production_review.py` — 全部单元测试。
- **Modify** crontab（用户级）— 加周度任务行。
- **Modify** `README.md` — 补该机制说明。

### 锁定的函数签名（task 间必须一致）

```python
BJT = timezone(timedelta(hours=8))
ENV_FILE = os.path.expanduser("~/.stock-monitor.env")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "logs", "production-source-log.jsonl")

parse_ts(ts: str) -> datetime                          # fromisoformat，带 +08:00
load_records(log_path: str) -> list[dict]              # 容错跳过坏行/裸行保留
filter_window(records: list, now: datetime, days: int) -> list
aggregate_by_source(records: list) -> dict             # {src: {"fetched","selected","active_days"}}
graduation_date(source: dict) -> "date | None"         # trial.end_date(graduated) 否则 None
tenure_days(source: dict, now: datetime) -> "int | None"
find_zombies(registry, records, now, *, window_days=30, grace_days=30,
             min_active_days=7, max_selected=1) -> list[dict]
median_or_none(xs: list) -> "float | None"
find_degraded(registry, records, now, *, recent_days=7,
              min_baseline=10, min_recent=5) -> list[dict]
snapshot_rows(registry, records, now, *, window_days=30) -> list[dict]
build_report_html(zombies, degraded, snapshot, now) -> str
send_report_email(html: str, subject: str, env_path: str = ENV_FILE) -> bool
cmd_run(registry_path=None, log_path=LOG_PATH, now=None, send=True) -> int
```

### 数据结构

- **record**: `{"ts": str, "source": str, "fetched": int, "selected": int, ["avg_title_len","avg_desc_len","pct_with_desc","pct_with_author": float]}`（裸行无后 4 项）
- **zombie**: `{"name","category": str, "fetched","selected","tenure_days": int|None}`
- **degraded**: `{"name": str, "signal": str, "baseline": float, "recent": float, "detail": str}`
- **snapshot row**: `{"name","category": str, "fetched","selected": int}`

---

## Task 1: 数据读取与窗口聚合

**Files:**
- Create: `rss-production-review.py`
- Test: `tests/test_rss_production_review.py`

- [ ] **Step 1: Write the failing test**

```python
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
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    recs = [_rec(1, "A", 3, 1), _rec(25, "A", 3, 1)]
    kept = _mod.filter_window(recs, now, 30)  # cutoff = 2026-05-31
    assert len(kept) == 1
    assert kept[0]["ts"].startswith("2026-06-25")


def test_aggregate_by_source_sums_and_active_days(tmp_path):
    recs = [_rec(1, "A", 3, 2), _rec(1, "A", 2, 1), _rec(3, "A", 4, 0), _rec(5, "B", 0, 0)]
    agg = _mod.aggregate_by_source(recs)
    assert agg["A"]["fetched"] == 9
    assert agg["A"]["selected"] == 3
    assert agg["A"]["active_days"] == 2   # 06-01 and 06-03 had fetched>0
    assert agg["B"]["active_days"] == 0   # fetched=0 day doesn't count
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -v`
Expected: FAIL — `No module named` / `module ... has no attribute 'load_records'`

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RSS Production 源在岗质量复查 — 读 telemetry，判 A 僵尸/B 变质，发邮件报告。

仅生成报告，不执行 demote（demote 由 rss-demote-source.py 人工确认后执行）。
Spec: docs/superpowers/specs/2026-06-13-rss-production-quality-review-design.md
"""
import json
import os
import sys
import base64
import subprocess
import tempfile
import statistics
from datetime import datetime, timezone, timedelta

import rss_registry as _reg

BJT = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "logs", "production-source-log.jsonl")
ENV_FILE = os.path.expanduser("~/.stock-monitor.env")


def parse_ts(ts: str) -> datetime:
    """Parse a telemetry ISO timestamp (carries +08:00 offset)."""
    return datetime.fromisoformat(ts)


def load_records(log_path: str) -> list:
    """Read JSONL, skipping blank/malformed lines. Bare rows (no metadata) kept as-is."""
    out = []
    if not os.path.isfile(log_path):
        return out
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and d.get("source"):
                out.append(d)
    return out


def filter_window(records: list, now: datetime, days: int) -> list:
    """Keep records with ts >= now - days."""
    cutoff = now - timedelta(days=days)
    kept = []
    for r in records:
        try:
            if parse_ts(r["ts"]) >= cutoff:
                kept.append(r)
        except (KeyError, ValueError):
            continue
    return kept


def aggregate_by_source(records: list) -> dict:
    """Sum fetched/selected per source; active_days = distinct dates with fetched>0."""
    agg = {}
    days_seen = {}
    for r in records:
        src = r.get("source")
        if not src:
            continue
        a = agg.setdefault(src, {"fetched": 0, "selected": 0, "active_days": 0})
        a["fetched"] += int(r.get("fetched", 0) or 0)
        a["selected"] += int(r.get("selected", 0) or 0)
        if int(r.get("fetched", 0) or 0) > 0:
            days_seen.setdefault(src, set()).add(r.get("ts", "")[:10])
    for src, dates in days_seen.items():
        agg[src]["active_days"] = len(dates)
    return agg
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "feat(prod-review): log reading + window aggregation"
```

---

## Task 2: 转正日期与在岗天数

**Files:**
- Modify: `rss-production-review.py`
- Test: `tests/test_rss_production_review.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k "graduation or tenure" -v`
Expected: FAIL — `has no attribute 'graduation_date'`

- [ ] **Step 3: Write minimal implementation** (append to `rss-production-review.py`)

```python
def graduation_date(source: dict):
    """Return date a source graduated from trial, or None for legacy/non-trial sources."""
    t = source.get("trial")
    if isinstance(t, dict) and t.get("outcome") in ("graduated", "auto-graduated") and t.get("end_date"):
        try:
            return datetime.strptime(t["end_date"], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def tenure_days(source: dict, now: datetime):
    """Days since graduation; None if legacy (no graduation date → treated as long-tenured)."""
    g = graduation_date(source)
    if g is None:
        return None
    return (now.date() - g).days
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k "graduation or tenure" -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "feat(prod-review): graduation date + tenure"
```

---

## Task 3: A 僵尸源判定

**Files:**
- Modify: `rss-production-review.py`
- Test: `tests/test_rss_production_review.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k zombie -v`
Expected: FAIL — `has no attribute 'find_zombies'`

- [ ] **Step 3: Write minimal implementation** (append)

```python
def find_zombies(registry, records, now, *, window_days=30, grace_days=30,
                 min_active_days=7, max_selected=1) -> list:
    """A: production sources still publishing (fetched>0) but ~never selected.

    Skips: non-production, in-grace (tenure<grace_days), insufficient sample
    (active_days<min_active_days), and dead feeds (fetched==0 → health-check's job).
    """
    windowed = filter_window(records, now, window_days)
    agg = aggregate_by_source(windowed)
    zombies = []
    for s in _reg.get_by_status(registry, "production"):
        name = s.get("name")
        a = agg.get(name)
        if not a or a["fetched"] <= 0:            # dead/never-seen → not a zombie
            continue
        t = tenure_days(s, now)
        if t is not None and t < grace_days:       # in grace
            continue
        if a["active_days"] < min_active_days:      # insufficient sample (low-freq safety)
            continue
        if a["selected"] <= max_selected:
            zombies.append({
                "name": name,
                "category": s.get("category", "?"),
                "fetched": a["fetched"],
                "selected": a["selected"],
                "tenure_days": t,
            })
    return zombies
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k zombie -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "feat(prod-review): A zombie detection with grace + sample gates"
```

---

## Task 4: B 内容变质判定

**Files:**
- Modify: `rss-production-review.py`
- Test: `tests/test_rss_production_review.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k degraded -v`
Expected: FAIL — `has no attribute 'find_degraded'`

- [ ] **Step 3: Write minimal implementation** (append)

```python
def median_or_none(xs: list):
    vals = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(vals) if vals else None


def _meta_series(records, source, field):
    return [r[field] for r in records if r.get("source") == source and field in r]


def find_degraded(registry, records, now, *, recent_days=7,
                  min_baseline=10, min_recent=5) -> list:
    """B: content-quality drift vs the source's OWN baseline (never absolute thresholds).

    baseline = records older than recent_days; recent = last recent_days. Warning only.
    """
    cutoff = now - timedelta(days=recent_days)
    baseline_recs, recent_recs = [], []
    for r in records:
        try:
            ts = parse_ts(r["ts"])
        except (KeyError, ValueError):
            continue
        (recent_recs if ts >= cutoff else baseline_recs).append(r)

    out = []
    for s in _reg.get_by_status(registry, "production"):
        name = s.get("name")
        for field, check, label in (
            ("pct_with_desc",
             lambda b, r: b is not None and r is not None and b > 0.8 and r < 0.3,
             "desc-collapse"),
            ("avg_desc_len",
             lambda b, r: b is not None and r is not None and b > 0 and r < b * 0.4,
             "desc-len-shrink"),
            ("pct_with_author",
             lambda b, r: b is not None and r is not None and b > 0.5 and r < b * 0.5,
             "author-drop"),
        ):
            b_series = _meta_series(baseline_recs, name, field)
            r_series = _meta_series(recent_recs, name, field)
            if len(b_series) < min_baseline or len(r_series) < min_recent:
                continue
            b, r = median_or_none(b_series), median_or_none(r_series)
            if check(b, r):
                out.append({"name": name, "signal": field + ":" + label,
                            "baseline": round(b, 2), "recent": round(r, 2),
                            "detail": f"{field} {b:.2f} → {r:.2f}"})
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k degraded -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "feat(prod-review): B content-degradation vs own baseline"
```

---

## Task 5: 全池快照 + 报告 HTML

**Files:**
- Modify: `rss-production-review.py`
- Test: `tests/test_rss_production_review.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k "snapshot or build_report" -v`
Expected: FAIL — `has no attribute 'snapshot_rows'`

- [ ] **Step 3: Write minimal implementation** (append)

```python
def snapshot_rows(registry, records, now, *, window_days=30) -> list:
    """All production sources' 30d fetched/selected, for transparency in the report."""
    agg = aggregate_by_source(filter_window(records, now, window_days))
    rows = []
    for s in _reg.get_by_status(registry, "production"):
        a = agg.get(s.get("name"), {"fetched": 0, "selected": 0})
        rows.append({"name": s.get("name"), "category": s.get("category", "?"),
                     "fetched": a["fetched"], "selected": a["selected"]})
    rows.sort(key=lambda r: r["selected"])
    return rows


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_report_html(zombies, degraded, snapshot, now) -> str:
    """Full HTML report: A zombie candidates (with demote command), B warnings, pool snapshot."""
    ts = now.strftime("%Y-%m-%d %H:%M BJT")

    if zombies:
        z_rows = "".join(
            f"<tr><td>{_esc(z['name'])}</td><td>{_esc(z['category'])}</td>"
            f"<td style='text-align:center'>{z['fetched']}</td>"
            f"<td style='text-align:center'>{z['selected']}</td>"
            f"<td style='text-align:center'>{z['tenure_days'] if z['tenure_days'] is not None else 'legacy'}</td>"
            f"<td><code>python3 ~/global-news/rss-demote-source.py --name \"{_esc(z['name'])}\" "
            f"--reason \"zombie-30d-no-selected\"</code></td></tr>"
            for z in zombies)
        a_section = (f"<h3>🧟 A — 僵尸源候选（{len(zombies)}）建议 demote（确认后执行）</h3>"
                     "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                     "<tr style='background:#f3f4f6'><th>源</th><th>类别</th><th>30d 抓取</th>"
                     "<th>30d 入选</th><th>在岗天</th><th>确认后执行</th></tr>"
                     f"{z_rows}</table>")
    else:
        a_section = "<h3>🧟 A — 僵尸源候选</h3><p>无。</p>"

    if degraded:
        d_rows = "".join(
            f"<tr><td>{_esc(d['name'])}</td><td>{_esc(d['signal'])}</td>"
            f"<td style='text-align:center'>{_esc(d['detail'])}</td></tr>" for d in degraded)
        b_section = (f"<h3>⚠️ B — 内容变质预警（{len(degraded)}）仅供人工判断</h3>"
                     "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                     "<tr style='background:#fff8e1'><th>源</th><th>信号</th><th>基线 → 近期</th></tr>"
                     f"{d_rows}</table>")
    else:
        b_section = "<h3>⚠️ B — 内容变质预警</h3><p>无。</p>"

    snap_rows = "".join(
        f"<tr><td>{_esc(r['name'])}</td><td>{_esc(r['category'])}</td>"
        f"<td style='text-align:center'>{r['fetched']}</td>"
        f"<td style='text-align:center'>{r['selected']}</td></tr>" for r in snapshot)
    snap_section = ("<h3>📊 全池 30 天贡献快照</h3>"
                    "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                    "<tr style='background:#f3f4f6'><th>源</th><th>类别</th><th>30d 抓取</th><th>30d 入选</th></tr>"
                    f"{snap_rows}</table>")

    return (f"<h2>RSS Production 源在岗质量复查</h2><p>生成：{ts}</p>"
            f"{a_section}{b_section}{snap_section}")
```

> 注：`build_report_html` 只产出 HTML body。`MIME-Version` 头属于邮件信封，由 `send_report_email`（Task 6）拼装，并在 Task 6 的 `test_send_report_email_builds_mime` 中断言——故本 task 不测 MIME 头。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k "snapshot or build_report" -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "feat(prod-review): pool snapshot + HTML report builder"
```

---

## Task 6: 邮件发送 + cmd_run 编排 + CLI

**Files:**
- Modify: `rss-production-review.py`
- Test: `tests/test_rss_production_review.py`

- [ ] **Step 1: Write the failing test**

```python
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
        # 读取 upload-file 内容验证 MIME 头
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k "cmd_run or send_report" -v`
Expected: FAIL — `has no attribute 'cmd_run'` / `'_load_env'`

- [ ] **Step 3: Write minimal implementation** (append)

```python
def _load_env(path: str = ENV_FILE) -> dict:
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def send_report_email(html: str, subject: str, env_path: str = ENV_FILE) -> bool:
    """Send the HTML report via curl SMTP (same pattern as discovery)."""
    env = _load_env(env_path)
    mail_to = env.get("MAIL_TO", "")
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")
    if not all([mail_to, smtp_user, smtp_pass]):
        print("Missing SMTP credentials", file=sys.stderr)
        return False
    subject_b64 = base64.b64encode(subject.encode("utf-8")).decode("ascii")
    msg_id = f"<rss-prod-review-{datetime.now(BJT).strftime('%Y%m%d%H%M%S')}-{os.getpid()}@ec2.sinostor.com.cn>"
    content = (f'From: "RSS Pool Review" <{smtp_user}>\r\n'
               f"To: {mail_to}\r\nMessage-ID: {msg_id}\r\n"
               f"Subject: =?UTF-8?B?{subject_b64}?=\r\n"
               f"Content-Type: text/html; charset=UTF-8\r\nMIME-Version: 1.0\r\n\r\n{html}")
    fd, mail_file = tempfile.mkstemp(suffix=".eml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        r = subprocess.run(
            ["curl", "--silent", "--ssl-reqd", "--max-time", "30",
             "--url", f"smtps://{env.get('SMTP_SERVER', 'smtp.163.com')}:{env.get('SMTP_PORT', '465')}",
             "--user", f"{smtp_user}:{smtp_pass}", "--mail-from", smtp_user,
             "--mail-rcpt", mail_to, "--upload-file", mail_file],
            capture_output=True, text=True, timeout=45)
        if r.returncode == 0:
            print(f"Report email sent to {mail_to}")
            return True
        print(f"Email send failed: {r.stderr}", file=sys.stderr)
        return False
    finally:
        if os.path.exists(mail_file):
            os.unlink(mail_file)


def cmd_run(registry_path=None, log_path: str = LOG_PATH, now=None, send: bool = True) -> int:
    registry = _reg.load_registry(registry_path)
    if now is None:
        now = datetime.now(BJT)
    records = load_records(log_path)
    zombies = find_zombies(registry, records, now)
    degraded = find_degraded(registry, records, now)
    snapshot = snapshot_rows(registry, records, now)
    html = build_report_html(zombies, degraded, snapshot, now)
    subject = (f"[RSS Pool 复查] {len(zombies)} 僵尸候选 / {len(degraded)} 变质预警 "
               f"— {now.strftime('%m月%d日')}")
    print(f"[prod-review] {len(zombies)} zombies, {len(degraded)} degraded, "
          f"{len(snapshot)} sources reviewed.")
    if send:
        if not send_report_email(html, subject):
            return 1
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        return cmd_run()
    print(f"Usage: {os.path.basename(__file__)} run", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -v`
Expected: PASS (all, ~19)

- [ ] **Step 5: Commit**

```bash
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "feat(prod-review): email send + cmd_run orchestration + CLI"
```

---

## Task 7: 真实数据 smoke test + cron 接线 + 文档

**Files:**
- Modify: crontab（用户级）
- Modify: `README.md`

- [ ] **Step 1: 真实数据 dry-run（不发邮件）**

Run:
```bash
cd ~/global-news && python3 -c "
import importlib.util, datetime
s=importlib.util.spec_from_file_location('m','rss-production-review.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
import rss_registry as reg
r=reg.load_registry(); recs=m.load_records(m.LOG_PATH)
now=datetime.datetime.now(m.BJT)
z=m.find_zombies(r,recs,now); d=m.find_degraded(r,recs,now)
print('zombies:', [x[\"name\"] for x in z])
print('degraded:', [x[\"name\"] for x in d])
print('snapshot sources:', len(m.snapshot_rows(r,recs,now)))
"
```
Expected: 不报错；打印僵尸/变质列表（17 天数据下 A 多半为空，正常）。

- [ ] **Step 2: 一次性发一封真实报告（验证邮件格式）**

Run: `cd ~/global-news && python3 rss-production-review.py run`
Expected: `Report email sent to ...`；检查邮箱 ch_w10@outlook.com 收到、表格/命令/快照渲染正常、无重复 style。

- [ ] **Step 3: 加 cron 周度任务**

Run:
```bash
( crontab -l; echo "# RSS production pool quality review — weekly Sun 09:30 BJT (01:30 UTC)"; \
  echo "30 1 * * 0 ~/cron-wrapper.sh --name rss-production-review --timeout 300 --lock -- python3 ~/global-news/rss-production-review.py run >> ~/logs/rss-production-review.log 2>&1" ) | crontab -
crontab -l | grep rss-production-review
```
Expected: 新行出现在 crontab 中。

- [ ] **Step 4: 更新 README**

在 `README.md` 源治理章节追加：
```markdown
- **rss-production-review.py** — production 源在岗质量周度复查（周日 09:30 BJT）。读 production-source-log.jsonl：A 僵尸源（30d 内 fetched>0 但 selected≤1、在岗≥30d、active_days≥7）建议 demote；B 内容变质（pct_with_desc/avg_desc_len/pct_with_author 相对自身基线漂移）预警。仅报告，demote 经人工确认后跑 rss-demote-source.py。Spec: docs/superpowers/specs/2026-06-13-rss-production-quality-review-design.md
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs(prod-review): wire weekly cron + README"
git push
```

---

## Self-Review

**Spec coverage:**
- A 僵尸（30d/selected≤1/宽限30d/样本7d/fetched=0 归 health-check）→ Task 3 ✅
- B 变质（自身基线、3 信号、最小样本）→ Task 4 ✅
- 邮件报告 + 全池快照 + demote 命令 → Task 5/6 ✅
- demote 人工（复用 rss-demote-source.py，本机制不写配置）→ 全程 ✅
- 测试期周度 cron → Task 7 ✅
- 17 天数据约束 → Task 7 Step 1 验证预期 ✅
- 转正日期 = trial.end_date / legacy=None → Task 2 ✅

**Placeholder scan:** 无 TBD/TODO；每个代码 step 含完整代码。Task 5 的 `MIME-Version` 断言已在"注"中明确修正（移到 Task 6 信封）。

**Type consistency:** `find_zombies`/`find_degraded`/`snapshot_rows`/`build_report_html`/`cmd_run`/`send_report_email` 签名与首部"锁定签名"一致；zombie/degraded/snapshot dict 字段在 Task 3/4/5/6 间一致（name/category/fetched/selected/tenure_days；name/signal/baseline/recent/detail）。

**测试污染防护:** registry/log 全走 tmp_path；cmd_run 测试 monkeypatch send + `_reg.REGISTRY_FILE`；send 测试 monkeypatch subprocess.run，不真发邮件、不写生产。
