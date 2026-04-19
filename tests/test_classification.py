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


# ===== Task 3: Stage 2 soft lock + escape =====


class TestStage2SoftLock:
    """Spec §4.1 Stage 2: 14 soft-lock sources (9 CHINA-bias + 5 ASIA-bias) default
    to their geographic region; escape to LLM when external geo dominates title.
    """

    def test_jiemian_china_topic_stays(self):
        """界面 article about Chinese company stays in CHINA (no escape)."""
        sender = _make_sender_with_news({
            "界面新闻": [("宁德时代为什么赚这么多钱", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("界面新闻", 0))
        assert entry is not None
        assert entry["reason_code"] == "source_lock:soft:界面新闻"
        assert entry["region"] == "🇨🇳 中国要闻 CHINA"

    def test_jiemian_us_topic_escapes(self):
        """界面 article about Trump/US (no Chinese keyword) escapes to LLM."""
        sender = _make_sender_with_news({
            "界面新闻": [("拜登签署对台法案引发关注", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("界面新闻", 0))
        # Wait — "对台法案" mentions 台 but our own_geo regex matches 台湾 not 台
        # Actually the title has 拜登 (escape) but no own-geo keyword → should escape
        assert entry is not None
        assert entry["reason_code"] == "soft_escape:界面新闻"
        assert entry["region"] is None  # LLM will set region in Task 5

    def test_jiemian_mixed_keeps(self):
        """界面 article mentioning both Trump and 中国 keeps soft-lock (own-geo wins)."""
        sender = _make_sender_with_news({
            "界面新闻": [("中国回应特朗普对华关税升级", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("界面新闻", 0))
        assert entry is not None
        assert entry["reason_code"] == "source_lock:soft:界面新闻"
        assert entry["region"] == "🇨🇳 中国要闻 CHINA"

    def test_scmp_hk_asian_topic_stays(self):
        """SCMP HK article about Singapore stays in ASIA-PAC."""
        sender = _make_sender_with_news({
            "SCMP Hong Kong": [("Singapore housing prices surge 8%", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("SCMP Hong Kong", 0))
        assert entry is not None
        assert entry["reason_code"] == "source_lock:soft:SCMP Hong Kong"
        assert entry["region"] == "🌏 亚太要闻 ASIA-PACIFIC"

    def test_scmp_hk_us_topic_escapes(self):
        """SCMP HK article about Putin meeting Trump (no Asia keyword) escapes."""
        sender = _make_sender_with_news({
            "SCMP Hong Kong": [("Putin meets Trump in Washington", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("SCMP Hong Kong", 0))
        assert entry is not None
        assert entry["reason_code"] == "soft_escape:SCMP Hong Kong"
        assert entry["region"] is None

    def test_huxiu_pure_tech_stays(self):
        """虎嗅 article about Chinese tech company stays in CHINA."""
        sender = _make_sender_with_news({
            "虎嗅": [("国产 GPU 厂商完成新一轮融资", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("虎嗅", 0))
        assert entry is not None
        assert entry["reason_code"] == "source_lock:soft:虎嗅"
        assert entry["region"] == "🇨🇳 中国要闻 CHINA"

    def test_non_softlock_source_no_stage2_entry(self):
        """Bloomberg (not soft-locked) → no Stage 2 entry, falls to to_classify."""
        sender = _make_sender_with_news({
            "Bloomberg": [("Fed signals April hold", "u1", None, None)],
        })
        sender.classify_articles()
        # Without LLM, no Stage 2 entry should exist for Bloomberg
        assert ("Bloomberg", 0) not in sender._classifications

    def test_36kr_chinese_company_no_external_keyword_stays(self):
        """36氪 article about ByteDance with no external geo keyword stays in CHINA."""
        sender = _make_sender_with_news({
            "36氪": [("字节跳动发布新一代大模型", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("36氪", 0))
        assert entry is not None
        # 字节跳动 / 大模型 has no external geo keyword in title → no escape, stays CHINA
        assert entry["reason_code"] == "source_lock:soft:36氪"
        assert entry["region"] == "🇨🇳 中国要闻 CHINA"


# ===== Task 4: Stage 3 geo keyword funnel =====


class TestStage3GeoKeyword:
    """Spec §4.1 Stage 3: non-locked / soft-escape articles with strong geo keyword
    in title route to CANADA / ASIA-PAC without LLM.
    """

    def test_canada_chinese_keyword(self):
        """SCMP article (not soft-locked) mentioning 加拿大 → CANADA."""
        sender = _make_sender_with_news({
            "SCMP": [("加拿大男子Kenneth Law为避免谋杀审判认罪自杀工具案", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("SCMP", 0))
        assert entry is not None
        assert entry["reason_code"] == "geo_keyword:canada"
        assert entry["region"] == "🇨🇦 加拿大 CANADA"

    def test_canada_english_keyword(self):
        """FT article mentioning Trudeau → CANADA."""
        sender = _make_sender_with_news({
            "FT": [("Trudeau announces budget package", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("FT", 0))
        assert entry is not None
        assert entry["reason_code"] == "geo_keyword:canada"
        assert entry["region"] == "🇨🇦 加拿大 CANADA"

    def test_asia_japan(self):
        """Bloomberg article about Japan → ASIA-PAC."""
        sender = _make_sender_with_news({
            "Bloomberg": [("Japan inflation hits 4% in Q3", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("Bloomberg", 0))
        assert entry is not None
        assert entry["reason_code"] == "geo_keyword:asia_pac"
        assert entry["region"] == "🌏 亚太要闻 ASIA-PACIFIC"

    def test_asia_india(self):
        """NYT Business article about India IPO → ASIA-PAC."""
        sender = _make_sender_with_news({
            "NYT Business": [("India IPO market booms past $10bn", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("NYT Business", 0))
        assert entry is not None
        assert entry["reason_code"] == "geo_keyword:asia_pac"
        assert entry["region"] == "🌏 亚太要闻 ASIA-PACIFIC"

    def test_asia_taiwan_via_tsmc(self):
        """Bloomberg article mentioning TSMC → ASIA-PAC (matches TSMC OR Taiwan)."""
        sender = _make_sender_with_news({
            "Bloomberg": [("TSMC quarterly results beat expectations", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("Bloomberg", 0))
        assert entry is not None
        assert entry["reason_code"] == "geo_keyword:asia_pac"

    def test_no_geo_keyword_returns_none(self):
        """BBC article about Federal Reserve (no Canada/Asia keyword) → no Stage 3 entry."""
        sender = _make_sender_with_news({
            "BBC World": [("Federal Reserve hikes rates 25bps", "u1", None, None)],
        })
        sender.classify_articles()
        # No LLM keys → no LLM entry, no Stage 3 match → no entry at all
        assert ("BBC World", 0) not in sender._classifications

    def test_stage3_overrides_soft_escape(self):
        """界面 article that escapes Stage 2 (mentions Trudeau) gets Stage 3 to CANADA."""
        sender = _make_sender_with_news({
            "界面新闻": [("Trudeau宣布对华关税新政策", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("界面新闻", 0))
        # Trudeau triggers escape (in _ESCAPE_EXTERNAL_GEO list as "Trudeau")
        # AND no own-geo keyword (对华 is not in _OWN_GEO_PER_REGION CHINA list)
        # So Stage 2 escapes → Stage 3 sees Trudeau → routes to CANADA
        assert entry is not None
        assert entry["reason_code"] == "geo_keyword:canada"
        assert entry["region"] == "🇨🇦 加拿大 CANADA"

    def test_stage3_does_not_override_hard_lock(self):
        """CBC article whose title mentions Tokyo stays in CANADA (hard-lock wins)."""
        sender = _make_sender_with_news({
            "CBC Business": [("Air Canada launches new Tokyo route", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("CBC Business", 0))
        assert entry is not None
        # Hard lock wins — Stage 3 does NOT override
        assert entry["reason_code"] == "source_lock:hard:CBC Business"
