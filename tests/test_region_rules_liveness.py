#!/usr/bin/env python3
"""Guard: every source named in a routing rule must still exist in the config.

Found 2026-08-09 while rebuilding the docs-site "sources at a glance" panel.
The source pool had turned over repeatedly, but the three routing tables in
unified-global-news-sender.py still named sources that had been removed from
news-sources-config.json months earlier:

  - _SOFT_LOCKS      10 of 14 entries dead (界面新闻/36氪/日经中文/SCMP Hong Kong/…)
  - REGION_GROUPS    11 of 40 entries dead
  - _LOCKED_SOURCES  1 of 6 dead (CBC Business)
  - SOURCE_TO_REGION 11 of 34 dead (evaluate_digest.py — a fourth copy of the
                     same mapping, which the sender-vs-evaluator agreement test
                     could not catch because it only checks one direction)

Dead entries are harmless at runtime — a lookup simply never matches — which
is exactly why they accumulated silently for months and why every test stayed
green. They are NOT harmless as documentation: the stale ASIA-PACIFIC soft-lock
list was what made the public site claim 日经中文 / SCMP Hong Kong / HKFP were
still being aggregated.

This guard fails the moment a source is dropped from the config without its
routing entries being cleaned up, so the tables can never silently rot again.

NOTE: the reverse direction is deliberately NOT asserted. A source that appears
in NO routing table is legitimate — since the Task 6/7 classifier redesign new
sources are routed by the Stage 4 LLM and fall back to REGION_OTHER, so only
legacy sources carry manual entries (see unified-global-news-sender.py:1334).
"""
import json
import os
import re

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SENDER = os.path.join(_REPO, "unified-global-news-sender.py")
_CONFIG = os.path.join(_REPO, "news-sources-config.json")


def _configured_sources() -> set[str]:
    """Every source name the production config actually fetches."""
    groups = json.load(open(_CONFIG))["news_sources"]
    return {
        entry["name"]
        for group in groups.values()
        if isinstance(group, list)
        for entry in group
        if isinstance(entry, dict) and entry.get("name")
    }


def _sender_src() -> str:
    return open(_SENDER, encoding="utf-8").read()


def _block(pattern: str) -> str:
    """Extract one routing table's literal body from the sender source."""
    match = re.search(pattern, _sender_src(), re.S)
    assert match, f"routing table not found — pattern changed? {pattern!r}"
    return match.group(1)


def _quoted(body: str) -> list[str]:
    return [name for name in re.findall(r'"([^"]+)"', body) if not name.startswith("REGION_")]


def test_soft_lock_sources_all_exist():
    """_SOFT_LOCKS may only name sources the config still fetches."""
    live = _configured_sources()
    named = re.findall(r'"([^"]+)":\s*REGION_', _block(r'_SOFT_LOCKS = \{(.*?)\n\}'))
    assert named, "no soft-lock entries parsed — table shape changed?"
    dead = sorted(n for n in named if n not in live)
    assert not dead, f"_SOFT_LOCKS names sources no longer in the config: {dead}"


def test_region_group_sources_all_exist():
    """REGION_GROUPS may only name sources the config still fetches."""
    live = _configured_sources()
    named = _quoted(_block(r'REGION_GROUPS = \[(.*?)\n    \]'))
    assert named, "no REGION_GROUPS entries parsed — table shape changed?"
    dead = sorted(set(n for n in named if n not in live))
    assert not dead, f"REGION_GROUPS names sources no longer in the config: {dead}"


def test_locked_sources_all_exist():
    """_LOCKED_SOURCES (Stage 1 hard locks) may only name live sources."""
    live = _configured_sources()
    named = _quoted(_block(r'_LOCKED_SOURCES = \{(.*?)\}'))
    assert named, "no _LOCKED_SOURCES entries parsed — table shape changed?"
    dead = sorted(n for n in named if n not in live)
    assert not dead, f"_LOCKED_SOURCES names sources no longer in the config: {dead}"


def test_evaluator_source_map_all_exist():
    """evaluate_digest.SOURCE_TO_REGION may only name live sources.

    This is a fourth copy of the source→region mapping. The existing
    sender-vs-evaluator agreement test only asserts sender ⊆ evaluator, so
    entries orphaned in the evaluator stay invisible to it.
    """
    import importlib.util

    live = _configured_sources()
    spec = importlib.util.spec_from_file_location(
        "evaluate_digest", os.path.join(_REPO, "evaluate_digest.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dead = sorted(n for n in mod.SOURCE_TO_REGION if n not in live)
    assert not dead, f"SOURCE_TO_REGION names sources no longer in the config: {dead}"
