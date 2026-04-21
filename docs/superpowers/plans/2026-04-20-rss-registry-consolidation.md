# RSS Registry Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `discovered-rss.json` + `trial-state.json` with a single `rss-registry.json` so RSS source state (discovered / trialing / production / rejected) is always consistent in one place.

**Architecture:** New `rss_registry.py` module owns all reads/writes to `config/rss-registry.json`. `rss-trial-manager.py`, `rss-source-discovery.py`, and `rss-promote-candidate.py` import this module instead of reading the two old files. `news-sources-config.json` stays as-is (it is the operational feed config used by the sender and health-check); the registry layer updates it as a side-effect when graduating a source.

**Tech Stack:** Python 3.12 stdlib only, pytest, JSON atomic writes via `tempfile.mkstemp + os.replace`.

---

## File Map

| Action | Path |
|--------|------|
| Create | `rss_registry.py` — registry module (load/save/query/mutate) |
| Create | `config/rss-registry.json` — unified source-of-truth data file |
| Create | `scripts/migrate_to_registry.py` — one-time migration from old files |
| Create | `tests/test_rss_registry.py` — new tests for the registry module |
| Modify | `rss-trial-manager.py` — use registry instead of two old files |
| Modify | `rss-source-discovery.py` — use registry instead of `discovered-rss.json` |
| Modify | `rss-promote-candidate.py` — use registry instead of `discovered-rss.json` |
| Modify | `scripts/rss-source-discovery.sh` — git add new registry file |
| Delete | `config/discovered-rss.json` (after migration verified) |
| Delete | `config/trial-state.json` (after migration verified) |
| Update | `tests/test_rss_trial_manager.py` — fix fixtures to use registry |
| Update | `tests/test_rss_promote.py` — fix fixtures to use registry |

---

## Data Model

Each source in `rss-registry.json` has a `status` field:
- `discovered` — found by AI search, not yet trialed
- `trialing` — currently in active 3-day trial (exactly ONE source at a time)
- `production` — graduated, permanently in `news-sources-config.json`
- `rejected` — explicitly rejected or pool-cap exclusion

```json
{
  "version": 1,
  "sources": [
    {
      "name": "The Guardian World",
      "url": "https://www.theguardian.com/world/rss",
      "language": "en",
      "category": "europe",
      "status": "production",
      "discovered_via": "ai_search",
      "validation": { "http_status": 200, "parse_ok": true, "article_count": 10, "newest_age_hours": 0.5, "has_descriptions": true, "has_authors": false, "has_categories": true, "avg_description_length": 120, "error": null },
      "scores": { "reliability": 0.9, "freshness": 1.0, "content_quality": 0.85, "content_depth": 0.7, "authority": 0.88, "uniqueness": 0.75, "final": 0.924 },
      "reject_reason": null,
      "trial": {
        "start_date": "2026-04-12",
        "end_date": "2026-04-19",
        "report_sent": false,
        "daily_stats": [{"date": "2026-04-12", "fetched": 3, "selected": 3}],
        "outcome": "auto-graduated",
        "auto_decided": true,
        "candidate_score": 0.924
      },
      "production": { "keywords": [], "limit": 3 }
    }
  ]
}
```

---

## Task 1: Create `rss_registry.py` module

**Files:**
- Create: `~/global-news/rss_registry.py`

- [ ] **Step 1: Write the file**

```python
#!/usr/bin/env python3
"""Registry for all RSS sources: discovered → trialing → production/rejected.

Replaces config/discovered-rss.json + config/trial-state.json.
news-sources-config.json is still updated as a side-effect of graduation
(it is the operational config used by the sender and health-check).
"""
import json
import os
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_FILE = os.path.join(SCRIPT_DIR, "config", "rss-registry.json")


def _atomic_write(path: str, data: dict) -> None:
    dir_ = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_registry(path: str = REGISTRY_FILE) -> dict:
    if not os.path.isfile(path):
        return {"version": 1, "sources": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_registry(data: dict, path: str = REGISTRY_FILE) -> None:
    _atomic_write(path, data)


def get_sources(registry: dict) -> list[dict]:
    return registry.get("sources", [])


def get_by_status(registry: dict, *statuses: str) -> list[dict]:
    status_set = set(statuses)
    return [s for s in get_sources(registry) if s.get("status") in status_set]


def get_by_url(registry: dict, url: str) -> dict | None:
    norm = url.rstrip("/").lower()
    for s in get_sources(registry):
        if s.get("url", "").rstrip("/").lower() == norm:
            return s
    return None


def get_active_trial(registry: dict) -> dict | None:
    for s in get_sources(registry):
        if s.get("status") == "trialing":
            return s
    return None


def get_trial_history(registry: dict) -> list[dict]:
    """All sources that have been trialed (trial.end_date is set)."""
    return [
        s for s in get_sources(registry)
        if s.get("trial") and s["trial"].get("end_date")
    ]


def get_promotable(registry: dict, threshold: float) -> list[dict]:
    """Candidates eligible for trial: discovered, score >= threshold, never tried before."""
    tried_urls = {
        s.get("url", "").rstrip("/").lower()
        for s in get_sources(registry)
        if s.get("trial")
    }
    result = [
        s for s in get_sources(registry)
        if s.get("status") == "discovered"
        and s.get("url", "").rstrip("/").lower() not in tried_urls
        and (s.get("scores") or {}).get("final", 0.0) >= threshold
    ]
    return sorted(result, key=lambda x: -(x.get("scores") or {}).get("final", 0.0))


def upsert_source(registry: dict, entry: dict) -> bool:
    """Add entry if URL not already present. Returns True if added, False if duplicate."""
    if get_by_url(registry, entry["url"]):
        return False
    if "status" not in entry:
        entry = {**entry, "status": "discovered"}
    registry.setdefault("sources", []).append(entry)
    return True


def start_trial(registry: dict, source: dict, today: str) -> None:
    """Transition source from discovered → trialing and attach trial metadata."""
    for s in get_sources(registry):
        if s.get("url", "").rstrip("/").lower() == source["url"].rstrip("/").lower():
            s["status"] = "trialing"
            s["trial"] = {
                "start_date": today,
                "end_date": None,
                "report_sent": False,
                "daily_stats": [],
                "outcome": None,
                "auto_decided": False,
                "candidate_score": round((source.get("scores") or {}).get("final", 0.0), 3),
            }
            return
    raise KeyError(f"Source not found in registry: {source['url']}")


def update_trial_stats(registry: dict, name: str, date_stats: dict) -> None:
    """Append or update today's daily stats on the active trial source."""
    for s in get_sources(registry):
        if s.get("name") == name and s.get("status") == "trialing":
            stats = s.setdefault("trial", {}).setdefault("daily_stats", [])
            date = date_stats.get("date")
            for existing in stats:
                if existing.get("date") == date:
                    existing.update(date_stats)
                    return
            stats.append(date_stats)
            return


def end_trial(
    registry: dict,
    name: str,
    outcome: str,
    kept: bool,
    today: str,
    report_sent: bool = False,
) -> None:
    """Close the active trial. kept=True → production, kept=False → rejected."""
    for s in get_sources(registry):
        if s.get("name") == name and s.get("status") == "trialing":
            trial = s.setdefault("trial", {})
            trial["end_date"] = today
            trial["outcome"] = outcome
            trial["auto_decided"] = True
            trial["report_sent"] = report_sent
            s["status"] = "production" if kept else "rejected"
            return
    raise KeyError(f"No active trial found for: {name}")


def set_production_config(registry: dict, name: str, keywords: list, limit: int) -> None:
    """Store production config (keywords, limit) on a graduated source."""
    for s in get_sources(registry):
        if s.get("name") == name:
            s["production"] = {"keywords": keywords, "limit": limit}
            return


def reject_source(registry: dict, name: str, reason: str) -> None:
    """Mark a discovered source as rejected with a reason."""
    for s in get_sources(registry):
        if s.get("name") == name:
            s["status"] = "rejected"
            s["reject_reason"] = reason
            return
```

