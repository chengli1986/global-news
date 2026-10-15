#!/usr/bin/env python3
"""Tests for rss-health-check.py auto-swap when the fallback URL is already live.

Found 2026-10-15: 澎湃新闻's config URL had already been set to the same URL
as FALLBACK_URLS["澎湃新闻"]. After 3 stale runs the auto-swap fired as an
A→A no-op, reported "已自动切换", and wrote swapped_from = <same URL> into the
state file. From then on the source was treated as "already swapped" (no
further swap) and the revert branch skipped it (current == original), so the
bogus marker could never clear.
"""
import json
import os
import importlib.util

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rss_health_check_noop",
    os.path.join(_repo, "rss-health-check.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

NAME = "澎湃新闻"
FALLBACK = _mod.FALLBACK_URLS[NAME]


def _setup(tmp_path, monkeypatch, state, ok):
    config = tmp_path / "news-sources-config.json"
    config.write_text(json.dumps({"news_sources": {"rss_feeds": [
        {"name": NAME, "url": FALLBACK, "keywords": [], "limit": 3}]}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    state_file = tmp_path / "rss-health.json"
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(_mod, "CONFIG_FILE", str(config))
    monkeypatch.setattr(_mod, "STATE_FILE", str(state_file))
    monkeypatch.setattr(_mod, "LOGS_DIR", str(tmp_path))
    res = {"ok": ok, "error": None if ok else "stale feed (newest 138h, max 72h)",
           "article_count": 20, "newest_age_hours": 1 if ok else 138}
    monkeypatch.setattr(_mod, "check_source", lambda name, url, st, ma: (name, dict(res)))
    return config


def test_no_swap_when_fallback_equals_current_url(tmp_path, monkeypatch):
    config = _setup(tmp_path, monkeypatch, {NAME: {
        "consecutive_fails": 2, "last_check": None, "last_error": None,
        "swapped_from": None}}, ok=False)
    before = config.read_text(encoding="utf-8")

    _results, state, swapped, _reverted = _mod.run_checks()

    assert swapped == []
    assert state[NAME]["swapped_from"] is None
    assert state[NAME]["consecutive_fails"] == 3
    assert config.read_text(encoding="utf-8") == before


def test_stale_self_swap_marker_is_cleared(tmp_path, monkeypatch):
    """State already poisoned by the old A→A swap must self-heal."""
    for ok in (False, True):
        _setup(tmp_path, monkeypatch, {NAME: {
            "consecutive_fails": 3, "last_check": None, "last_error": None,
            "swapped_from": FALLBACK}}, ok=ok)

        _results, state, swapped, reverted = _mod.run_checks()

        assert state[NAME]["swapped_from"] is None, f"ok={ok}"
        assert swapped == [] and reverted == []
