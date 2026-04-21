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


if __name__ == "__main__":
    unittest.main()