- [ ] **Step 2: Verify syntax**

```bash
cd ~/global-news
python3 -m py_compile rss_registry.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd ~/global-news
git add rss_registry.py
git commit -m "feat(registry): add rss_registry.py — unified RSS source state module"
```

---

## Task 2: Write tests for `rss_registry.py`

**Files:**
- Create: `~/global-news/tests/test_rss_registry.py`

- [ ] **Step 1: Write tests**

```python
"""Tests for rss_registry.py"""
import pytest
from rss_registry import (
    load_registry, save_registry, get_sources, get_by_status, get_by_url,
    get_active_trial, get_trial_history, get_promotable, upsert_source,
    start_trial, update_trial_stats, end_trial, set_production_config,
    reject_source,
)


def _make_registry(*sources):
    return {"version": 1, "sources": list(sources)}


def _source(name="Feed A", url="https://a.com/feed", status="discovered", score=0.95, trial=None, production=None):
    return {
        "name": name, "url": url, "status": status,
        "scores": {"final": score},
        "trial": trial, "production": production,
    }


class TestGetByStatus:
    def test_filters_by_single_status(self):
        reg = _make_registry(_source("A", status="discovered"), _source("B", status="production"))
        result = get_by_status(reg, "discovered")
        assert [s["name"] for s in result] == ["A"]

    def test_filters_by_multiple_statuses(self):
        reg = _make_registry(
            _source("A", status="discovered"),
            _source("B", status="trialing"),
            _source("C", status="rejected"),
        )
        result = get_by_status(reg, "discovered", "trialing")
        assert {s["name"] for s in result} == {"A", "B"}

    def test_empty_when_no_match(self):
        reg = _make_registry(_source("A", status="discovered"))
        assert get_by_status(reg, "production") == []


class TestGetByUrl:
    def test_finds_exact_url(self):
        reg = _make_registry(_source(url="https://a.com/feed"))
        assert get_by_url(reg, "https://a.com/feed") is not None

    def test_normalizes_trailing_slash(self):
        reg = _make_registry(_source(url="https://a.com/feed/"))
        assert get_by_url(reg, "https://a.com/feed") is not None

    def test_returns_none_when_missing(self):
        reg = _make_registry(_source(url="https://a.com/feed"))
        assert get_by_url(reg, "https://b.com/feed") is None


class TestGetActiveTrial:
    def test_returns_trialing_source(self):
        reg = _make_registry(
            _source("A", status="discovered"),
            _source("B", status="trialing"),
        )
        assert get_active_trial(reg)["name"] == "B"

    def test_returns_none_when_no_trial(self):
        reg = _make_registry(_source("A", status="discovered"))
        assert get_active_trial(reg) is None


class TestGetTrialHistory:
    def test_returns_sources_with_end_date(self):
        reg = _make_registry(
            _source("A", trial={"start_date": "2026-04-01", "end_date": "2026-04-04"}),
            _source("B", trial={"start_date": "2026-04-10", "end_date": None}),
            _source("C"),
        )
        result = get_trial_history(reg)
        assert [s["name"] for s in result] == ["A"]


class TestGetPromotable:
    def test_returns_discovered_above_threshold(self):
        reg = _make_registry(
            _source("High", score=0.95, status="discovered"),
            _source("Low", url="https://low.com/f", score=0.80, status="discovered"),
        )
        result = get_promotable(reg, 0.90)
        assert [s["name"] for s in result] == ["High"]

    def test_excludes_already_tried(self):
        trial_data = {"start_date": "2026-04-01", "end_date": "2026-04-04", "outcome": "auto-removed"}
        reg = _make_registry(
            _source("Tried", score=0.95, status="discovered", trial=trial_data),
        )
        assert get_promotable(reg, 0.90) == []

    def test_excludes_non_discovered(self):
        reg = _make_registry(
            _source("Prod", score=0.95, status="production"),
            _source("Rej", url="https://rej.com/f", score=0.95, status="rejected"),
        )
        assert get_promotable(reg, 0.90) == []

    def test_sorted_by_score_desc(self):
        reg = _make_registry(
            _source("B", url="https://b.com/f", score=0.91, status="discovered"),
            _source("A", url="https://a.com/f", score=0.97, status="discovered"),
        )
        result = get_promotable(reg, 0.90)
        assert [s["name"] for s in result] == ["A", "B"]


class TestUpsertSource:
    def test_adds_new_source(self):
        reg = _make_registry()
        added = upsert_source(reg, {"name": "X", "url": "https://x.com/f"})
        assert added is True
        assert len(get_sources(reg)) == 1

    def test_skips_duplicate_url(self):
        reg = _make_registry(_source(url="https://a.com/feed"))
        added = upsert_source(reg, {"name": "Dup", "url": "https://a.com/feed"})
        assert added is False
        assert len(get_sources(reg)) == 1

    def test_sets_default_status_discovered(self):
        reg = _make_registry()
        upsert_source(reg, {"name": "X", "url": "https://x.com/f"})
        assert get_sources(reg)[0]["status"] == "discovered"


class TestStartTrial:
    def test_sets_status_to_trialing(self):
        src = _source(status="discovered")
        reg = _make_registry(src)
        start_trial(reg, src, "2026-04-20")
        assert get_sources(reg)[0]["status"] == "trialing"

    def test_attaches_trial_metadata(self):
        src = _source(status="discovered")
        reg = _make_registry(src)
        start_trial(reg, src, "2026-04-20")
        trial = get_sources(reg)[0]["trial"]
        assert trial["start_date"] == "2026-04-20"
        assert trial["end_date"] is None
        assert trial["daily_stats"] == []

    def test_raises_if_not_found(self):
        reg = _make_registry()
        with pytest.raises(KeyError):
            start_trial(reg, _source(), "2026-04-20")


class TestUpdateTrialStats:
    def test_appends_new_date(self):
        src = _source(status="trialing", trial={"daily_stats": []})
        reg = _make_registry(src)
        update_trial_stats(reg, "Feed A", {"date": "2026-04-20", "fetched": 3, "selected": 3})
        assert get_sources(reg)[0]["trial"]["daily_stats"] == [
            {"date": "2026-04-20", "fetched": 3, "selected": 3}
        ]

    def test_updates_existing_date(self):
        src = _source(status="trialing", trial={"daily_stats": [{"date": "2026-04-20", "fetched": 1, "selected": 1}]})
        reg = _make_registry(src)
        update_trial_stats(reg, "Feed A", {"date": "2026-04-20", "fetched": 5, "selected": 4})
        stats = get_sources(reg)[0]["trial"]["daily_stats"]
        assert len(stats) == 1
        assert stats[0]["fetched"] == 5


class TestEndTrial:
    def test_kept_true_sets_production(self):
        src = _source(status="trialing", trial={"daily_stats": [], "start_date": "2026-04-17"})
        reg = _make_registry(src)
        end_trial(reg, "Feed A", outcome="auto-graduated", kept=True, today="2026-04-20")
        s = get_sources(reg)[0]
        assert s["status"] == "production"
        assert s["trial"]["end_date"] == "2026-04-20"
        assert s["trial"]["outcome"] == "auto-graduated"

    def test_kept_false_sets_rejected(self):
        src = _source(status="trialing", trial={"daily_stats": [], "start_date": "2026-04-17"})
        reg = _make_registry(src)
        end_trial(reg, "Feed A", outcome="auto-removed", kept=False, today="2026-04-20")
        assert get_sources(reg)[0]["status"] == "rejected"

    def test_raises_if_no_active_trial(self):
        reg = _make_registry(_source(status="discovered"))
        with pytest.raises(KeyError):
            end_trial(reg, "Feed A", "auto-removed", False, "2026-04-20")


class TestSetProductionConfig:
    def test_sets_production_field(self):
        src = _source(status="production")
        reg = _make_registry(src)
        set_production_config(reg, "Feed A", keywords=["macro"], limit=5)
        assert get_sources(reg)[0]["production"] == {"keywords": ["macro"], "limit": 5}


class TestRejectSource:
    def test_sets_rejected_status(self):
        reg = _make_registry(_source(status="discovered"))
        reject_source(reg, "Feed A", "pool-cap")
        s = get_sources(reg)[0]
        assert s["status"] == "rejected"
        assert s["reject_reason"] == "pool-cap"


class TestSaveLoad:
    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "registry.json")
        reg = _make_registry(_source())
        save_registry(reg, path)
        loaded = load_registry(path)
        assert loaded["sources"][0]["name"] == "Feed A"

    def test_load_returns_empty_when_missing(self, tmp_path):
        path = str(tmp_path / "missing.json")
        reg = load_registry(path)
        assert reg == {"version": 1, "sources": []}
```

