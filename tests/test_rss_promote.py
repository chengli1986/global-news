#!/usr/bin/env python3
"""Tests for rss-promote-candidate.py"""
import os
import sys
import json
import importlib
import importlib.util
import pytest

# Load the dashed-filename module
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rss_promote_candidate",
    os.path.join(_repo, "rss-promote-candidate.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
promote_candidate = _mod.promote_candidate


def _make_registry(tmp_path, sources: list) -> str:
    """Create a registry file with given sources."""
    path = str(tmp_path / "registry.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "sources": sources}, f)
    return path


def _make_sources(tmp_path, feeds: list) -> str:
    path = str(tmp_path / "sources.json")
    data = {"news_sources": {"rss_feeds": feeds}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_promote_existing_candidate(tmp_path):
    """Promote a valid discovered candidate: sets status=production, adds to rss_feeds."""
    source = {
        "name": "Test Feed",
        "url": "https://example.com/rss",
        "language": "en",
        "category": "tech",
        "status": "discovered",
        "scores": {"final": 0.95},
    }
    registry_file = _make_registry(tmp_path, [source])
    sources_file = _make_sources(tmp_path, [])

    result = promote_candidate(
        name="Test Feed",
        limit=5,
        registry_file=registry_file,
        sources_file=sources_file,
    )

    assert result is True

    # Registry source should now have status=production
    rdata = _load_json(registry_file)
    promoted_source = next(
        s for s in rdata["sources"] if s["name"] == "Test Feed"
    )
    assert promoted_source["status"] == "production"
    assert promoted_source["production"]["limit"] == 5
    assert promoted_source["production"]["keywords"] == []

    # Source should be added to rss_feeds
    sdata = _load_json(sources_file)
    feeds = sdata["news_sources"]["rss_feeds"]
    assert len(feeds) == 1
    added = feeds[0]
    assert added["name"] == "Test Feed"
    assert added["url"] == "https://example.com/rss"
    assert added["keywords"] == []
    assert added["limit"] == 5


def test_promote_nonexistent(tmp_path):
    """Return False when no discovered candidate with that name exists."""
    registry_file = _make_registry(tmp_path, [])
    sources_file = _make_sources(tmp_path, [])

    result = promote_candidate(
        name="Nonexistent Feed",
        registry_file=registry_file,
        sources_file=sources_file,
    )

    assert result is False

    # Sources file must remain untouched
    sdata = _load_json(sources_file)
    assert sdata["news_sources"]["rss_feeds"] == []


def test_promote_already_production(tmp_path):
    """Return False when candidate is already in production status."""
    source = {
        "name": "Already Done",
        "url": "https://example.com/already",
        "status": "production",
        "production": {"keywords": [], "limit": 3},
    }
    registry_file = _make_registry(tmp_path, [source])
    sources_file = _make_sources(tmp_path, [])

    result = promote_candidate(
        name="Already Done",
        registry_file=registry_file,
        sources_file=sources_file,
    )

    assert result is False

    # Sources file must remain untouched
    sdata = _load_json(sources_file)
    assert sdata["news_sources"]["rss_feeds"] == []
