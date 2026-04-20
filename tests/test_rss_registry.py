"""Tests for rss_registry.py"""
import json
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
        reg = _make_registry(_source("A", status="discovered"), _source("B", url="https://b.com/f", status="production"))
        result = get_by_status(reg, "discovered")
        assert [s["name"] for s in result] == ["A"]

    def test_filters_by_multiple_statuses(self):
        reg = _make_registry(
            _source("A", status="discovered"),
            _source("B", url="https://b.com/f", status="trialing"),
            _source("C", url="https://c.com/f", status="rejected"),
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
            _source("B", url="https://b.com/f", status="trialing"),
        )
        assert get_active_trial(reg)["name"] == "B"

    def test_returns_none_when_no_trial(self):
        reg = _make_registry(_source("A", status="discovered"))
        assert get_active_trial(reg) is None


class TestGetTrialHistory:
    def test_returns_sources_with_end_date(self):
        reg = _make_registry(
            _source("A", trial={"start_date": "2026-04-01", "end_date": "2026-04-04"}),
            _source("B", url="https://b.com/f", trial={"start_date": "2026-04-10", "end_date": None}),
            _source("C", url="https://c.com/f"),
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

    def test_raises_if_no_active_trial(self):
        reg = _make_registry(_source(status="discovered"))
        with pytest.raises(KeyError):
            update_trial_stats(reg, "Feed A", {"date": "2026-04-20", "fetched": 1, "selected": 1})


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

    def test_auto_decided_param(self):
        src = _source(status="trialing", trial={"daily_stats": [], "start_date": "2026-04-17"})
        reg = _make_registry(src)
        end_trial(reg, "Feed A", outcome="manual-keep", kept=True, today="2026-04-20", auto_decided=False)
        assert get_sources(reg)[0]["trial"]["auto_decided"] is False

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