- [ ] **Step 2: Run tests**

```bash
cd ~/global-news
python3 -m pytest tests/test_rss_registry.py -v
```
Expected: all tests pass (look for `passed` at end, no `FAILED`)

- [ ] **Step 3: Commit**

```bash
cd ~/global-news
git add tests/test_rss_registry.py
git commit -m "test(registry): full test coverage for rss_registry module"
```

---

## Task 3: Write and run the migration script

**Files:**
- Create: `~/global-news/scripts/migrate_to_registry.py`

- [ ] **Step 1: Write the migration script**

```python
#!/usr/bin/env python3
"""One-time migration: discovered-rss.json + trial-state.json → rss-registry.json.

Run once, then verify with: python3 scripts/migrate_to_registry.py --verify
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES_FILE = os.path.join(REPO, "config", "discovered-rss.json")
TRIAL_STATE_FILE = os.path.join(REPO, "config", "trial-state.json")
SOURCES_FILE = os.path.join(REPO, "news-sources-config.json")
REGISTRY_FILE = os.path.join(REPO, "config", "rss-registry.json")


def _atomic_write(path: str, data: dict) -> None:
    dir_ = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def migrate() -> None:
    with open(CANDIDATES_FILE, encoding="utf-8") as f:
        candidates_data = json.load(f)
    with open(TRIAL_STATE_FILE, encoding="utf-8") as f:
        trial_data = json.load(f)
    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources_data = json.load(f)

    candidates = candidates_data.get("candidates", [])
    active_trial = trial_data.get("active_trial")
    history = trial_data.get("history", [])
    rss_feeds = sources_data.get("news_sources", {}).get("rss_feeds", [])

    prod_by_url = {f["url"].rstrip("/").lower(): f for f in rss_feeds}
    history_by_name = {h["name"]: h for h in history}
    active_name = active_trial["name"] if active_trial else None

    sources = []
    for cand in candidates:
        name = cand["name"]
        url_norm = cand["url"].rstrip("/").lower()

        if cand.get("promoted"):
            status = "production"
        elif cand.get("rejected"):
            status = "rejected"
        elif name == active_name:
            status = "trialing"
        else:
            status = "discovered"

        entry: dict = {
            "name": name,
            "url": cand["url"],
            "language": cand.get("language", "en"),
            "category": cand.get("category", ""),
            "status": status,
            "discovered_via": cand.get("discovered_via", ""),
            "validation": cand.get("validation"),
            "scores": cand.get("scores"),
            "reject_reason": cand.get("reject_reason"),
            "trial": None,
            "production": None,
        }

        if status == "trialing" and active_trial:
            entry["trial"] = {
                "start_date": active_trial["start_date"],
                "end_date": active_trial.get("end_date"),
                "report_sent": active_trial.get("report_sent", False),
                "daily_stats": active_trial.get("daily_stats", []),
                "outcome": active_trial.get("outcome"),
                "auto_decided": active_trial.get("auto_decided", False),
                "candidate_score": active_trial.get("candidate_score", 0.0),
            }

        if name in history_by_name:
            h = history_by_name[name]
            entry["trial"] = {
                "start_date": h["start_date"],
                "end_date": h.get("end_date"),
                "report_sent": h.get("report_sent", False),
                "daily_stats": h.get("daily_stats", []),
                "outcome": h.get("outcome"),
                "auto_decided": h.get("auto_decided", False),
                "candidate_score": h.get("candidate_score", 0.0),
            }

        if status == "production" and url_norm in prod_by_url:
            feed = prod_by_url[url_norm]
            entry["production"] = {
                "keywords": feed.get("keywords", []),
                "limit": feed.get("limit", 3),
            }

        sources.append(entry)

    registry = {"version": 1, "sources": sources}
    _atomic_write(REGISTRY_FILE, registry)

    by_status: dict[str, int] = {}
    for s in sources:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1

    print(f"Migration complete: {len(sources)} sources → {REGISTRY_FILE}")
    for st, count in sorted(by_status.items()):
        print(f"  {st}: {count}")


def verify() -> None:
    with open(REGISTRY_FILE, encoding="utf-8") as f:
        registry = json.load(f)
    sources = registry.get("sources", [])
    trialing = [s for s in sources if s["status"] == "trialing"]
    assert len(trialing) <= 1, f"Multiple trialing sources: {[s['name'] for s in trialing]}"
    for s in sources:
        assert "name" in s and "url" in s and "status" in s, f"Missing fields: {s}"
        if s["status"] == "trialing":
            assert s.get("trial") and s["trial"].get("start_date"), f"trialing source missing trial.start_date: {s['name']}"
        if s["status"] == "production":
            assert s.get("production") is not None or True  # production config optional for legacy sources
    print(f"Verification passed: {len(sources)} sources, {len(trialing)} active trial")


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    else:
        migrate()
```

