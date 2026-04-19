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

# Module loaded by tests/conftest.py under the dashed-name-to-underscore alias
from unified_global_news_sender import (
    TOPIC_LABELS,
    GEO_LABELS,
    SUBTOPIC_LABELS,
    REASON_PREFIXES,
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
