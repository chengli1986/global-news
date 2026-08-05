#!/usr/bin/env python3
"""Tests for rss-health-check.py symlink handling — config writes and SCRIPT_DIR.

Root cause #1 (found 2026-06-02): the production workspace config
(~/.openclaw/workspace/news-sources-config.json) is meant to be a symlink to
the git-managed repo config (same pattern as digest-tuning.json). But
swap_url_in_config()'s atomic write (tempfile + os.replace) replaced the
symlink itself with a regular file on the first auto-swap, silently splitting
production config from the repo. Result: trial-promoted sources never reached
production, and sources removed from the repo kept being fetched.

Root cause #2 (found 2026-08-05): cron invokes this script through the same
workspace symlink, and SCRIPT_DIR was built with os.path.abspath(__file__),
which does NOT resolve symlinks. So the live state file landed in
~/.openclaw/workspace/logs/rss-health.json while any manual run from the repo
wrote ~/global-news/logs/rss-health.json — two separate ledgers, so
consecutive_fails diverged and reading the repo copy showed stale counters.
Fixed by using os.path.realpath(__file__) (same pattern already used by
unified-global-news-sender.py and evaluate_digest.py).
"""
import os
import json
import importlib.util

# Load the dashed-filename module
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rss_health_check",
    os.path.join(_repo, "rss-health-check.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _load_via(path):
    """Import rss-health-check.py from an arbitrary path (used to simulate the
    production workspace symlink invocation)."""
    spec = importlib.util.spec_from_file_location("rss_health_check_alt", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_dir_resolves_symlink(tmp_path):
    """Invoked through a symlink, SCRIPT_DIR must point at the REAL repo dir.

    Production cron runs `python3 ~/.openclaw/workspace/rss-health-check.py`,
    which is a symlink into the repo. With abspath() the state file, logs dir
    and config all resolved next to the symlink instead of in the repo.
    """
    link = tmp_path / "rss-health-check.py"
    link.symlink_to(os.path.join(_repo, "rss-health-check.py"))

    mod = _load_via(link)

    assert mod.SCRIPT_DIR == _repo, (
        "SCRIPT_DIR followed the symlink's own directory — state and config "
        "would split from the repo copy")
    assert mod.STATE_FILE == os.path.join(_repo, "logs", "rss-health.json")
    assert mod.LOGS_DIR == os.path.join(_repo, "logs")
    assert mod.CONFIG_FILE == os.path.join(_repo, "news-sources-config.json")


def test_script_dir_unchanged_for_direct_invocation():
    """Regression guard: running the real file directly still resolves to the repo."""
    assert _mod.SCRIPT_DIR == _repo
    assert _mod.STATE_FILE == os.path.join(_repo, "logs", "rss-health.json")


def _write_config(path, url="https://old.example.com/feed"):
    path.write_text(json.dumps({
        "news_sources": {
            "rss_feeds": [{"name": "Test Feed", "url": url, "keywords": [], "limit": 3}]
        }
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def test_swap_preserves_symlink(tmp_path, monkeypatch):
    """Auto-swap through a symlinked config must write to the real file and
    keep the symlink intact — NOT replace the symlink with a regular file."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    real_config = repo_dir / "news-sources-config.json"
    _write_config(real_config)

    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    link = ws_dir / "news-sources-config.json"
    link.symlink_to(real_config)

    monkeypatch.setattr(_mod, "CONFIG_FILE", str(link))

    result = _mod.swap_url_in_config(
        "https://old.example.com/feed", "https://new.example.com/feed")

    assert result is True
    # Core assertion: the symlink must survive the write
    assert link.is_symlink(), (
        "swap_url_in_config replaced the symlink with a regular file — "
        "this is the bug that split production config from the repo")
    # The new URL must be visible through both paths (written to the real file)
    assert "new.example.com" in real_config.read_text(encoding="utf-8")
    assert "new.example.com" in link.read_text(encoding="utf-8")


def test_swap_works_on_regular_file(tmp_path, monkeypatch):
    """Regression guard: swap on a plain (non-symlink) config still works."""
    config = tmp_path / "news-sources-config.json"
    _write_config(config)
    monkeypatch.setattr(_mod, "CONFIG_FILE", str(config))

    result = _mod.swap_url_in_config(
        "https://old.example.com/feed", "https://new.example.com/feed")

    assert result is True
    assert "new.example.com" in config.read_text(encoding="utf-8")
    assert not config.is_symlink()


def test_swap_returns_false_when_url_not_found(tmp_path, monkeypatch):
    """Unknown URL → no write, returns False."""
    config = tmp_path / "news-sources-config.json"
    _write_config(config)
    monkeypatch.setattr(_mod, "CONFIG_FILE", str(config))

    result = _mod.swap_url_in_config(
        "https://not-in-config.example.com/feed", "https://new.example.com/feed")

    assert result is False
    assert "old.example.com" in config.read_text(encoding="utf-8")


def test_swap_preserves_formatting_and_other_sources(tmp_path, monkeypatch):
    """Text-level swap must only touch the target URL, preserving everything else."""
    config = tmp_path / "news-sources-config.json"
    config.write_text(json.dumps({
        "news_sources": {
            "rss_feeds": [
                {"name": "Keep Me", "url": "https://keep.example.com/rss", "limit": 5},
                {"name": "Swap Me", "url": "https://old.example.com/feed", "limit": 3},
            ]
        }
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(_mod, "CONFIG_FILE", str(config))

    result = _mod.swap_url_in_config(
        "https://old.example.com/feed", "https://new.example.com/feed")

    assert result is True
    data = json.loads(config.read_text(encoding="utf-8"))
    feeds = data["news_sources"]["rss_feeds"]
    assert feeds[0]["url"] == "https://keep.example.com/rss"
    assert feeds[1]["url"] == "https://new.example.com/feed"
    assert feeds[0]["limit"] == 5