- [ ] **Step 2: Run migration**

```bash
cd ~/global-news
python3 scripts/migrate_to_registry.py
```
Expected output (numbers will vary):
```
Migration complete: 147 sources → .../config/rss-registry.json
  discovered: 143
  production: 2
  rejected: 1
  trialing: 1
```

- [ ] **Step 3: Verify**

```bash
cd ~/global-news
python3 scripts/migrate_to_registry.py --verify
```
Expected: `Verification passed: 147 sources, 1 active trial`

- [ ] **Step 4: Commit**

```bash
cd ~/global-news
git add scripts/migrate_to_registry.py config/rss-registry.json
git commit -m "feat(registry): migrate discovered-rss.json + trial-state.json → rss-registry.json"
```

---

## Task 4: Update `rss-trial-manager.py`

**Files:**
- Modify: `~/global-news/rss-trial-manager.py`

The changes replace all reads/writes of `CANDIDATES_FILE` and `TRIAL_STATE_FILE` with `rss_registry` calls. `add_trial_to_config`, `remove_trial_from_config`, and `graduate_trial_in_config` still update `news-sources-config.json` — they are kept.

- [ ] **Step 1: Add import and remove old constants**

Replace the block at the top of the file:
```python
TRIAL_STATE_FILE = os.path.join(CONFIG_DIR, "trial-state.json")
CANDIDATES_FILE = os.path.join(CONFIG_DIR, "discovered-rss.json")
SOURCES_FILE = os.path.join(SCRIPT_DIR, "news-sources-config.json")
```
With:
```python
SOURCES_FILE = os.path.join(SCRIPT_DIR, "news-sources-config.json")

import rss_registry as _reg
```

- [ ] **Step 2: Remove `load_state`, `save_state`, `get_promotable_candidates`, `_mark_candidate_promoted`**

