#!/usr/bin/env python3
"""Tests for rss-demote-source.py"""
import os
import json
import importlib.util

# Load the dashed-filename module
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rss_demote_source",
    os.path.join(_repo, "rss-demote-source.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
demote_source = _mod.demote_source


def _make_registry(tmp_path, sources: list) -> str:
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


def test_demote_existing_production(tmp_path):
    """Standard path: registry status: production -> rejected + reject_reason; feed removed from sources."""
    source = {
        "name": "Dead Feed",
        "url": "https://example.com/dead",
        "status": "production",
        "production": {"keywords": [], "limit": 3},
    }
    registry_file = _make_registry(tmp_path, [source])
    sources_file = _make_sources(tmp_path, [
        {"name": "Dead Feed", "url": "https://example.com/dead", "keywords": [], "limit": 3},
        {"name": "Other", "url": "https://example.com/other", "keywords": [], "limit": 3},
    ])

    result = demote_source(
        name="Dead Feed",
        reason="persistent-timeout",
        registry_file=registry_file,
        sources_file=sources_file,
    )

    assert result is True

    rdata = _load_json(registry_file)
    demoted = next(s for s in rdata["sources"] if s["name"] == "Dead Feed")
    assert demoted["status"] == "rejected"
    assert demoted["reject_reason"] == "persistent-timeout"

    sdata = _load_json(sources_file)
    feeds = sdata["news_sources"]["rss_feeds"]
    assert len(feeds) == 1
    assert feeds[0]["name"] == "Other"


def test_demote_nonexistent(tmp_path):
    """Return False when source not in registry."""
    registry_file = _make_registry(tmp_path, [])
    sources_file = _make_sources(tmp_path, [])

    result = demote_source(
        name="Missing",
        reason="test",
        registry_file=registry_file,
        sources_file=sources_file,
    )
    assert result is False


def test_demote_already_rejected_idempotent(tmp_path):
    """Return False when source is already rejected (idempotent no-op)."""
    source = {
        "name": "Already Dead",
        "url": "https://example.com/x",
        "status": "rejected",
        "reject_reason": "earlier",
    }
    registry_file = _make_registry(tmp_path, [source])
    sources_file = _make_sources(tmp_path, [])

    result = demote_source(
        name="Already Dead",
        reason="new-reason",
        registry_file=registry_file,
        sources_file=sources_file,
    )
    assert result is False
    # Reason should NOT be overwritten
    rdata = _load_json(registry_file)
    assert rdata["sources"][0]["reject_reason"] == "earlier"


def test_demote_drift_case_not_in_sources_config(tmp_path):
    """Registry shows production but news-sources-config doesn't (the Endpoints/Nikkei case).

    Demote should still flip registry status, sources file is untouched.
    """
    source = {
        "name": "Orphan Prod",
        "url": "https://example.com/orphan",
        "status": "production",
        "production": {"keywords": [], "limit": 3},
    }
    registry_file = _make_registry(tmp_path, [source])
    sources_file = _make_sources(tmp_path, [
        {"name": "Unrelated", "url": "https://example.com/u", "keywords": [], "limit": 3},
    ])

    result = demote_source(
        name="Orphan Prod",
        reason="drift-cleanup",
        registry_file=registry_file,
        sources_file=sources_file,
    )
    assert result is True
    rdata = _load_json(registry_file)
    assert rdata["sources"][0]["status"] == "rejected"
    assert rdata["sources"][0]["reject_reason"] == "drift-cleanup"
    sdata = _load_json(sources_file)
    assert len(sdata["news_sources"]["rss_feeds"]) == 1
    assert sdata["news_sources"]["rss_feeds"][0]["name"] == "Unrelated"


def test_demote_trial_source_refuses(tmp_path):
    """Demote is for production sources only — trialing should be refused (trial manager handles those)."""
    source = {
        "name": "Trialing",
        "url": "https://example.com/t",
        "status": "trialing",
        "trial": {"start_date": "2026-05-26"},
    }
    registry_file = _make_registry(tmp_path, [source])
    sources_file = _make_sources(tmp_path, [])

    result = demote_source(
        name="Trialing",
        reason="should-not-touch",
        registry_file=registry_file,
        sources_file=sources_file,
    )
    assert result is False
    rdata = _load_json(registry_file)
    assert rdata["sources"][0]["status"] == "trialing"
