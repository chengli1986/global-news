#!/usr/bin/env python3
"""Tests for the news classification redesign (4-stage funnel + 2-axis labels).

Spec:  docs/superpowers/specs/2026-04-19-classification-redesign.md
Plan:  docs/superpowers/plans/2026-04-19-classification-redesign.md

This file accumulates tests across Tasks 1-11. New tests should be appended
under task-specific class/section markers below.
"""
import sys
import os

sys.path.insert(0, os.path.expanduser("~/global-news"))

from unittest.mock import patch

# Module loaded by tests/conftest.py under the dashed-name-to-underscore alias
from unified_global_news_sender import (
    TOPIC_LABELS,
    GEO_LABELS,
    SUBTOPIC_LABELS,
    REASON_PREFIXES,
    UnifiedNewsSender,
)


# ===== Task 1: Label vocabularies =====


class TestLabelVocabularies:
    """Validate the topic / geo / subtopic / reason_code constants are well-formed.

    These constants are the contract between the LLM prompt, routing matrix, and
    monitoring; keep in sync with spec §4.2-§4.4 if anything changes here.
    """

    def test_topic_labels_well_formed(self):
        assert len(TOPIC_LABELS) == 5
        assert TOPIC_LABELS == {"politics", "business", "tech", "consumer_tech", "society"}
        for label in TOPIC_LABELS:
            assert label == label.lower(), f"{label!r} not lowercase"
            assert " " not in label, f"{label!r} contains whitespace"

    def test_geo_labels_well_formed(self):
        assert len(GEO_LABELS) == 6
        assert GEO_LABELS == {"china", "canada", "asia_other", "us", "europe", "global"}
        for label in GEO_LABELS:
            assert label == label.lower(), f"{label!r} not lowercase"
            assert " " not in label, f"{label!r} contains whitespace"

    def test_subtopic_labels_only_for_tech_business(self):
        # Subtopics are only required for tech and business topics
        assert set(SUBTOPIC_LABELS.keys()) == {"tech", "business"}
        assert SUBTOPIC_LABELS["tech"] == {"tech_ai", "tech_consumer"}
        assert SUBTOPIC_LABELS["business"] == {"business_macro", "business_corp"}
        # Defensive: parent topics in the subtopic dict must also exist in TOPIC_LABELS
        for parent in SUBTOPIC_LABELS:
            assert parent in TOPIC_LABELS, f"subtopic parent {parent!r} not in TOPIC_LABELS"

    def test_reason_prefixes_well_formed(self):
        # Bonus check: REASON_PREFIXES used by Task 8 monitoring should be
        # well-formed strings with no trailing whitespace or duplicates
        assert len(REASON_PREFIXES) == 9
        for prefix in REASON_PREFIXES:
            assert prefix == prefix.strip(), f"{prefix!r} has whitespace"
            assert ":" in prefix or prefix == "soft_escape", f"{prefix!r} should be namespaced with ':'"


# ===== Task 2: Stage 1 hard lock — annotate reason_code =====


def _make_sender_with_news(news_data: dict) -> UnifiedNewsSender:
    """Build a UnifiedNewsSender with pre-populated news_data, no LLM keys.

    classify_articles will populate Stage 1 hard-lock entries even without LLM
    (LLM section is gated by 'if not self._openai_key' early return).
    """
    sender = UnifiedNewsSender()
    sender.news_data = news_data
    sender._llm_status = []
    # Force no LLM keys so classify_articles only does Stage 1 work
    sender._openai_key = None
    sender._gemini_key = None
    return sender


class TestStage1HardLock:
    """Spec §4.1 Stage 1: 6 hard-lock sources skip LLM and stay in source-default region.
    Each gets a _classifications entry with reason_code='source_lock:hard:<src>'.
    """

    def test_cbc_business_creates_hard_lock_entry(self):
        sender = _make_sender_with_news({
            "CBC Business": [("Air Canada suspends 6 routes", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("CBC Business", 0))
        assert entry is not None
        assert entry["reason_code"] == "source_lock:hard:CBC Business"
        assert entry["region"] is None  # None = stay in REGION_GROUPS default (CANADA)
        assert entry["topic"] is None
        assert entry["geo"] is None

    def test_globe_and_mail_creates_hard_lock_entry(self):
        sender = _make_sender_with_news({
            "Globe & Mail": [("How AI promise takes hold at Canada banks", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("Globe & Mail", 0))
        assert entry is not None
        assert entry["reason_code"] == "source_lock:hard:Globe & Mail"

    def test_economist_finance_creates_hard_lock_entry(self):
        sender = _make_sender_with_news({
            "Economist Finance": [("Wealth advisers earn $2bn", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("Economist Finance", 0))
        assert entry is not None
        assert entry["reason_code"] == "source_lock:hard:Economist Finance"

    def test_non_locked_source_no_hard_lock_entry(self):
        sender = _make_sender_with_news({
            "Bloomberg": [("Fed signals April hold", "u1", None, None)],
        })
        sender.classify_articles()
        # Bloomberg is not hard-locked; without LLM keys, no entry should exist
        assert ("Bloomberg", 0) not in sender._classifications

    def test_reclassify_article_returns_none_for_hard_lock(self):
        """_reclassify_article on a hard-lock entry returns None (keep in source default)."""
        sender = _make_sender_with_news({
            "CBC Business": [("Some Canadian biz title", "u1", None, None)],
        })
        sender.classify_articles()
        result = sender._reclassify_article("Some Canadian biz title", "CBC Business", 0)
        assert result is None  # None means "keep in REGION_GROUPS default region (CANADA)"

    def test_all_six_locked_sources_get_entries(self):
        """All 6 entries in _LOCKED_SOURCES should populate when present in news_data."""
        sender = _make_sender_with_news({
            src: [(f"Sample title for {src}", "u1", None, None)]
            for src in ["CBC Business", "Globe & Mail",
                        "Economist Leaders", "Economist Finance",
                        "Economist Business", "Economist Science"]
        })
        sender.classify_articles()
        for src in ["CBC Business", "Globe & Mail",
                    "Economist Leaders", "Economist Finance",
                    "Economist Business", "Economist Science"]:
            entry = sender._classifications.get((src, 0))
            assert entry is not None, f"{src} missing _classifications entry"
            assert entry["reason_code"].startswith("source_lock:hard"), f"{src} wrong prefix"