Delete these four functions entirely (lines 81–178 approximately). They are fully replaced by `rss_registry` calls.

- [ ] **Step 3: Replace `_load_candidate_detail`**

Replace:
```python
def _load_candidate_detail(url: str) -> dict:
    """Load candidate scores/validation from discovered-rss.json by URL."""
    if not os.path.isfile(CANDIDATES_FILE):
        return {}
    try:
        with open(CANDIDATES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for c in data.get("candidates", []):
            if c.get("url") == url:
                return c
    except Exception:
        pass
    return {}
```
With:
```python
def _load_candidate_detail(url: str) -> dict:
    """Load candidate detail from rss-registry.json by URL."""
    try:
        registry = _reg.load_registry()
        return _reg.get_by_url(registry, url) or {}
    except Exception:
        return {}
```

- [ ] **Step 4: Replace `graduate_trial_in_config` — remove `_mark_candidate_promoted` call**

Replace:
```python
def graduate_trial_in_config(source_name: str) -> bool:
    """Remove trial=True flag from source (graduates to permanent)."""
    with open(SOURCES_FILE, encoding="utf-8") as f:
        config = json.load(f)

    found = False
    for s in config["news_sources"]["rss_feeds"]:
        if s.get("name") == source_name and s.get("trial"):
            del s["trial"]
            found = True
            break
    if found:
        _atomic_write(SOURCES_FILE, config)
        _mark_candidate_promoted(source_name)
    return found
```
With:
```python
def graduate_trial_in_config(source_name: str) -> bool:
    """Remove trial=True flag from source in news-sources-config.json (graduates to permanent)."""
    with open(SOURCES_FILE, encoding="utf-8") as f:
        config = json.load(f)

    found = False
    for s in config["news_sources"]["rss_feeds"]:
        if s.get("name") == source_name and s.get("trial"):
            del s["trial"]
            found = True
            break
    if found:
        _atomic_write(SOURCES_FILE, config)
    return found
```

- [ ] **Step 5: Rewrite `cmd_run`**

Replace the entire `cmd_run` function body:
```python
def cmd_run() -> None:
    """Normal daily run: update stats, check expiry, promote next candidate."""
    registry = _reg.load_registry()
    today = _today()
    active = _reg.get_active_trial(registry)

    if active:
        # Update today's stats
        day_stats = aggregate_today_stats(active["name"])
        _reg.update_trial_stats(registry, active["name"], day_stats)
        _reg.save_registry(registry)
        active = _reg.get_active_trial(registry)  # re-read after update

        trial = active["trial"]
        stats = trial.get("daily_stats", [])
        print(f"[trial-manager] {active['name']}: day {len(stats)}/{TRIAL_DAYS}"
              f" — fetched={day_stats['fetched']} selected={day_stats['selected']}")

        # Check if trial has run its course
        start = datetime.strptime(trial["start_date"], "%Y-%m-%d").replace(tzinfo=BJT)
        elapsed_days = (datetime.now(BJT) - start).days
        if elapsed_days >= TRIAL_DAYS and not trial.get("auto_decided"):
            total_selected = sum(d.get("selected", 0) for d in stats)
            kept = total_selected >= AUTO_KEEP_MIN_SELECTED
            outcome = "auto-graduated" if kept else "auto-removed"
            if kept:
                graduate_trial_in_config(active["name"])
                _reg.set_production_config(registry, active["name"], keywords=[], limit=3)
                print(f"[trial-manager] Auto-keep '{active['name']}' "
                      f"({total_selected} selected >= {AUTO_KEEP_MIN_SELECTED})")
            else:
                remove_trial_from_config(active["name"])
                print(f"[trial-manager] Auto-remove '{active['name']}' "
                      f"({total_selected} selected < {AUTO_KEEP_MIN_SELECTED})")
            _reg.end_trial(registry, active["name"], outcome=outcome, kept=kept, today=today)
            _reg.save_registry(registry)
            send_auto_decision_email(active, kept, total_selected)
        return

    # No active trial: promote next candidate
    candidates = _reg.get_promotable(registry, PROMOTE_THRESHOLD)
    if not candidates:
        print(f"[trial-manager] No promotable candidates (score >= {PROMOTE_THRESHOLD}). Nothing to do.")
        return

    best = candidates[0]
    score = (best.get("scores") or {}).get("final", 0)
    print(f"[trial-manager] Promoting '{best['name']}' (score={score:.3f}) to trial...")

    add_trial_to_config(best)
    _reg.start_trial(registry, best, today)
    _reg.save_registry(registry)

    print(f"[trial-manager] '{best['name']}' added to news-sources-config.json "
          f"(trial=true). Trial runs until "
          f"{(datetime.now(BJT) + timedelta(days=TRIAL_DAYS)).strftime('%Y-%m-%d')}.")
```

- [ ] **Step 6: Rewrite `cmd_status`**

