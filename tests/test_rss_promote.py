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


def _make_candidates(tmp_path, entries: list) -> str:
    path = str(tmp_path / "candidates.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"discovered": entries}, f)
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
    """Promote a valid unpromoted candidate: sets promoted=True, adds to rss_feeds."""
    candidate = {
        "name": "Test Feed",
        "url": "https://example.com/rss",
        "language": "en",
        "category": "tech",
        "promoted": False,
        "rejected": False,
    }
    candidates_file = _make_candidates(tmp_path, [candidate])
    sources_file = _make_sources(tmp_path, [])

    result = promote_candidate(
        name="Test Feed",
        limit=5,
        candidates_file=candidates_file,
        sources_file=sources_file,
    )

    assert result is True

    # Candidate should now have promoted=True
    cdata = _load_json(candidates_file)
    promoted_entry = next(
        e for e in cdata["discovered"] if e["name"] == "Test Feed"
    )
    assert promoted_entry["promoted"] is True

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
    """Return False when no candidate with that name exists."""
    candidates_file = _make_candidates(tmp_path, [])
    sources_file = _make_sources(tmp_path, [])

    result = promote_candidate(
        name="Nonexistent Feed",
        candidates_file=candidates_file,
        sources_file=sources_file,
    )

    assert result is False

    # Sources file must remain untouched
    sdata = _load_json(sources_file)
    assert sdata["news_sources"]["rss_feeds"] == []


def test_promote_already_promoted(tmp_path):
    """Return False when candidate is already marked promoted=True."""
    candidate = {
        "name": "Already Done",
        "url": "https://example.com/already",
        "promoted": True,
        "rejected": False,
    }
    candidates_file = _make_candidates(tmp_path, [candidate])
    sources_file = _make_sources(tmp_path, [])

    result = promote_candidate(
        name="Already Done",
        candidates_file=candidates_file,
        sources_file=sources_file,
    )

    assert result is False

    # Sources file must remain untouched
    sdata = _load_json(sources_file)
    assert sdata["news_sources"]["rss_feeds"] == []
