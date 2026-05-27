#!/usr/bin/env python3
"""Tests for scripts/backfill_legacy_to_registry.py"""
import os
import json
import importlib.util

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "backfill_legacy_to_registry",
    os.path.join(_repo, "scripts", "backfill_legacy_to_registry.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
backfill = _mod.backfill


def _write(tmp_path, name, payload):
    p = str(tmp_path / name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return p


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_add_new_legacy_source(tmp_path):
    sources = _write(tmp_path, "sources.json", {
        "news_sources": {"rss_feeds": [
            {"name": "Bloomberg", "url": "https://bloomberg.com/feed", "keywords": [], "limit": 4},
        ]}
    })
    registry = _write(tmp_path, "registry.json", {"version": 1, "sources": []})

    added = backfill(sources_path=sources, registry_path=registry)

    assert added == ["Bloomberg"]
    reg = _load(registry)
    s = reg["sources"][0]
    assert s["name"] == "Bloomberg"
    assert s["status"] == "production"
    assert s["discovered_via"] == "legacy"
    assert s["production"] == {"keywords": [], "limit": 4}


def test_skip_existing_by_url(tmp_path):
    sources = _write(tmp_path, "sources.json", {
        "news_sources": {"rss_feeds": [
            {"name": "Same", "url": "https://x.com/feed", "keywords": [], "limit": 3},
        ]}
    })
    registry = _write(tmp_path, "registry.json", {"version": 1, "sources": [
        {"name": "Same", "url": "https://x.com/feed", "status": "production"},
    ]})

    added = backfill(sources_path=sources, registry_path=registry)
    assert added == []


def test_skip_url_drift_by_name(tmp_path):
    """Registry has source under old URL, sources-config under new URL (auto-swap drift).

    澎湃新闻 case: registry url=rsshub.rssforever.com/thepaper/featured (production),
    sources-config url=plink.anyfeeder.com/thepaper. Backfill must NOT add duplicate.
    """
    sources = _write(tmp_path, "sources.json", {
        "news_sources": {"rss_feeds": [
            {"name": "ThePaper", "url": "https://new-host.com/feed", "keywords": [], "limit": 3},
        ]}
    })
    registry = _write(tmp_path, "registry.json", {"version": 1, "sources": [
        {"name": "ThePaper", "url": "https://old-host.com/feed", "status": "production"},
    ]})

    added = backfill(sources_path=sources, registry_path=registry)
    assert added == []


def test_dry_run_does_not_write(tmp_path):
    sources = _write(tmp_path, "sources.json", {
        "news_sources": {"rss_feeds": [
            {"name": "DryRun", "url": "https://dry.com", "keywords": [], "limit": 3},
        ]}
    })
    registry = _write(tmp_path, "registry.json", {"version": 1, "sources": []})

    added = backfill(sources_path=sources, registry_path=registry, dry_run=True)
    assert added == ["DryRun"]
    # File on disk is unchanged
    reg = _load(registry)
    assert reg["sources"] == []


def test_idempotent_rerun(tmp_path):
    sources = _write(tmp_path, "sources.json", {
        "news_sources": {"rss_feeds": [
            {"name": "Once", "url": "https://once.com", "keywords": ["a"], "limit": 6},
        ]}
    })
    registry = _write(tmp_path, "registry.json", {"version": 1, "sources": []})

    first = backfill(sources_path=sources, registry_path=registry)
    second = backfill(sources_path=sources, registry_path=registry)
    assert first == ["Once"]
    assert second == []


def test_preserves_existing_sources(tmp_path):
    sources = _write(tmp_path, "sources.json", {
        "news_sources": {"rss_feeds": [
            {"name": "NewLegacy", "url": "https://new.com", "keywords": [], "limit": 3},
        ]}
    })
    registry = _write(tmp_path, "registry.json", {"version": 1, "sources": [
        {"name": "OriginalProd", "url": "https://old.com", "status": "production"},
        {"name": "OriginalRej", "url": "https://rej.com", "status": "rejected"},
    ]})

    added = backfill(sources_path=sources, registry_path=registry)
    assert added == ["NewLegacy"]
    reg = _load(registry)
    names = {s["name"] for s in reg["sources"]}
    assert names == {"OriginalProd", "OriginalRej", "NewLegacy"}