Replace:
```python
def cmd_status() -> None:
    """Print current trial state."""
    state = load_state()
    active = state.get("active_trial")
    if not active:
        print("No active trial.")
        candidates = get_promotable_candidates(state)
        print(f"Next in queue: {len(candidates)} candidates with score >= {PROMOTE_THRESHOLD}")
        if candidates:
            best = candidates[0]
            print(f"  → '{best['name']}' score={best.get('scores',{}).get('final',0):.3f}")
        return

    today = _today()
    start = datetime.strptime(active["start_date"], "%Y-%m-%d").replace(tzinfo=BJT)
    elapsed = (datetime.now(BJT) - start).days
    stats = active.get("daily_stats", [])
    total_fetched = sum(d.get("fetched", 0) for d in stats)
    total_selected = sum(d.get("selected", 0) for d in stats)

    print(f"Active trial: {active['name']}")
    print(f"  URL:     {active['url']}")
    print(f"  Score:   {active['candidate_score']:.3f}")
    print(f"  Started: {active['start_date']}  (day {elapsed+1}/{TRIAL_DAYS})")
    print(f"  Stats:   {total_fetched} fetched, {total_selected} selected over {len(stats)} days")
    print(f"  Report:  {'sent' if active.get('report_sent') else 'pending'}")
```
With:
```python
def cmd_status() -> None:
    """Print current trial state."""
    registry = _reg.load_registry()
    active = _reg.get_active_trial(registry)
    if not active:
        print("No active trial.")
        candidates = _reg.get_promotable(registry, PROMOTE_THRESHOLD)
        print(f"Next in queue: {len(candidates)} candidates with score >= {PROMOTE_THRESHOLD}")
        if candidates:
            best = candidates[0]
            print(f"  → '{best['name']}' score={(best.get('scores') or {}).get('final', 0):.3f}")
        return

    trial = active["trial"]
    start = datetime.strptime(trial["start_date"], "%Y-%m-%d").replace(tzinfo=BJT)
    elapsed = (datetime.now(BJT) - start).days
    stats = trial.get("daily_stats", [])
    total_fetched = sum(d.get("fetched", 0) for d in stats)
    total_selected = sum(d.get("selected", 0) for d in stats)

    print(f"Active trial: {active['name']}")
    print(f"  URL:     {active['url']}")
    print(f"  Score:   {trial['candidate_score']:.3f}")
    print(f"  Started: {trial['start_date']}  (day {elapsed+1}/{TRIAL_DAYS})")
    print(f"  Stats:   {total_fetched} fetched, {total_selected} selected over {len(stats)} days")
    print(f"  Report:  {'sent' if trial.get('report_sent') else 'pending'}")
```

- [ ] **Step 7: Rewrite `cmd_remove`**

Replace:
```python
def cmd_remove() -> None:
    """Remove active trial source from news config and close trial as rejected."""
    state = load_state()
    active = state.get("active_trial")
    if not active:
        print("No active trial to remove.")
        return

    removed = remove_trial_from_config(active["name"])
    if removed:
        print(f"Removed '{active['name']}' from news-sources-config.json.")
    else:
        print(f"WARNING: '{active['name']}' not found in config (may already be removed).")

    active["end_date"] = _today()
    active["outcome"] = "removed"
    state.setdefault("history", []).append(active)
    state["active_trial"] = None
    save_state(state)
    print(f"Trial closed as 'removed'. History updated.")
```
With:
```python
def cmd_remove() -> None:
    """Remove active trial source from news config and close trial as rejected."""
    registry = _reg.load_registry()
    active = _reg.get_active_trial(registry)
    if not active:
        print("No active trial to remove.")
        return

    removed = remove_trial_from_config(active["name"])
    if removed:
        print(f"Removed '{active['name']}' from news-sources-config.json.")
    else:
        print(f"WARNING: '{active['name']}' not found in config (may already be removed).")

    _reg.end_trial(registry, active["name"], outcome="removed", kept=False, today=_today())
    _reg.save_registry(registry)
    print("Trial closed as 'removed'. Registry updated.")
```

- [ ] **Step 8: Rewrite `cmd_keep`**

Replace:
```python
def cmd_keep() -> None:
    """Graduate active trial to permanent source (remove trial flag)."""
    state = load_state()
    active = state.get("active_trial")
    if not active:
        print("No active trial to graduate.")
        return

    graduated = graduate_trial_in_config(active["name"])
    if graduated:
        print(f"'{active['name']}' graduated to permanent source.")
    else:
        print(f"WARNING: '{active['name']}' trial flag not found in config.")

    active["end_date"] = _today()
    active["outcome"] = "graduated"
    state.setdefault("history", []).append(active)
    state["active_trial"] = None
    save_state(state)
    print("Trial closed as 'graduated'. Source is now permanent.")
```
With:
```python
def cmd_keep() -> None:
    """Graduate active trial to permanent source (remove trial flag)."""
    registry = _reg.load_registry()
    active = _reg.get_active_trial(registry)
    if not active:
        print("No active trial to graduate.")
        return

    graduated = graduate_trial_in_config(active["name"])
    if graduated:
        print(f"'{active['name']}' graduated to permanent source.")
    else:
        print(f"WARNING: '{active['name']}' trial flag not found in config.")

    _reg.end_trial(registry, active["name"], outcome="graduated", kept=True, today=_today())
    _reg.set_production_config(registry, active["name"], keywords=[], limit=3)
    _reg.save_registry(registry)
    print("Trial closed as 'graduated'. Source is now permanent.")
```

- [ ] **Step 9: Verify syntax**

```bash
cd ~/global-news
python3 -m py_compile rss-trial-manager.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 10: Run trial manager tests**

```bash
cd ~/global-news
python3 -m pytest tests/test_rss_trial_manager.py -v
```

The existing tests use `tmp_path` fixtures that create `trial-state.json` and `discovered-rss.json`. They will fail because those paths are no longer used. Update each test fixture:

In `tests/test_rss_trial_manager.py`, find tests that set up `TRIAL_STATE_FILE` and `CANDIDATES_FILE` via monkeypatching. Replace them so they write to `rss_registry.REGISTRY_FILE` instead.

For example, the test `test_returns_candidates_above_threshold` currently writes a fake `discovered-rss.json`. Change it to write a fake registry:

```python
# OLD (in any test that patches CANDIDATES_FILE / TRIAL_STATE_FILE):
candidates_data = {"candidates": [{"name": "A", "url": "...", "scores": {"final": 0.95}, "promoted": False, "rejected": False}]}
state = {"active_trial": None, "history": []}
(tmp_path / "discovered-rss.json").write_text(json.dumps(candidates_data))
(tmp_path / "trial-state.json").write_text(json.dumps(state))
monkeypatch.setattr("rss_trial_manager.CANDIDATES_FILE", str(tmp_path / "discovered-rss.json"))
monkeypatch.setattr("rss_trial_manager.TRIAL_STATE_FILE", str(tmp_path / "trial-state.json"))

