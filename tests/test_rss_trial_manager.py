#!/usr/bin/env python3
"""Tests for rss-trial-manager.py"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

import rss_registry as _reg

# Module file uses hyphens; load by path
import importlib.util
spec = importlib.util.spec_from_file_location(
    "rss_trial_manager",
    os.path.join(REPO_DIR, "rss-trial-manager.py"),
)
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)


# ── helpers ───────────────────────────────────────────────────────────────────

def make_candidate(name="Test Feed", url="https://example.com/rss", score=0.90):
    return {
        "name": name,
        "url": url,
        "language": "en",
        "category": "tech_ai",
        "scores": {"final": score, "authority": 0.8, "uniqueness": 0.8},
        "status": "discovered",
    }


def make_registry(sources=None):
    return {"version": 1, "sources": sources or []}


# ── get_promotable (via rss_registry) ─────────────────────────────────────────

class TestGetPromotableCandidates(unittest.TestCase):

    def _write_registry(self, tmp_dir, sources):
        path = os.path.join(tmp_dir, "rss-registry.json")
        with open(path, "w") as f:
            json.dump({"version": 1, "sources": sources}, f)
        return path

    def test_returns_candidates_above_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            reg_path = self._write_registry(d, [
                make_candidate("A", score=0.95),
                make_candidate("B", score=0.80),  # below threshold (PROMOTE_THRESHOLD=0.90)
                make_candidate("C", score=0.92),
            ])
            registry = _reg.load_registry(reg_path)
            result = _reg.get_promotable(registry, tm.PROMOTE_THRESHOLD)
        names = [c["name"] for c in result]
        self.assertIn("A", names)
        self.assertIn("C", names)
        self.assertNotIn("B", names)

    def test_excludes_already_tried(self):
        with tempfile.TemporaryDirectory() as d:
            # Source that has been trialed before (has trial block → excluded)
            tried = {**make_candidate("A", url="https://a.com/rss", score=0.90),
                     "trial": {"start_date": "2026-01-01", "end_date": "2026-01-04"}}
            reg_path = self._write_registry(d, [tried])
            registry = _reg.load_registry(reg_path)
            result = _reg.get_promotable(registry, tm.PROMOTE_THRESHOLD)
        self.assertEqual(result, [])

    def test_excludes_active_trial(self):
        with tempfile.TemporaryDirectory() as d:
            # Source currently in trialing status (has trial block → excluded)
            trialing = {**make_candidate("A", url="https://a.com/rss", score=0.90),
                        "status": "trialing",
                        "trial": {"start_date": "2026-04-18", "end_date": None}}
            reg_path = self._write_registry(d, [trialing])
            registry = _reg.load_registry(reg_path)
            result = _reg.get_promotable(registry, tm.PROMOTE_THRESHOLD)
        self.assertEqual(result, [])

    def test_sorted_by_score_desc(self):
        with tempfile.TemporaryDirectory() as d:
            reg_path = self._write_registry(d, [
                make_candidate("Low",  score=0.90),
                make_candidate("High", score=0.98),
                make_candidate("Mid",  score=0.93),
            ])
            registry = _reg.load_registry(reg_path)
            result = _reg.get_promotable(registry, tm.PROMOTE_THRESHOLD)
        self.assertEqual(result[0]["name"], "High")
        self.assertEqual(result[-1]["name"], "Low")


# ── news-sources-config management ───────────────────────────────────────────

class TestConfigManagement(unittest.TestCase):

    def _write_config(self, tmp_dir, feeds=None):
        path = os.path.join(tmp_dir, "news-sources-config.json")
        config = {"news_sources": {"rss_feeds": feeds or [], "sina_api": [], "hn_api": []}}
        with open(path, "w") as f:
            json.dump(config, f)
        return path

    def test_add_trial_to_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._write_config(d)
            candidate = make_candidate("ProPublica", url="https://pp.org/rss")
            with patch.object(tm, "SOURCES_FILE", cfg):
                tm.add_trial_to_config(candidate)
            with open(cfg) as f:
                config = json.load(f)
        feeds = config["news_sources"]["rss_feeds"]
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["name"], "ProPublica")
        self.assertTrue(feeds[0]["trial"])
        self.assertEqual(feeds[0]["limit"], 3)

    def test_remove_trial_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._write_config(d, feeds=[
                {"name": "Existing", "url": "https://ex.com/rss", "keywords": [], "limit": 3},
                {"name": "ProPublica", "url": "https://pp.org/rss", "keywords": [], "limit": 3, "trial": True},
            ])
            with patch.object(tm, "SOURCES_FILE", cfg):
                removed = tm.remove_trial_from_config("ProPublica")
            with open(cfg) as f:
                config = json.load(f)
        self.assertTrue(removed)
        feeds = config["news_sources"]["rss_feeds"]
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["name"], "Existing")

    def test_graduate_removes_trial_flag(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._write_config(d, feeds=[
                {"name": "ProPublica", "url": "https://pp.org/rss", "keywords": [], "limit": 3, "trial": True},
            ])
            with patch.object(tm, "SOURCES_FILE", cfg):
                graduated = tm.graduate_trial_in_config("ProPublica")
            with open(cfg) as f:
                config = json.load(f)
        self.assertTrue(graduated)
        feed = config["news_sources"]["rss_feeds"][0]
        self.assertNotIn("trial", feed)
        self.assertEqual(feed["name"], "ProPublica")


# ── aggregate_today_stats ─────────────────────────────────────────────────────

class TestAggregateStats(unittest.TestCase):

    def test_sums_todays_entries(self):
        today = tm._today()
        entries = [
            json.dumps({"ts": f"{today}T08:00:00+08:00", "source": "ProPublica", "fetched": 5, "selected": 5}),
            json.dumps({"ts": f"{today}T16:00:00+08:00", "source": "ProPublica", "fetched": 3, "selected": 3}),
            json.dumps({"ts": f"{today}T08:00:00+08:00", "source": "OtherSource", "fetched": 9, "selected": 9}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(entries) + "\n")
            log_path = f.name
        try:
            with patch.object(tm, "TRIAL_LOG_FILE", log_path):
                stats = tm.aggregate_today_stats("ProPublica")
        finally:
            os.unlink(log_path)
        self.assertEqual(stats["fetched"], 8)
        self.assertEqual(stats["date"], today)

    def test_ignores_other_days(self):
        entries = [
            json.dumps({"ts": "2026-01-01T08:00:00+08:00", "source": "ProPublica", "fetched": 99, "selected": 99}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(entries) + "\n")
            log_path = f.name
        try:
            with patch.object(tm, "TRIAL_LOG_FILE", log_path):
                stats = tm.aggregate_today_stats("ProPublica")
        finally:
            os.unlink(log_path)
        self.assertEqual(stats["fetched"], 0)


class TestClearHealthStateForRemovedTrial(unittest.TestCase):
    """remove_trial_from_config should also clear rss-health.json entry so that
    stale consecutive_fails from the trial window doesn't bleed into future runs."""

    def test_removes_health_state_entry(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Seed a mock sources config containing a trial source
            sources_path = os.path.join(tmp_dir, "news-sources-config.json")
            with open(sources_path, "w") as f:
                json.dump({
                    "news_sources": {
                        "rss_feeds": [
                            {"name": "Trial Foo", "url": "https://foo/rss", "trial": True},
                            {"name": "Other", "url": "https://other/rss"},
                        ]
                    }
                }, f)
            # Seed health-state with entries for both
            health_path = os.path.join(tmp_dir, "rss-health.json")
            with open(health_path, "w") as f:
                json.dump({
                    "Trial Foo": {"consecutive_fails": 2, "last_check": "2026-04-21 BJT"},
                    "Other":     {"consecutive_fails": 0, "last_check": "2026-04-21 BJT"},
                }, f)
            with patch.object(tm, "SOURCES_FILE", sources_path), \
                 patch.object(tm, "HEALTH_STATE_FILE", health_path):
                removed = tm.remove_trial_from_config("Trial Foo")
            self.assertTrue(removed)
            with open(health_path) as f:
                state_after = json.load(f)
            self.assertNotIn("Trial Foo", state_after)  # cleared
            self.assertIn("Other", state_after)         # untouched

    def test_no_health_file_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sources_path = os.path.join(tmp_dir, "news-sources-config.json")
            with open(sources_path, "w") as f:
                json.dump({"news_sources": {"rss_feeds": [
                    {"name": "Trial Foo", "url": "https://foo/rss", "trial": True}]}}, f)
            health_path = os.path.join(tmp_dir, "rss-health.json")  # intentionally absent
            with patch.object(tm, "SOURCES_FILE", sources_path), \
                 patch.object(tm, "HEALTH_STATE_FILE", health_path):
                # Must not raise even when health state file doesn't exist
                self.assertTrue(tm.remove_trial_from_config("Trial Foo"))


class TestCmdRetry(unittest.TestCase):
    """cmd_retry: reset auto-removed trials so they can re-enter the queue."""

    def _seed_registry(self, tmp_dir, target):
        path = os.path.join(tmp_dir, "rss-registry.json")
        with open(path, "w") as f:
            json.dump({"version": 1, "sources": [target]}, f)
        return path

    def test_retry_resets_auto_removed_to_discovered(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = {
                "name": "Flaky Feed",
                "url": "https://flaky.example.com/rss",
                "status": "rejected",
                "scores": {"final": 0.93},
                "trial": {"start_date": "2026-04-20", "end_date": "2026-04-23",
                          "outcome": "auto-removed", "daily_stats": [],
                          "auto_decided": True, "candidate_score": 0.93},
            }
            reg_path = self._seed_registry(tmp_dir, target)
            with patch.object(_reg, "REGISTRY_FILE", reg_path), \
                 patch.object(sys, "argv", ["rss-trial-manager.py", "retry", "Flaky Feed"]):
                tm.cmd_retry()
            with open(reg_path) as f:
                reg = json.load(f)
            s = reg["sources"][0]
            self.assertEqual(s["status"], "discovered")
            self.assertIsNone(s["trial"])
            self.assertEqual(len(s["trial_history"]), 1)
            self.assertEqual(s["trial_history"][0]["outcome"], "auto-removed")

    def test_retry_refuses_pool_cap_rejection(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = {
                "name": "Low Score",
                "url": "https://low.example.com/rss",
                "status": "rejected",
                "reject_reason": "pool-cap",
                "scores": {"final": 0.62},
                "trial": None,
            }
            reg_path = self._seed_registry(tmp_dir, target)
            with patch.object(_reg, "REGISTRY_FILE", reg_path), \
                 patch.object(sys, "argv", ["rss-trial-manager.py", "retry", "Low Score"]):
                with self.assertRaises(SystemExit) as cm:
                    tm.cmd_retry()
                self.assertEqual(cm.exception.code, 1)
            with open(reg_path) as f:
                reg = json.load(f)
            # Unchanged
            self.assertEqual(reg["sources"][0]["status"], "rejected")
            self.assertEqual(reg["sources"][0]["reject_reason"], "pool-cap")

    def test_retry_refuses_unknown_name(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            reg_path = self._seed_registry(tmp_dir, {
                "name": "Something Else", "url": "https://x/r", "status": "discovered",
                "scores": {"final": 0.8}, "trial": None,
            })
            with patch.object(_reg, "REGISTRY_FILE", reg_path), \
                 patch.object(sys, "argv", ["rss-trial-manager.py", "retry", "Nonexistent"]):
                with self.assertRaises(SystemExit):
                    tm.cmd_retry()


# ── cmd_run multi-trial logic ────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone

_BJT = timezone(timedelta(hours=8))


def _days_ago(n: int) -> str:
    return (datetime.now(_BJT) - timedelta(days=n)).strftime("%Y-%m-%d")


def _today_str() -> str:
    return datetime.now(_BJT).strftime("%Y-%m-%d")


def _trialing(name, url, category, start_days_ago=0, daily_stats=None, score=0.95):
    """Build a registry entry for an active trial."""
    return {
        "name": name, "url": url, "status": "trialing", "category": category,
        "scores": {"final": score},
        "trial": {
            "start_date": _days_ago(start_days_ago),
            "end_date": None,
            "daily_stats": daily_stats or [],
            "outcome": None,
            "auto_decided": False,
            "candidate_score": score,
            "report_sent": False,
        },
    }


def _discovered(name, url, category, score=0.95):
    return {
        "name": name, "url": url, "status": "discovered", "category": category,
        "scores": {"final": score},
        "trial": None, "production": None,
    }


class _CmdRunHarness:
    """Set up tmp paths + initial config for cmd_run integration tests."""

    def __init__(self, tmp_dir):
        self.tmp = tmp_dir
        self.reg_path = os.path.join(tmp_dir, "rss-registry.json")
        self.cfg_path = os.path.join(tmp_dir, "news-sources-config.json")
        self.health_path = os.path.join(tmp_dir, "rss-health.json")
        self.log_path = os.path.join(tmp_dir, "trial-source-log.jsonl")
        with open(self.cfg_path, "w") as f:
            json.dump({"news_sources": {"rss_feeds": [], "sina_api": [], "hn_api": []}}, f)

    def write_registry(self, sources):
        with open(self.reg_path, "w") as f:
            json.dump({"version": 1, "sources": sources}, f)

    def read_registry(self):
        with open(self.reg_path) as f:
            return json.load(f)

    def read_config(self):
        with open(self.cfg_path) as f:
            return json.load(f)

    def patch_all(self):
        """Returns a list of patch context managers — apply via ExitStack."""
        return [
            patch.object(_reg, "REGISTRY_FILE", self.reg_path),
            patch.object(tm, "SOURCES_FILE", self.cfg_path),
            patch.object(tm, "HEALTH_STATE_FILE", self.health_path),
            patch.object(tm, "TRIAL_LOG_FILE", self.log_path),
            patch.object(tm, "send_auto_decision_email", MagicMock(return_value=True)),
        ]


import contextlib


def _run_cmd_run(harness):
    with contextlib.ExitStack() as stack:
        for p in harness.patch_all():
            stack.enter_context(p)
        tm.cmd_run()


class TestMaxConcurrentTrialsConstant(unittest.TestCase):

    def test_constant_is_2(self):
        self.assertEqual(tm.MAX_CONCURRENT_TRIALS, 2)


class TestCmdRunMultipleTrials(unittest.TestCase):

    def test_promotes_when_no_active_and_pool_has_candidates(self):
        """Baseline: 0 active, 1 promotable → it gets promoted."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            h.write_registry([
                _discovered("CandA", "https://a.com/f", category="europe", score=0.95),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["CandA"], "trialing")

    def test_promotes_second_trial_when_one_active_and_slot_available(self):
        """1 active + slot available + diff category → second trial promoted."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            h.write_registry([
                _trialing("Active1", "https://a.com/f", category="healthcare", start_days_ago=1),
                _discovered("CandTech", "https://t.com/f", category="tech_ai", score=0.95),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["Active1"], "trialing")
        self.assertEqual(statuses["CandTech"], "trialing")

    def test_does_not_promote_when_max_concurrent_reached(self):
        """2 active (=MAX) → no new promotion even if pool has high-scoring candidates."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            h.write_registry([
                _trialing("Active1", "https://a.com/f", category="healthcare", start_days_ago=1),
                _trialing("Active2", "https://b.com/f", category="europe", start_days_ago=1),
                _discovered("CandTech", "https://t.com/f", category="tech_ai", score=0.99),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["CandTech"], "discovered")

    def test_skips_promotion_of_same_category_as_active(self):
        """1 active in healthcare + pool has higher-scoring healthcare and lower-scoring europe.
        Mutex must skip healthcare and promote europe."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            h.write_registry([
                _trialing("ActiveHealth", "https://a.com/f", category="healthcare", start_days_ago=1),
                _discovered("CandHealth", "https://h.com/f", category="healthcare", score=0.99),
                _discovered("CandEurope", "https://e.com/f", category="europe", score=0.91),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["CandHealth"], "discovered")
        self.assertEqual(statuses["CandEurope"], "trialing")

    def test_does_not_promote_twice_in_one_day(self):
        """1 active started today + slot avail + pool has candidate.
        Daily promotion budget = 1, so no second promotion same day even though slot is open."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            h.write_registry([
                _trialing("PromotedToday", "https://a.com/f", category="healthcare", start_days_ago=0),
                _discovered("CandTech", "https://t.com/f", category="tech_ai", score=0.99),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["CandTech"], "discovered")

    def test_updates_stats_for_each_active_trial(self):
        """Each active trial gets daily_stats backfilled for [start_date, today].
        With start_days_ago=1, that's 2 days of zero-stats (no JSONL entries)."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            h.write_registry([
                _trialing("A", "https://a.com/f", category="healthcare", start_days_ago=1),
                _trialing("B", "https://b.com/f", category="europe", start_days_ago=1),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        for s in reg["sources"]:
            stats = s["trial"]["daily_stats"]
            self.assertEqual(len(stats), 2, f"{s['name']} should have 2 days of stats")
            dates = [x["date"] for x in stats]
            self.assertIn(_days_ago(1), dates)
            self.assertIn(_today_str(), dates)

    def test_evaluates_each_trial_independently_for_expiry(self):
        """One trial expired (>= 3 days, ≥3 selected → auto-graduate),
        one not yet expired (started today). Only the expired one ends.
        The expired trial's selections live in trial-source-log so backfill
        can reconstruct them — this is the real production data flow."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            log_entries = [
                {"ts": f"{_days_ago(3)}T08:00:00+08:00", "source": "Ripe", "fetched": 5, "selected": 5},
                {"ts": f"{_days_ago(2)}T08:00:00+08:00", "source": "Ripe", "fetched": 5, "selected": 5},
                {"ts": f"{_days_ago(1)}T08:00:00+08:00", "source": "Ripe", "fetched": 5, "selected": 5},
            ]
            with open(h.log_path, "w") as f:
                for e in log_entries:
                    f.write(json.dumps(e) + "\n")
            h.write_registry([
                _trialing("Ripe", "https://r.com/f", category="europe",
                          start_days_ago=4, daily_stats=[]),
                _trialing("Fresh", "https://f.com/f", category="healthcare",
                          start_days_ago=0),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["Ripe"], "production")  # graduated
        self.assertEqual(statuses["Fresh"], "trialing")   # still active


class TestCmdRemoveAndKeepWithMultiple(unittest.TestCase):
    """When multiple trials are active, manual cmd_remove/cmd_keep require a name."""

    def _seed(self, d, sources, config_feeds):
        reg_path = os.path.join(d, "rss-registry.json")
        cfg_path = os.path.join(d, "news-sources-config.json")
        with open(reg_path, "w") as f:
            json.dump({"version": 1, "sources": sources}, f)
        with open(cfg_path, "w") as f:
            json.dump({"news_sources": {"rss_feeds": config_feeds, "sina_api": [], "hn_api": []}}, f)
        return reg_path, cfg_path

    def test_remove_requires_name_when_multiple_active(self):
        with tempfile.TemporaryDirectory() as d:
            reg_path, cfg_path = self._seed(d, [
                _trialing("A", "https://a.com/f", category="healthcare", start_days_ago=1),
                _trialing("B", "https://b.com/f", category="europe", start_days_ago=1),
            ], [
                {"name": "A", "url": "https://a.com/f", "trial": True, "limit": 3},
                {"name": "B", "url": "https://b.com/f", "trial": True, "limit": 3},
            ])
            with patch.object(_reg, "REGISTRY_FILE", reg_path), \
                 patch.object(tm, "SOURCES_FILE", cfg_path), \
                 patch.object(tm, "HEALTH_STATE_FILE", os.path.join(d, "h.json")), \
                 patch.object(sys, "argv", ["rss-trial-manager.py", "remove"]):
                with self.assertRaises(SystemExit):
                    tm.cmd_remove()
            # Both trials untouched
            with open(reg_path) as f:
                reg = json.load(f)
            statuses = {s["name"]: s["status"] for s in reg["sources"]}
            self.assertEqual(statuses["A"], "trialing")
            self.assertEqual(statuses["B"], "trialing")

    def test_remove_with_name_targets_specific_trial(self):
        with tempfile.TemporaryDirectory() as d:
            reg_path, cfg_path = self._seed(d, [
                _trialing("A", "https://a.com/f", category="healthcare", start_days_ago=1),
                _trialing("B", "https://b.com/f", category="europe", start_days_ago=1),
            ], [
                {"name": "A", "url": "https://a.com/f", "trial": True, "limit": 3},
                {"name": "B", "url": "https://b.com/f", "trial": True, "limit": 3},
            ])
            with patch.object(_reg, "REGISTRY_FILE", reg_path), \
                 patch.object(tm, "SOURCES_FILE", cfg_path), \
                 patch.object(tm, "HEALTH_STATE_FILE", os.path.join(d, "h.json")), \
                 patch.object(sys, "argv", ["rss-trial-manager.py", "remove", "A"]):
                tm.cmd_remove()
            with open(reg_path) as f:
                reg = json.load(f)
            statuses = {s["name"]: s["status"] for s in reg["sources"]}
            self.assertEqual(statuses["A"], "rejected")
            self.assertEqual(statuses["B"], "trialing")

    def test_keep_requires_name_when_multiple_active(self):
        with tempfile.TemporaryDirectory() as d:
            reg_path, cfg_path = self._seed(d, [
                _trialing("A", "https://a.com/f", category="healthcare", start_days_ago=1),
                _trialing("B", "https://b.com/f", category="europe", start_days_ago=1),
            ], [
                {"name": "A", "url": "https://a.com/f", "trial": True, "limit": 3},
                {"name": "B", "url": "https://b.com/f", "trial": True, "limit": 3},
            ])
            with patch.object(_reg, "REGISTRY_FILE", reg_path), \
                 patch.object(tm, "SOURCES_FILE", cfg_path), \
                 patch.object(tm, "HEALTH_STATE_FILE", os.path.join(d, "h.json")), \
                 patch.object(sys, "argv", ["rss-trial-manager.py", "keep"]):
                with self.assertRaises(SystemExit):
                    tm.cmd_keep()
            with open(reg_path) as f:
                reg = json.load(f)
            statuses = {s["name"]: s["status"] for s in reg["sources"]}
            self.assertEqual(statuses["A"], "trialing")
            self.assertEqual(statuses["B"], "trialing")


# ── daily-stats backfill across [start_date, today] ──────────────────────────

class TestAggregateStatsForRange(unittest.TestCase):
    """aggregate_stats_for_range returns one stats dict per date in [start, end],
    filling gaps with zeros so callers can blindly upsert each entry."""

    def _write_log(self, path, entries):
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_returns_one_entry_per_date_inclusive(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "log.jsonl")
            self._write_log(log_path, [
                {"ts": "2026-04-26T08:00:00+08:00", "source": "X", "fetched": 3, "selected": 2},
                {"ts": "2026-04-26T16:00:00+08:00", "source": "X", "fetched": 3, "selected": 3},
                {"ts": "2026-04-28T08:00:00+08:00", "source": "X", "fetched": 3, "selected": 3},
            ])
            with patch.object(tm, "TRIAL_LOG_FILE", log_path):
                result = tm.aggregate_stats_for_range("X", "2026-04-26", "2026-04-29")
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0], {"date": "2026-04-26", "fetched": 6, "selected": 5})
        self.assertEqual(result[1], {"date": "2026-04-27", "fetched": 0, "selected": 0})
        self.assertEqual(result[2], {"date": "2026-04-28", "fetched": 3, "selected": 3})
        self.assertEqual(result[3], {"date": "2026-04-29", "fetched": 0, "selected": 0})

    def test_ignores_other_sources(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "log.jsonl")
            self._write_log(log_path, [
                {"ts": "2026-04-26T08:00:00+08:00", "source": "Y", "fetched": 99, "selected": 99},
                {"ts": "2026-04-26T08:00:00+08:00", "source": "X", "fetched": 3, "selected": 3},
            ])
            with patch.object(tm, "TRIAL_LOG_FILE", log_path):
                result = tm.aggregate_stats_for_range("X", "2026-04-26", "2026-04-26")
        self.assertEqual(result, [{"date": "2026-04-26", "fetched": 3, "selected": 3}])

    def test_returns_zeros_when_log_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(tm, "TRIAL_LOG_FILE", os.path.join(d, "no_such.jsonl")):
                result = tm.aggregate_stats_for_range("X", "2026-04-26", "2026-04-27")
        self.assertEqual(result, [
            {"date": "2026-04-26", "fetched": 0, "selected": 0},
            {"date": "2026-04-27", "fetched": 0, "selected": 0},
        ])


class TestDailyStatsBackfill(unittest.TestCase):
    """cmd_run must backfill daily_stats for every date in [start_date, today],
    not just today. Otherwise day-1 (trial-creation-day) data is permanently
    lost because cmd_run runs "update active trials → promote new" — on day-1
    the trial is created at the end, so the same cmd_run never updates its
    stats. Subsequent cmd_run calls only aggregated today, missing day-1."""

    def _write_log(self, path, entries):
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_backfills_missing_day1_data(self):
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            day_minus_2 = _days_ago(2)
            day_minus_1 = _days_ago(1)
            today = _today_str()
            self._write_log(h.log_path, [
                {"ts": f"{day_minus_2}T08:00:00+08:00", "source": "A", "fetched": 3, "selected": 2},
                {"ts": f"{day_minus_2}T16:00:00+08:00", "source": "A", "fetched": 3, "selected": 3},
                {"ts": f"{day_minus_1}T00:00:00+08:00", "source": "A", "fetched": 3, "selected": 1},
                # today: no entries yet
            ])
            h.write_registry([
                _trialing("A", "https://a.com/f", category="europe",
                          start_days_ago=2, daily_stats=[]),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        stats = reg["sources"][0]["trial"]["daily_stats"]
        by_date = {s["date"]: s for s in stats}
        self.assertEqual(set(by_date.keys()), {day_minus_2, day_minus_1, today})
        self.assertEqual(by_date[day_minus_2], {"date": day_minus_2, "fetched": 6, "selected": 5})
        self.assertEqual(by_date[day_minus_1], {"date": day_minus_1, "fetched": 3, "selected": 1})
        self.assertEqual(by_date[today], {"date": today, "fetched": 0, "selected": 0})

    def test_backfill_updates_existing_dates_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            day_minus_1 = _days_ago(1)
            today = _today_str()
            self._write_log(h.log_path, [
                {"ts": f"{day_minus_1}T08:00:00+08:00", "source": "A", "fetched": 3, "selected": 3},
                {"ts": f"{today}T08:00:00+08:00", "source": "A", "fetched": 3, "selected": 2},
            ])
            stale_stats = [{"date": day_minus_1, "fetched": 0, "selected": 0}]
            h.write_registry([
                _trialing("A", "https://a.com/f", category="europe",
                          start_days_ago=1, daily_stats=stale_stats),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        stats = reg["sources"][0]["trial"]["daily_stats"]
        dates = [s["date"] for s in stats]
        self.assertEqual(len(dates), 2, f"expected 2 entries, got {dates}")
        self.assertEqual(len(set(dates)), 2, "no duplicates")
        by_date = {s["date"]: s for s in stats}
        self.assertEqual(by_date[day_minus_1]["fetched"], 3)
        self.assertEqual(by_date[day_minus_1]["selected"], 3)


# ── email rendering: nested-trial-field access (bug fix) ─────────────────────

class TestEmailRenderingNestedFields(unittest.TestCase):
    """generate_report_html and _render_auto_decision_html receive the source
    object (top-level), but candidate_score / start_date / daily_stats live in
    the nested `trial` sub-object. Earlier code did `trial.get("daily_stats")`
    on the source object, returning [] / 0 / "?" instead of real values."""

    def _source(self):
        return {
            "name": "Politico Europe",
            "url": "https://www.politico.eu/feed/",
            "category": "europe",
            "language": "en",
            "scores": {
                "final": 0.91,
                "reliability": 0.8, "freshness": 1.0, "content_quality": 1.0,
                "content_depth": 0.6, "authority": 0.8, "uniqueness": 0.95,
            },
            "trial": {
                "start_date": "2026-04-26",
                "end_date": "2026-04-29",
                "daily_stats": [
                    {"date": "2026-04-27", "fetched": 3, "selected": 3},
                    {"date": "2026-04-28", "fetched": 0, "selected": 0},
                    {"date": "2026-04-29", "fetched": 0, "selected": 0},
                ],
                "candidate_score": 0.91,
                "outcome": "auto-graduated",
                "auto_decided": True,
                "report_sent": False,
            },
        }

    def test_generate_report_html_renders_nested_candidate_score(self):
        html = tm.generate_report_html(self._source())
        self.assertIn("0.910", html)
        self.assertNotIn("综合评分：0.000", html)

    def test_generate_report_html_renders_nested_start_date(self):
        html = tm.generate_report_html(self._source())
        self.assertIn("2026-04-26", html)
        # The footer label "试用期" should not show "?" for start date
        self.assertNotIn("? →", html)

    def test_generate_report_html_aggregates_fetched_from_nested_daily_stats(self):
        html = tm.generate_report_html(self._source())
        # Footer row "3 天合计" must show fetched=3 (sum of 3+0+0), not 0
        # The structure is: <td>... 3 天合计</td><td>{total_fetched}</td><td>{total_selected}</td>
        idx = html.find("天合计")
        self.assertNotEqual(idx, -1, "footer not found")
        footer_tail = html[idx:idx + 600]
        # First numeric cell after "天合计" is total_fetched
        import re
        cells = re.findall(r">(\d+)<", footer_tail)
        self.assertGreaterEqual(len(cells), 2, f"need at least 2 numeric cells, got {cells}")
        self.assertEqual(cells[0], "3", f"total_fetched should be 3, got {cells[0]} (full cells={cells})")

    def test_render_auto_decision_html_renders_nested_candidate_score(self):
        html = tm._render_auto_decision_html(self._source(), kept=True, total_selected=3,
                                             smtp_user="x@example.com", mail_to="y@example.com")
        self.assertIn("0.910", html)
        self.assertNotIn("发现评分：</strong>0.000", html)

    def test_render_auto_decision_html_renders_nested_start_date(self):
        html = tm._render_auto_decision_html(self._source(), kept=True, total_selected=3,
                                             smtp_user="x@example.com", mail_to="y@example.com")
        self.assertIn("2026-04-26", html)
        self.assertNotIn("? →", html)

    def test_render_auto_decision_html_aggregates_fetched_from_nested_daily_stats(self):
        html = tm._render_auto_decision_html(self._source(), kept=True, total_selected=3,
                                             smtp_user="x@example.com", mail_to="y@example.com")
        idx = html.find("天合计")
        self.assertNotEqual(idx, -1, "footer not found")
        footer_tail = html[idx:idx + 600]
        import re
        cells = re.findall(r">(\d+)<", footer_tail)
        self.assertGreaterEqual(len(cells), 2)
        self.assertEqual(cells[0], "3", f"total_fetched should be 3, got {cells[0]} (full cells={cells})")


# ── strict auto-keep gates (volume + distribution) ────────────────────────────

class TestStrictAutoKeepThresholds(unittest.TestCase):
    """Auto-keep requires BOTH gates to pass:
      • volume: total_selected ≥ AUTO_KEEP_MIN_SELECTED (5)
      • distribution: days_with_content ≥ MIN_DAYS_WITH_CONTENT (2)
    Either gate failing → auto-remove. Distribution gate prevents promoting
    sources that pass the volume gate via a single bursty day (Politico Europe
    pattern: 3 articles all on day 1, 0 on days 2-3 under old rules)."""

    def test_constants_match_strict_thresholds(self):
        self.assertEqual(tm.AUTO_KEEP_MIN_SELECTED, 5)
        self.assertEqual(tm.MIN_DAYS_WITH_CONTENT, 2)

    def test_auto_keep_at_exact_thresholds(self):
        """5 selected over exactly 2 days (3 + 2) → auto-graduated."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            log_entries = [
                {"ts": f"{_days_ago(2)}T08:00:00+08:00", "source": "Borderline",
                 "fetched": 3, "selected": 3},
                {"ts": f"{_days_ago(1)}T08:00:00+08:00", "source": "Borderline",
                 "fetched": 2, "selected": 2},
            ]
            with open(h.log_path, "w") as f:
                for e in log_entries:
                    f.write(json.dumps(e) + "\n")
            h.write_registry([
                _trialing("Borderline", "https://b.com/f", category="europe",
                          start_days_ago=4),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["Borderline"], "production")

    def test_auto_remove_below_selected_threshold(self):
        """4 selected over 3 days (2+1+1) → auto-removed.
        Distribution OK (3 ≥ 2) but volume fails (4 < 5)."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            log_entries = [
                {"ts": f"{_days_ago(3)}T08:00:00+08:00", "source": "LowVolume",
                 "fetched": 2, "selected": 2},
                {"ts": f"{_days_ago(2)}T08:00:00+08:00", "source": "LowVolume",
                 "fetched": 1, "selected": 1},
                {"ts": f"{_days_ago(1)}T08:00:00+08:00", "source": "LowVolume",
                 "fetched": 1, "selected": 1},
            ]
            with open(h.log_path, "w") as f:
                for e in log_entries:
                    f.write(json.dumps(e) + "\n")
            h.write_registry([
                _trialing("LowVolume", "https://l.com/f", category="europe",
                          start_days_ago=4),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["LowVolume"], "rejected")

    def test_auto_remove_below_days_threshold_politico_pattern(self):
        """6 selected ALL on a single day → auto-removed.
        Volume OK (6 ≥ 5) but distribution fails (1 < 2). This is the exact
        pattern that motivated the rule: Politico Europe trial 2026-04-26→29
        had 3 selected on day 1, 0 on days 2–3."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            log_entries = [
                {"ts": f"{_days_ago(2)}T08:00:00+08:00", "source": "Spike",
                 "fetched": 6, "selected": 6},
            ]
            with open(h.log_path, "w") as f:
                for e in log_entries:
                    f.write(json.dumps(e) + "\n")
            h.write_registry([
                _trialing("Spike", "https://s.com/f", category="europe",
                          start_days_ago=4),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["Spike"], "rejected")

    def test_auto_remove_when_both_gates_fail(self):
        """3 selected on a single day → both gates fail (3 < 5 AND 1 < 2)."""
        with tempfile.TemporaryDirectory() as d:
            h = _CmdRunHarness(d)
            log_entries = [
                {"ts": f"{_days_ago(2)}T08:00:00+08:00", "source": "Both",
                 "fetched": 3, "selected": 3},
            ]
            with open(h.log_path, "w") as f:
                for e in log_entries:
                    f.write(json.dumps(e) + "\n")
            h.write_registry([
                _trialing("Both", "https://b.com/f", category="europe",
                          start_days_ago=4),
            ])
            _run_cmd_run(h)
            reg = h.read_registry()
        statuses = {s["name"]: s["status"] for s in reg["sources"]}
        self.assertEqual(statuses["Both"], "rejected")

    def test_auto_decision_email_references_both_thresholds(self):
        """Auto-decision email body must reference both numerical thresholds
        (≥ 5 篇 + ≥ 2 天) so the recipient can interpret the decision."""
        trial = {
            "name": "Politico Pattern",
            "url": "https://p.com/f",
            "category": "europe",
            "language": "en",
            "scores": {"final": 0.91},
            "trial": {
                "start_date": "2026-04-26",
                "end_date": "2026-04-29",
                "candidate_score": 0.91,
                "daily_stats": [
                    {"date": "2026-04-27", "fetched": 6, "selected": 6},
                    {"date": "2026-04-28", "fetched": 0, "selected": 0},
                    {"date": "2026-04-29", "fetched": 0, "selected": 0},
                ],
            },
        }
        html = tm._render_auto_decision_html(
            trial, kept=False, total_selected=6,
            smtp_user="bot@example.com", mail_to="user@example.com",
        )
        self.assertIn("≥ 5", html, "volume threshold (5 篇) must appear")
        self.assertIn("≥ 2", html, "distribution threshold (2 天) must appear")


if __name__ == "__main__":
    unittest.main()
