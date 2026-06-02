#!/usr/bin/env python3
"""Tests for rss-health-check.py config write path — symlink preservation.

Root cause (found 2026-06-02): the production workspace config
(~/.openclaw/workspace/news-sources-config.json) is meant to be a symlink to
the git-managed repo config (same pattern as digest-tuning.json). But
swap_url_in_config()'s atomic write (tempfile + os.replace) replaced the
symlink itself with a regular file on the first auto-swap, silently splitting
production config from the repo. Result: trial-promoted sources never reached
production, and sources removed from the repo kept being fetched.
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