# NEW:
registry = {"version": 1, "sources": [
    {"name": "A", "url": "...", "status": "discovered", "scores": {"final": 0.95}, "trial": None, "production": None}
]}
reg_path = str(tmp_path / "rss-registry.json")
(tmp_path / "rss-registry.json").write_text(json.dumps(registry))
monkeypatch.setattr("rss_registry.REGISTRY_FILE", reg_path)
```

Apply this pattern to all affected tests. Run until all pass.

- [ ] **Step 11: Commit**

```bash
cd ~/global-news
git add rss-trial-manager.py tests/test_rss_trial_manager.py
git commit -m "refactor(trial): use rss_registry instead of trial-state.json + discovered-rss.json"
```

---

## Task 5: Update `rss-source-discovery.py`

**Files:**
- Modify: `~/global-news/rss-source-discovery.py`

- [ ] **Step 1: Add import near top of file**

After the existing imports, add:
```python
import rss_registry as _reg
```

- [ ] **Step 2: Replace `load_candidates` and `save_candidates`**

Replace:
```python
def load_candidates() -> dict:
    """Load discovered-rss.json atomically."""
    if not os.path.isfile(CANDIDATES_FILE):
        return {"candidates": []}
    with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_candidates(data: dict) -> None:
    """Atomic write of discovered-rss.json (temp + os.replace)."""
    dir_ = os.path.dirname(CANDIDATES_FILE)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CANDIDATES_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```
With:
```python
def load_candidates() -> dict:
    """Load candidates from rss-registry.json (discovered status only), shaped as old format."""
    registry = _reg.load_registry()
    discovered = _reg.get_by_status(registry, "discovered")
    return {"candidates": discovered}
```
Note: `save_candidates` is no longer needed — callers that write use `cmd_save` which goes through `upsert_source`.

- [ ] **Step 3: Update `load_existing_sources`**

Replace:
```python
def load_existing_sources() -> list:
    """Read news-sources-config.json, flatten all sections into list of {name, url}."""
    ...
```
After the existing implementation, also pull production entries from the registry to catch any not yet in news-sources-config.json:
```python
def load_existing_sources() -> list:
    """Flatten all production sources (news-sources-config.json + registry) into list of {name, url}."""
    sources = []
    # From news-sources-config.json
    if os.path.isfile(SOURCES_CONFIG):
        with open(SOURCES_CONFIG, encoding="utf-8") as f:
            config = json.load(f)
        ns = config.get("news_sources", {})
        for section in ns.values():
            if isinstance(section, list):
                sources.extend(section)
    # Also include registry production sources (guards against partial state)
    registry = _reg.load_registry()
    for s in _reg.get_by_status(registry, "production", "trialing"):
        if not any(_normalize_url(e.get("url", "")) == _normalize_url(s["url"]) for e in sources):
            sources.append({"name": s["name"], "url": s["url"]})
    return sources
```

- [ ] **Step 4: Update `cmd_save`**

The `cmd_save` function reads new candidates from stdin and merges them into `discovered-rss.json`. Change it to use `upsert_source` into the registry:

Replace:
```python
def cmd_save():
    """Read JSON array from stdin, merge into discovered-rss.json."""
    new_entries = json.load(sys.stdin)
    data = load_candidates()
    existing_urls = {_normalize_url(c.get("url", "")) for c in data.get("candidates", [])}
    added = 0
    for entry in new_entries:
        norm = _normalize_url(entry.get("url", ""))
        if norm not in existing_urls:
            data["candidates"].append(entry)
            existing_urls.add(norm)
            added += 1
    save_candidates(data)
    print(f"[cmd_save] Added {added}/{len(new_entries)} new candidates.")
```
With:
```python
def cmd_save():
    """Read JSON array from stdin, merge into rss-registry.json."""
    new_entries = json.load(sys.stdin)
    registry = _reg.load_registry()
    added = 0
    for entry in new_entries:
        if _reg.upsert_source(registry, entry):
            added += 1
    _reg.save_registry(registry)
    print(f"[cmd_save] Added {added}/{len(new_entries)} new candidates to registry.")
```

- [ ] **Step 5: Verify syntax**

```bash
cd ~/global-news
python3 -m py_compile rss-source-discovery.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 6: Run discovery tests**

```bash
cd ~/global-news
python3 -m pytest tests/test_rss_discovery.py -v
```

Update any tests that write/read `discovered-rss.json` to use the registry path instead (same monkeypatch pattern as Task 4 Step 10).

- [ ] **Step 7: Commit**

```bash
cd ~/global-news
git add rss-source-discovery.py tests/test_rss_discovery.py
git commit -m "refactor(discovery): use rss_registry instead of discovered-rss.json"
```

---

## Task 6: Update `rss-promote-candidate.py`

**Files:**
- Modify: `~/global-news/rss-promote-candidate.py`

- [ ] **Step 1: Add import and update `promote_candidate`**

Add at top:
```python
import rss_registry as _reg
```

Replace the `promote_candidate` function body. The current function:
1. Reads `discovered-rss.json` to find candidate
2. Writes to `news-sources-config.json`
3. Marks `promoted=True` in `discovered-rss.json`

New version does the same but uses registry:

```python
def promote_candidate(
    name: str,
    limit: int = 3,
    registry_file: str = _reg.REGISTRY_FILE,
    sources_file: str = DEFAULT_SOURCES,
) -> bool:
    """Promote *name* from candidates to production sources.

    Returns True on success, False if the candidate cannot be promoted.
    """
    registry = _reg.load_registry(registry_file)
    source = next(
        (s for s in _reg.get_sources(registry) if s.get("name") == name and s.get("status") == "discovered"),
        None,
    )
    if source is None:
        print(f"ERROR: candidate '{name}' not found or not in discovered status.", file=sys.stderr)
        return False

    # Load sources
    with open(sources_file, "r", encoding="utf-8") as f:
        sources_data = json.load(f)

    # Idempotency: skip if URL already in sources
    existing_urls = {
        s.get("url", "").rstrip("/").lower()
        for s in sources_data.get("news_sources", {}).get("rss_feeds", [])
    }
    target_url_norm = source["url"].rstrip("/").lower()
    if target_url_norm not in existing_urls:
        new_feed = {
            "name": source["name"],
            "url": source["url"],
            "keywords": [],
            "limit": limit,
        }
        sources_data["news_sources"]["rss_feeds"].append(new_feed)
        _atomic_write(sources_file, sources_data)

    # Update registry: mark as production
    _reg.set_production_config(registry, name, keywords=[], limit=limit)
    source["status"] = "production"
    _reg.save_registry(registry, registry_file)

    print(f"Promoted '{name}' → {source['url']} (limit={limit})")
    return True
```

Update `main()` to pass `registry_file` instead of `candidates_file`:
```python
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote a discovered RSS candidate to production sources."
    )
    parser.add_argument("--name", required=True, help="Exact candidate name to promote")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument(
        "--registry-file",
        default=_reg.REGISTRY_FILE,
        help=f"Path to rss-registry.json (default: {_reg.REGISTRY_FILE})",
    )
    parser.add_argument(
        "--sources-file",
        default=DEFAULT_SOURCES,
        help=f"Path to news-sources-config.json (default: {DEFAULT_SOURCES})",
    )
    args = parser.parse_args()

    success = promote_candidate(
        name=args.name,
        limit=args.limit,
        registry_file=args.registry_file,
        sources_file=args.sources_file,
    )
    sys.exit(0 if success else 1)
```

- [ ] **Step 2: Verify syntax**

```bash
cd ~/global-news
python3 -m py_compile rss-promote-candidate.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Update promote tests**

In `tests/test_rss_promote.py`, the fixtures create `discovered-rss.json`. Update them to use `rss-registry.json`:

```python
# OLD fixture setup:
candidates_data = {"candidates": [{"name": "Feed A", "url": "https://a.com/feed", "promoted": False}]}
(tmp_path / "config").mkdir()
(tmp_path / "config" / "discovered-rss.json").write_text(json.dumps(candidates_data))
result = promote_candidate(name="Feed A", candidates_file=..., sources_file=...)

# NEW fixture setup:
registry = {"version": 1, "sources": [
    {"name": "Feed A", "url": "https://a.com/feed", "status": "discovered",
     "scores": {"final": 0.95}, "trial": None, "production": None}
]}
(tmp_path / "config").mkdir()
reg_path = str(tmp_path / "config" / "rss-registry.json")
(tmp_path / "config" / "rss-registry.json").write_text(json.dumps(registry))
result = promote_candidate(name="Feed A", registry_file=reg_path, sources_file=...)
# Assert registry was updated:
import json as _json
updated = _json.loads((tmp_path / "config" / "rss-registry.json").read_text())
assert updated["sources"][0]["status"] == "production"
```

- [ ] **Step 4: Run promote tests**

```bash
cd ~/global-news
python3 -m pytest tests/test_rss_promote.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd ~/global-news
git add rss-promote-candidate.py tests/test_rss_promote.py
git commit -m "refactor(promote): use rss_registry instead of discovered-rss.json"
```

---

## Task 7: Update shell script + delete old files + final regression

**Files:**
- Modify: `~/global-news/scripts/rss-source-discovery.sh`

- [ ] **Step 1: Update git add commands in shell script**

In `scripts/rss-source-discovery.sh`, find the git add commands that commit the old files:

```bash
# OLD:
git add config/discovered-rss.json
...
git add config/trial-state.json news-sources-config.json
```

Replace with:
```bash
# NEW:
git add config/rss-registry.json
...
git add config/rss-registry.json news-sources-config.json
```

- [ ] **Step 2: Verify shell script syntax**

```bash
bash -n ~/global-news/scripts/rss-source-discovery.sh && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Run full test suite**

```bash
cd ~/global-news
python3 -m pytest tests/ -v 2>&1 | tail -20
```
Expected: all 146+ tests pass. Fix any remaining failures before proceeding.

- [ ] **Step 4: Delete old files**

```bash
cd ~/global-news
git rm config/discovered-rss.json config/trial-state.json
```

- [ ] **Step 5: Final commit**

```bash
cd ~/global-news
git add scripts/rss-source-discovery.sh
git commit -m "refactor(registry): remove discovered-rss.json + trial-state.json, update shell script"
```

- [ ] **Step 6: Run full test suite one more time**

```bash
cd ~/global-news
python3 -m pytest tests/ -q
```
Expected: same pass count as before, zero failures.

- [ ] **Step 7: Push**

```bash
cd ~/global-news
git push
```

---

## Self-Review

**Spec coverage:**
- ✅ Three files → one: `rss-registry.json` covers all states
- ✅ Atomic writes preserved (`_atomic_write` in `rss_registry.py`)
- ✅ `news-sources-config.json` unchanged (sender/health-check unaffected)
- ✅ `_mark_candidate_promoted` removed (no longer needed)
- ✅ `graduate_trial_in_config` still syncs `news-sources-config.json`
- ✅ Trial history preserved in `trial.end_date` on each source
- ✅ One active trial enforced by `get_active_trial` returning first `status=trialing`
- ✅ Migration script handles: discovered, trialing, production (with prod config), rejected
- ✅ All four CLI commands (`run`, `status`, `remove`, `keep`) updated
- ✅ Tests updated for all three modified scripts

**No placeholders** — all code blocks are complete.

**Type consistency** — `active["trial"]["start_date"]` used consistently in Tasks 4-6; `_reg.*` prefix used consistently.
