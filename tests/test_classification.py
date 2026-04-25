#!/usr/bin/env python3
"""Tests for the news classification redesign (4-stage funnel + 2-axis labels).

Spec:  docs/superpowers/specs/2026-04-19-classification-redesign.md
Plan:  docs/superpowers/plans/2026-04-19-classification-redesign.md

This file accumulates tests across Tasks 1-11. New tests should be appended
under task-specific class/section markers below.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# ===== Task 5: LLM 3-label parser + simplified region mapping =====


class TestParse3LabelResponse:
    """Spec §5: parser tolerates flat / nested shapes; validates labels; defaults
    missing subtopic for tech/business; drops invalid topic/geo entries.

    Tests target the static helper _parse_3label_response directly (no LLM call).
    """

    def test_parse_well_formed_3label(self):
        parsed = {
            "1": {"topic": "tech", "geo": "us", "subtopic": "tech_ai"},
            "2": {"topic": "business", "geo": "china", "subtopic": "business_corp"},
        }
        result = UnifiedNewsSender._parse_3label_response(parsed)
        assert result == {
            0: {"topic": "tech", "geo": "us", "subtopic": "tech_ai"},
            1: {"topic": "business", "geo": "china", "subtopic": "business_corp"},
        }

    def test_parse_nested_classifications_shape(self):
        # Some LLMs wrap output in a single top-level key
        parsed = {
            "classifications": {
                "1": {"topic": "politics", "geo": "global"},
                "2": {"topic": "society", "geo": "canada"},
            }
        }
        result = UnifiedNewsSender._parse_3label_response(parsed)
        assert 0 in result and result[0]["topic"] == "politics"
        assert 1 in result and result[1]["geo"] == "canada"

    def test_parse_malformed_returns_empty(self):
        # parsed could be a list, string, or None on LLM error
        assert UnifiedNewsSender._parse_3label_response("garbage") == {}
        assert UnifiedNewsSender._parse_3label_response(None) == {}
        assert UnifiedNewsSender._parse_3label_response([1, 2, 3]) == {}

    def test_parse_skips_non_dict_values(self):
        # Mixed shape: some entries are strings (legacy single-label), should be skipped
        parsed = {
            "1": {"topic": "tech", "geo": "us"},
            "2": "tech",  # legacy single-label, not a dict — drop
        }
        result = UnifiedNewsSender._parse_3label_response(parsed)
        assert 0 in result
        assert 1 not in result


class TestClassifyArticlesValidation:
    """Spec §5: full classify_articles flow with mocked LLM, asserting validation +
    region mapping + reason_code preservation.
    """

    def _mock_llm(self, sender, response_dict):
        """Patch sender to return response_dict from the LLM call."""
        sender._openai_key = "fake-key-for-test"
        api_response = {"choices": [{"message": {"content": __import__("json").dumps(response_dict)}}]}

        def fake_call(payload, timeout=60):
            sender._last_provider = "MockLLM"
            return api_response
        sender._llm_api_call = fake_call

    def test_missing_subtopic_for_tech_defaults(self, capsys):
        """tech topic without subtopic → defaults to tech_ai with stdout warning."""
        sender = _make_sender_with_news({
            "TechCrunch": [("Some AI breakthrough", "u1", None, None)],
        })
        self._mock_llm(sender, {"1": {"topic": "tech", "geo": "us"}})  # no subtopic
        sender.classify_articles()
        entry = sender._classifications.get(("TechCrunch", 0))
        assert entry is not None
        assert entry["topic"] == "tech"
        assert entry["subtopic"] == "tech_ai"  # defaulted
        out = capsys.readouterr().out
        assert "missing/invalid subtopic" in out

    def test_missing_subtopic_for_politics_no_warning(self, capsys):
        """politics topic without subtopic is fine (subtopic not required for politics)."""
        sender = _make_sender_with_news({
            "BBC World": [("Iran-Israel ceasefire", "u1", None, None)],
        })
        self._mock_llm(sender, {"1": {"topic": "politics", "geo": "global"}})
        sender.classify_articles()
        entry = sender._classifications.get(("BBC World", 0))
        assert entry is not None
        assert entry["topic"] == "politics"
        out = capsys.readouterr().out
        assert "missing/invalid subtopic" not in out

    def test_invalid_topic_drops_article(self):
        """topic='news' (not in TOPIC_LABELS) → article dropped, no _classifications entry."""
        sender = _make_sender_with_news({
            "Bloomberg": [("Some title", "u1", None, None)],
        })
        self._mock_llm(sender, {"1": {"topic": "news", "geo": "us"}})
        sender.classify_articles()
        # Bloomberg is non-locked → only entry source would be LLM. Dropped → no entry.
        assert ("Bloomberg", 0) not in sender._classifications

    def test_invalid_geo_drops_article(self):
        """geo='mars' (not in GEO_LABELS) → article dropped."""
        sender = _make_sender_with_news({
            "Bloomberg": [("Some title", "u1", None, None)],
        })
        self._mock_llm(sender, {"1": {"topic": "business", "geo": "mars", "subtopic": "business_macro"}})
        sender.classify_articles()
        assert ("Bloomberg", 0) not in sender._classifications

    def test_soft_escape_preserves_reason_code(self):
        """界面 escape entry gets topic/geo set by LLM but keeps soft_escape reason_code."""
        sender = _make_sender_with_news({
            # Title triggers Stage 2 escape (Trump + no own-geo) AND no Stage 3 keyword
            "界面新闻": [("特朗普签署 H1B 签证新规", "u1", None, None)],
        })
        self._mock_llm(sender, {"1": {"topic": "politics", "geo": "us"}})
        sender.classify_articles()
        entry = sender._classifications.get(("界面新闻", 0))
        assert entry is not None
        # reason_code preserved as soft_escape (set by Stage 2), topic/geo filled by LLM
        assert entry["reason_code"] == "soft_escape:界面新闻"
        assert entry["topic"] == "politics"
        assert entry["geo"] == "us"
        assert entry["region"] == "🏛 全球政治 GLOBAL POLITICS"

    def test_region_mapping_china_business_to_china(self):
        """China + business → CHINA region (Q1B exemption via _route)."""
        sender = _make_sender_with_news({
            # NYT Business is not soft-locked, so falls through to Stage 4 LLM
            "NYT Business": [("China consumer spending rises 3%", "u1", None, None)],
        })
        self._mock_llm(sender, {"1": {"topic": "business", "geo": "china", "subtopic": "business_corp"}})
        sender.classify_articles()
        entry = sender._classifications.get(("NYT Business", 0))
        assert entry is not None
        assert entry["region"] == "🇨🇳 中国要闻 CHINA"
        # Post-Task-6 reason_code: llm:china+<topic> for Q1B routing branches
        assert entry["reason_code"] == "llm:china+business"

    def test_region_mapping_china_tech_falls_to_topic(self):
        """China + tech → AI/前沿 zone (Q1B: tech topic wins for global comparison)."""
        sender = _make_sender_with_news({
            "NYT Business": [("DeepSeek 训练成本下降 80%", "u1", None, None)],
        })
        self._mock_llm(sender, {"1": {"topic": "tech", "geo": "china", "subtopic": "tech_ai"}})
        sender.classify_articles()
        entry = sender._classifications.get(("NYT Business", 0))
        assert entry is not None
        # china + tech does NOT route to CHINA — falls through to AI/前沿 zone
        assert entry["region"] == "🧠 AI/前沿 AI FRONTIER"


# ===== Task 6: Routing matrix + 10-zone REGION_GROUPS =====


class TestRouteMatrix:
    """Spec §4.5 routing matrix. Each test exercises one branch of _route().

    _route is a static method, so tests don't need a sender instance.
    """

    @staticmethod
    def _route(topic, geo, subtopic=None):
        return UnifiedNewsSender._route(topic, geo, subtopic)

    def test_china_business_corp_to_china(self):
        """china + business → CHINA (Q1B: china+business goes to CHINA, not topic)."""
        region, reason = self._route("business", "china", "business_corp")
        assert region == "🇨🇳 中国要闻 CHINA"
        assert reason == "llm:china+business"

    def test_china_society_to_china(self):
        """china + society → CHINA (Q1B)."""
        region, reason = self._route("society", "china", None)
        assert region == "🇨🇳 中国要闻 CHINA"
        assert reason == "llm:china+society"

    def test_china_consumer_tech_to_china(self):
        """china + consumer_tech → CHINA (per spec routing matrix)."""
        region, reason = self._route("consumer_tech", "china", None)
        assert region == "🇨🇳 中国要闻 CHINA"
        assert reason == "llm:china+consumer_tech"

    def test_china_tech_ai_to_ai_frontier(self):
        """china + tech_ai → AI/前沿 (Q1B exemption: tech wins for global comparison)."""
        region, reason = self._route("tech", "china", "tech_ai")
        assert region == "🧠 AI/前沿 AI FRONTIER"
        assert reason == "llm:topic:tech_ai"

    def test_china_politics_to_politics(self):
        """china + politics → POLITICS (Q1B exemption: politics wins for global comparison)."""
        region, reason = self._route("politics", "china", None)
        assert region == "🏛 全球政治 GLOBAL POLITICS"
        assert reason == "llm:topic:politics"

    def test_canada_society_to_canada(self):
        """canada geo wins over topic for personal-context region."""
        region, reason = self._route("society", "canada", None)
        assert region == "🇨🇦 加拿大 CANADA"
        assert reason == "llm:geo:canada"

    def test_asia_other_society_to_asia_pac(self):
        """asia_other geo wins over topic for personal-context region."""
        region, reason = self._route("society", "asia_other", None)
        assert region == "🌏 亚太要闻 ASIA-PACIFIC"
        assert reason == "llm:geo:asia_other"

    def test_us_business_macro_to_macro(self):
        """us + business + business_macro → 市场/宏观."""
        region, reason = self._route("business", "us", "business_macro")
        assert region == "📈 市场/宏观 MACRO & MARKETS"
        assert reason == "llm:topic:business_macro"

    def test_us_business_corp_to_corp(self):
        """us + business + business_corp → 公司/产业."""
        region, reason = self._route("business", "us", "business_corp")
        assert region == "🏢 公司/产业 CORPORATE & INDUSTRY"
        assert reason == "llm:topic:business_corp"

    def test_us_society_to_society(self):
        """us + society → SOCIETY (new Q4 zone for non-geo society)."""
        region, reason = self._route("society", "us", None)
        assert region == "🌐 社会观察 SOCIETY"
        assert reason == "llm:topic:society"

    def test_global_politics_to_politics(self):
        """global politics → POLITICS."""
        region, reason = self._route("politics", "global", None)
        assert region == "🏛 全球政治 GLOBAL POLITICS"
        assert reason == "llm:topic:politics"

    def test_us_consumer_tech_to_consumer_tech(self):
        """us + consumer_tech (or tech+tech_consumer subtopic) → 消费科技."""
        # Direct topic="consumer_tech"
        region1, reason1 = self._route("consumer_tech", "us", None)
        assert region1 == "📱 消费科技 CONSUMER TECH"
        # Via tech topic with tech_consumer subtopic
        region2, reason2 = self._route("tech", "us", "tech_consumer")
        assert region2 == "📱 消费科技 CONSUMER TECH"

    def test_missing_topic_returns_fallback(self):
        """None topic → (None, fallback:source_default)."""
        region, reason = self._route(None, "us", None)
        assert region is None
        assert reason == "fallback:source_default"

    def test_invalid_topic_returns_fallback(self):
        """Unknown topic → (None, fallback:source_default)."""
        region, reason = self._route("news", "us", None)
        assert region is None
        assert reason == "fallback:source_default"


class TestRegionGroupsStructure:
    """Spec §4.6: 10-zone REGION_GROUPS in display order per F3."""

    def test_region_groups_has_10_zones(self):
        assert len(UnifiedNewsSender.REGION_GROUPS) == 10

    def test_all_routed_regions_in_region_groups(self):
        """Every region key emitted by _route must exist in REGION_GROUPS."""
        region_keys = {r for r, _sources in UnifiedNewsSender.REGION_GROUPS}
        # Walk a representative set of routing inputs
        test_cases = [
            ("business", "china", "business_corp"),
            ("society", "china", None),
            ("consumer_tech", "china", None),
            ("tech", "china", "tech_ai"),
            ("politics", "china", None),
            ("society", "canada", None),
            ("society", "asia_other", None),
            ("business", "us", "business_macro"),
            ("business", "us", "business_corp"),
            ("society", "us", None),
            ("politics", "global", None),
            ("consumer_tech", "us", None),
            ("tech", "us", "tech_consumer"),
        ]
        for topic, geo, subtopic in test_cases:
            region, _reason = UnifiedNewsSender._route(topic, geo, subtopic)
            if region is not None:
                assert region in region_keys, f"{(topic, geo, subtopic)} → {region!r} not in REGION_GROUPS"

    def test_region_display_order_per_f3(self):
        """First 4 zones per F3 display priority: AI → Markets → POLITICS → CHINA."""
        keys = [r for r, _ in UnifiedNewsSender.REGION_GROUPS]
        assert keys[0] == "🧠 AI/前沿 AI FRONTIER"
        assert keys[1] == "📈 市场/宏观 MACRO & MARKETS"
        assert keys[2] == "🏛 全球政治 GLOBAL POLITICS"
        assert keys[3] == "🇨🇳 中国要闻 CHINA"
        # Last 4 per F3: ASIA-PAC → CANADA → ECONOMIST → SOCIETY
        assert keys[-1] == "🌐 社会观察 SOCIETY"
        assert keys[-2] == "📕 经济学人 THE ECONOMIST"
        assert keys[-3] == "🇨🇦 加拿大 CANADA"
        assert keys[-4] == "🌏 亚太要闻 ASIA-PACIFIC"

    def test_source_default_region_lookup(self):
        """_source_default_region returns the REGION_GROUPS region containing the source."""
        sender = _make_sender_with_news({})
        assert sender._source_default_region("CBC Business") == "🇨🇦 加拿大 CANADA"
        assert sender._source_default_region("Bloomberg Econ") == "📈 市场/宏观 MACRO & MARKETS"
        assert sender._source_default_region("Economist Finance") == "📕 经济学人 THE ECONOMIST"
        # Unknown source falls back to first region
        assert sender._source_default_region("Unknown Source") == "🧠 AI/前沿 AI FRONTIER"


# ===== Task 7: digest-tuning.json + AR program.md sync =====


class TestDigestTuningConfig:
    """Spec §4.6 + F1: digest-tuning.json must encode 10-zone region_quotas with
    max_total=150 and sum_max ≥ max_total (binding cap behavior).
    """

    @staticmethod
    def _load_tuning():
        import json
        with open(os.path.expanduser("~/global-news/digest-tuning.json")) as f:
            return json.load(f)

    def test_max_total_is_150(self):
        cfg = self._load_tuning()
        assert cfg["max_total_articles"] == 150
        assert cfg["target_article_count"] == 150
        assert cfg["max_total_articles"] == cfg["target_article_count"]

    def test_region_quotas_has_10_zones(self):
        cfg = self._load_tuning()
        assert len(cfg["region_quotas"]) == 10

    def test_region_quotas_sum_bounds(self):
        cfg = self._load_tuning()
        sum_min = sum(q["min"] for q in cfg["region_quotas"].values())
        sum_max = sum(q["max"] for q in cfg["region_quotas"].values())
        # max_total=150 acts as binding cap; sum_max should be slightly above to allow flex
        assert sum_max >= cfg["max_total_articles"], f"sum_max={sum_max} < max_total={cfg['max_total_articles']}"
        assert sum_max <= 200, f"sum_max={sum_max} too generous"
        # sum_min well below max_total — gives selection algo flexibility
        assert sum_min < cfg["max_total_articles"]

    def test_region_quotas_keys_match_sender_regions(self):
        """Each digest-tuning region key must match a sender REGION_GROUPS region (no-emoji form)."""
        cfg = self._load_tuning()
        # Strip emoji from sender's REGION_GROUPS keys (matches _apply_pipeline logic)
        def _strip_emoji(s):
            for char in s:
                if char.isalnum() or char in ' &':
                    return s[s.index(char):].strip()
            return s

        sender_no_emoji = {_strip_emoji(r) for r, _ in UnifiedNewsSender.REGION_GROUPS}
        for tuning_key in cfg["region_quotas"]:
            assert tuning_key in sender_no_emoji, (
                f"digest-tuning region key {tuning_key!r} does not match any sender "
                f"REGION_GROUPS (after emoji strip): {sender_no_emoji}"
            )

    def test_evaluator_source_to_region_matches_tuning(self):
        """evaluate_digest.py SOURCE_TO_REGION values must be a subset of tuning region_quotas keys."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "evaluate_digest",
            os.path.expanduser("~/global-news/evaluate_digest.py"),
        )
        eval_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(eval_mod)

        cfg = self._load_tuning()
        tuning_keys = set(cfg["region_quotas"].keys())
        eval_regions = set(eval_mod.SOURCE_TO_REGION.values())

        missing = eval_regions - tuning_keys
        assert not missing, f"evaluate_digest regions not in tuning: {missing}"


# ===== Task 8: reason_code logging + per-stage stats =====


class TestRoutingStats:
    """Spec §6 Task 8 + §9 Monitoring: classify_articles prints per-stage
    distribution at end, alert threshold at Stage 4 > 70%.
    """

    def test_stats_printed_after_classification(self, capsys):
        """After classify_articles, '📊 Routing distribution' header appears in stdout."""
        sender = _make_sender_with_news({
            "CBC Business": [("Some Canadian biz", "u1", None, None)],
            "界面新闻": [("宁德时代财报", "u2", None, None)],
        })
        sender.classify_articles()
        out = capsys.readouterr().out
        assert "📊 Routing distribution" in out

    def test_stats_counts_stages(self, capsys):
        """Stats show counts per stage label (Stage 1 hard lock + Stage 2 soft lock)."""
        sender = _make_sender_with_news({
            "CBC Business": [("Air Canada news", "u1", None, None)],   # Stage 1 hard
            "Globe & Mail": [("Globe biz", "u2", None, None)],          # Stage 1 hard
            "界面新闻": [("宁德时代财报", "u3", None, None)],            # Stage 2 soft
            "南方周末": [("中国教育报告", "u4", None, None)],            # Stage 2 soft
        })
        sender.classify_articles()
        out = capsys.readouterr().out
        assert "Stage 1 (hard lock)" in out
        assert "Stage 2 (soft lock)" in out
        # Verify counts present (format: "Stage 1 (hard lock)        :   2")
        # Two hard-locked + two soft-locked
        import re
        m1 = re.search(r"Stage 1 \(hard lock\)\s+:\s+(\d+)", out)
        m2 = re.search(r"Stage 2 \(soft lock\)\s+:\s+(\d+)", out)
        assert m1 and int(m1.group(1)) == 2
        assert m2 and int(m2.group(1)) == 2

    def test_stats_total_equals_classifications_size(self, capsys):
        """The (N classifications) header matches len(_classifications)."""
        sender = _make_sender_with_news({
            "CBC Business": [
                ("title 1", "u1", None, None),
                ("title 2", "u2", None, None),
                ("title 3", "u3", None, None),
            ],
        })
        sender.classify_articles()
        out = capsys.readouterr().out
        import re
        m = re.search(r"\((\d+) classifications\)", out)
        assert m is not None
        assert int(m.group(1)) == len(sender._classifications) == 3

    def test_stats_no_print_when_empty(self, capsys):
        """No '📊 Routing distribution' line when _classifications is empty."""
        sender = _make_sender_with_news({})
        sender.classify_articles()
        out = capsys.readouterr().out
        # Either no print or a degenerate empty stats — accept either
        assert "📊 Routing distribution" not in out or "0 classifications" in out

    def test_stage4_warning_when_llm_dominates(self, capsys):
        """When LLM-hit share > 70%, a warning line is emitted."""
        # Mock LLM to return 3-label for all 4 non-locked, non-soft-locked sources
        sender = _make_sender_with_news({
            "Bloomberg":      [("Fed signals hold", "u1", None, None)],
            "BBC World":      [("UN summit Geneva", "u2", None, None)],
            "Hacker News":    [("Programming language X 2.0 released", "u3", None, None)],
            "The Guardian World": [("Global politics shift", "u4", None, None)],
        })
        sender._openai_key = "fake-key"
        api_response = {"choices": [{"message": {"content": __import__("json").dumps({
            "1": {"topic": "business", "geo": "us", "subtopic": "business_macro"},
            "2": {"topic": "politics", "geo": "global"},
            "3": {"topic": "tech", "geo": "global", "subtopic": "tech_ai"},
            "4": {"topic": "politics", "geo": "global"},
        })}}]}
        def fake_call(payload, timeout=60):
            sender._last_provider = "MockLLM"
            return api_response
        sender._llm_api_call = fake_call

        sender.classify_articles()
        out = capsys.readouterr().out
        # All 4 routed via Stage 4 LLM = 100% > 70% threshold
        assert "LLM-hit share" in out
        assert "70%" in out


class TestRoutingStatsCodexFix:
    """Codex review fix: soft_escape entries DO hit LLM; the 'handled by' view
    must count them as LLM-hit, not as deterministic Stage 2.
    """

    def test_soft_escape_counted_as_llm_hit_not_deterministic(self, capsys):
        """界面 article that escapes Stage 2 + LLM returns topic/geo: should
        appear in BOTH '📊 Stage 2 (escape→LLM)' provenance row AND in
        'Hit LLM' summary, NOT in 'Deterministic'.
        """
        sender = _make_sender_with_news({
            # Title triggers Stage 2 escape (Trump, no own-geo) AND no Stage 3 keyword
            "界面新闻": [("特朗普签署 H1B 签证新规", "u1", None, None)],
        })
        sender._openai_key = "fake-key"
        api_response = {"choices": [{"message": {"content": __import__("json").dumps({
            "1": {"topic": "politics", "geo": "us"},
        })}}]}
        def fake_call(payload, timeout=60):
            sender._last_provider = "MockLLM"
            return api_response
        sender._llm_api_call = fake_call

        sender.classify_articles()
        out = capsys.readouterr().out
        # Provenance view: shows as Stage 2 (escape→LLM)
        assert "Stage 2 (escape→LLM)" in out
        # Handled-by view: counts as Hit LLM=1, Deterministic=0
        import re
        m_det = re.search(r"Deterministic \(no LLM\)\s+:\s+(\d+)", out)
        m_llm = re.search(r"Hit LLM[^:]*:\s+(\d+)", out)
        assert m_det is not None and int(m_det.group(1)) == 0
        assert m_llm is not None and int(m_llm.group(1)) == 1

    def test_handled_by_view_split_correct(self, capsys):
        """Mix of stages: 1 hard, 2 soft, 1 escape, 1 geo, 2 LLM →
        Deterministic=4 (1 hard + 2 soft + 1 geo), Hit LLM=3 (1 escape + 2 Stage 4).
        """
        sender = _make_sender_with_news({
            "CBC Business":    [("Canadian biz news", "u1", None, None)],   # Stage 1
            "界面新闻":        [("宁德时代财报", "u2", None, None)],          # Stage 2 soft
            "南方周末":        [("中国教育报告", "u3", None, None)],          # Stage 2 soft
            "界面新闻 (escape)": [("特朗普签新规", "u4", None, None)],        # Stage 2 escape (different src)
            "FT":              [("Trudeau announces budget", "u5", None, None)],  # Stage 3
            "Bloomberg":       [("Fed signals hold", "u6", None, None)],     # Stage 4
            "BBC World":       [("UN summit", "u7", None, None)],            # Stage 4
        })
        # Note: "界面新闻 (escape)" isn't a real soft-lock source, so it falls through
        # to LLM as Stage 4. Adjust: use a different soft-lock source with escape title
        sender.news_data = {
            "CBC Business":    [("Canadian biz news", "u1", None, None)],   # Stage 1 hard
            "界面新闻":        [("宁德时代财报", "u2", None, None),           # Stage 2 soft
                                ("特朗普签新规", "u3", None, None)],          # Stage 2 escape (no own-geo)
            "南方周末":        [("教育部发布报告", "u4", None, None)],         # Stage 2 soft (no escape kw)
            "FT":              [("Trudeau announces budget", "u5", None, None)],  # Stage 3
            "Bloomberg":       [("Fed signals hold", "u6", None, None)],     # Stage 4
            "BBC World":       [("UN summit", "u7", None, None)],            # Stage 4
        }
        sender._openai_key = "fake-key"
        # Mock returns 3 LLM results: 1 escape + 2 fresh Stage 4
        api_response = {"choices": [{"message": {"content": __import__("json").dumps({
            "1": {"topic": "politics", "geo": "us"},  # escape (界面 idx=1)
            "2": {"topic": "business", "geo": "us", "subtopic": "business_macro"},  # Bloomberg
            "3": {"topic": "politics", "geo": "global"},  # BBC World
        })}}]}
        def fake_call(payload, timeout=60):
            sender._last_provider = "MockLLM"
            return api_response
        sender._llm_api_call = fake_call

        sender.classify_articles()
        out = capsys.readouterr().out

        # Expected handled-by counts:
        #   Deterministic = 1 (CBC) + 2 (界面 idx=0 + 南方周末) + 1 (FT geo_keyword) = 4
        #   Hit LLM = 1 (界面 idx=1 escape) + 2 (Bloomberg + BBC) = 3
        import re
        m_det = re.search(r"Deterministic \(no LLM\)\s+:\s+(\d+)", out)
        m_llm = re.search(r"Hit LLM[^:]*:\s+(\d+)", out)
        assert m_det is not None, f"missing deterministic line in: {out}"
        assert m_llm is not None, f"missing Hit LLM line in: {out}"
        assert int(m_det.group(1)) == 4, f"deterministic count wrong, got {m_det.group(1)}"
        assert int(m_llm.group(1)) == 3, f"Hit LLM count wrong, got {m_llm.group(1)}"


class TestEvaluatorSoftLockConsistency:
    """Codex review fix: evaluator SOURCE_TO_REGION must mirror sender's
    POST-Stage-2 effective routing for soft-lock sources.
    """

    def _load_evaluator(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "evaluate_digest",
            os.path.expanduser("~/global-news/evaluate_digest.py"),
        )
        eval_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(eval_mod)
        return eval_mod

    def test_chinese_soft_lock_sources_route_to_china_in_evaluator(self):
        """All 9 Chinese soft-lock sources must map to 中国要闻 CHINA in evaluator."""
        eval_mod = self._load_evaluator()
        chinese_soft_lock = [
            "界面新闻", "南方周末", "中国财经要闻",
            "中国科技/AI", "36氪", "虎嗅", "钛媒体", "IT之家", "少数派",
        ]
        for src in chinese_soft_lock:
            assert eval_mod.SOURCE_TO_REGION.get(src) == "中国要闻 CHINA", (
                f"{src} → {eval_mod.SOURCE_TO_REGION.get(src)!r} "
                f"(expected '中国要闻 CHINA' to mirror sender's Stage 2 soft-lock)"
            )

    def test_asia_soft_lock_sources_route_to_asia_in_evaluator(self):
        """5 Asia-Pac soft-lock sources route to ASIA-PAC in evaluator (matches sender)."""
        eval_mod = self._load_evaluator()
        asia_soft_lock = ["SCMP Hong Kong", "RTHK中文", "HKFP", "Straits Times", "日经中文"]
        for src in asia_soft_lock:
            assert eval_mod.SOURCE_TO_REGION.get(src) == "亚太要闻 ASIA-PACIFIC"

    def test_evaluator_matches_sender_soft_lock_table(self):
        """Sender's _SOFT_LOCKS map must agree with evaluator's SOURCE_TO_REGION
        (after stripping emoji from sender's region constants).
        """
        from unified_global_news_sender import _SOFT_LOCKS
        eval_mod = self._load_evaluator()

        def _strip_emoji(s):
            for char in s:
                if char.isalnum() or char in ' &':
                    return s[s.index(char):].strip()
            return s

        for src, sender_region in _SOFT_LOCKS.items():
            eval_region = eval_mod.SOURCE_TO_REGION.get(src)
            assert eval_region == _strip_emoji(sender_region), (
                f"Soft-lock mismatch for {src}: sender→{sender_region!r} "
                f"(stripped {_strip_emoji(sender_region)!r}), evaluator→{eval_region!r}"
            )


# ===== Task 11: Kill switch (Option B emergency safety belt) =====


class TestKillSwitch:
    """Spec §9 Option B: NEWS_CLASSIFIER_VERSION=v1 disables Stages 2-4.

    Behavior expected:
    - v2 (default): full 4-stage funnel runs (Stage 1+2+3+4 entries in _classifications)
    - v1: only Stage 1 hard-lock entries populated; Stages 2-4 skipped entirely;
      remaining articles fall back to source-default REGION_GROUPS region via
      _reclassify_article returning None
    """

    def test_v2_default_runs_full_pipeline(self, monkeypatch):
        """Without env var (or with v2), Stage 2 soft-lock fires for 界面新闻."""
        monkeypatch.delenv("NEWS_CLASSIFIER_VERSION", raising=False)
        sender = _make_sender_with_news({
            "界面新闻": [("宁德时代财报", "u1", None, None)],
        })
        sender.classify_articles()
        entry = sender._classifications.get(("界面新闻", 0))
        assert entry is not None
        # Stage 2 soft lock fired → reason_code starts with source_lock:soft
        assert entry["reason_code"].startswith("source_lock:soft"), (
            f"Expected Stage 2 soft lock, got {entry['reason_code']!r}"
        )

    def test_v1_kill_switch_skips_stage_2(self, monkeypatch, capsys):
        """With NEWS_CLASSIFIER_VERSION=v1, Stage 2 soft-lock does NOT fire."""
        monkeypatch.setenv("NEWS_CLASSIFIER_VERSION", "v1")
        sender = _make_sender_with_news({
            "界面新闻": [("宁德时代财报", "u1", None, None)],
        })
        sender.classify_articles()
        # 界面新闻 is NOT a hard-lock source, so v1 mode → no _classifications entry
        assert ("界面新闻", 0) not in sender._classifications
        # Warning message emitted
        out = capsys.readouterr().out
        assert "kill switch ACTIVE" in out
        assert "spec §9 Option B" in out

    def test_v1_still_populates_hard_lock(self, monkeypatch):
        """v1 mode keeps Stage 1 hard-lock entries (for monitoring consistency)."""
        monkeypatch.setenv("NEWS_CLASSIFIER_VERSION", "v1")
        sender = _make_sender_with_news({
            "CBC Business": [("Canadian biz news", "u1", None, None)],
            "Bloomberg": [("Fed signals hold", "u2", None, None)],  # not locked
        })
        sender.classify_articles()
        # CBC is hard-locked → entry exists with source_lock:hard reason_code
        assert ("CBC Business", 0) in sender._classifications
        assert sender._classifications[("CBC Business", 0)]["reason_code"] == "source_lock:hard:CBC Business"
        # Bloomberg is NOT locked → no entry (v1 skips Stages 2-4)
        assert ("Bloomberg", 0) not in sender._classifications

    def test_v1_articles_fall_back_to_source_default(self, monkeypatch):
        """v1 mode: non-hard-lock articles fall back to source-default REGION_GROUPS region."""
        monkeypatch.setenv("NEWS_CLASSIFIER_VERSION", "v1")
        sender = _make_sender_with_news({
            "Bloomberg": [("Fed signals hold", "u1", None, None)],
        })
        sender.classify_articles()
        # _reclassify_article should return None (no entry → keep in source default)
        result = sender._reclassify_article("Fed signals hold", "Bloomberg", 0)
        assert result is None
        # Source default for Bloomberg is 市场/宏观 MACRO & MARKETS
        assert sender._source_default_region("Bloomberg") == "📈 市场/宏观 MACRO & MARKETS"


# ===== Task 12: routing health metrics in email footer =====


class TestRoutingHealthMetrics:
    """Spec add-on: 3 monitoring metrics rendered in HTML email footer."""

    def test_compute_health_thresholds(self):
        """_compute_routing_health buckets each metric into ok/warn/fail status."""
        sender = _make_sender_with_news({})
        # Manually populate _classifications to test threshold logic
        sender._classifications = {
            ("CBC Business", 0): {"reason_code": "source_lock:hard:CBC Business"},
            ("界面新闻", 0):     {"reason_code": "source_lock:soft:界面新闻"},
            ("FT", 0):           {"reason_code": "geo_keyword:canada"},
            ("Bloomberg", 0):    {"reason_code": "llm:topic:business_macro"},
            ("BBC World", 0):    {"reason_code": "llm:topic:politics"},
        }
        # Mock all_region_articles with consumer_tech=8 (in range), society=2 (below min)
        all_region_articles = [
            ("📱 消费科技 CONSUMER TECH", [("t1", "u1", "src", None, None)] * 8),
            ("🌐 社会观察 SOCIETY",       [("t1", "u1", "src", None, None)] * 2),
        ]
        m = sender._compute_routing_health(all_region_articles)

        # 2 of 5 hit LLM = 40% (ok, < 60% warn threshold)
        assert m["llm_hit_pct"] == 40.0
        assert m["llm_hit_status"] == "ok"
        # 0 fallbacks = 0% (ok)
        assert m["fallback_pct"] == 0.0
        assert m["fallback_status"] == "ok"
        # consumer_tech 8 ∈ [6, 10] = ok
        assert m["consumer_tech_count"] == 8
        assert m["consumer_tech_status"] == "ok"
        # society 2 < qmin 3 = warn
        assert m["society_count"] == 2
        assert m["society_status"] == "warn"

    def test_compute_health_fail_states(self):
        """High LLM-hit + empty SOCIETY → fail status."""
        sender = _make_sender_with_news({})
        # All 10 articles via LLM = 100% > 70% → fail
        sender._classifications = {
            (f"src{i}", 0): {"reason_code": "llm:topic:politics"} for i in range(10)
        }
        # consumer_tech 0 = empty (fail), society 0 = empty (fail)
        all_region_articles = [
            ("📱 消费科技 CONSUMER TECH", []),
            ("🌐 社会观察 SOCIETY", []),
        ]
        m = sender._compute_routing_health(all_region_articles)
        assert m["llm_hit_pct"] == 100.0
        assert m["llm_hit_status"] == "fail"
        assert m["consumer_tech_status"] == "fail"
        assert m["society_status"] == "fail"

    def test_render_health_html_contains_all_4_rows(self):
        """HTML output includes all 4 metric rows + threshold annotations."""
        from unified_global_news_sender import UnifiedNewsSender
        m = {
            "llm_hit_pct": 49.5, "llm_hit_status": "ok",
            "fallback_pct": 1.2, "fallback_status": "ok",
            "consumer_tech_count": 7, "consumer_tech_status": "ok",
            "society_count": 4,      "society_status": "ok",
            "total_classified": 234,
        }
        html = UnifiedNewsSender._render_routing_health_html(m, "sans", "#888", "#ccc")
        # All 4 metric labels present
        assert "LLM-hit" in html
        assert "Fallback" in html
        assert "消费科技" in html
        assert "社会观察" in html
        # Values rendered correctly
        assert "49.5%" in html and "1.2%" in html
        assert "7 篇" in html and "4 篇" in html
        # Threshold annotations
        assert "&lt;70%" in html or "<70%" in html
        assert "quota 6-10" in html and "quota 3-8" in html
        # Total classifications shown
        assert "234" in html
        # Doc link present
        assert "docs.sinostor.com.cn" in html

    def test_render_health_html_uses_status_colors(self):
        """fail status renders red, warn=amber, ok=green via icon color."""
        from unified_global_news_sender import UnifiedNewsSender
        m = {
            "llm_hit_pct": 80.0, "llm_hit_status": "fail",        # red ✗
            "fallback_pct": 4.0, "fallback_status": "warn",       # amber ⚠
            "consumer_tech_count": 7, "consumer_tech_status": "ok",  # green ✓
            "society_count": 0,      "society_status": "fail",
            "total_classified": 100,
        }
        html = UnifiedNewsSender._render_routing_health_html(m, "sans", "#888", "#ccc")
        # Colors should be in HTML
        assert "#a03a3a" in html  # red (fail)
        assert "#a07a1a" in html  # amber (warn)
        assert "#3a7a3a" in html  # green (ok)
        # Icons present
        assert "✓" in html and "⚠" in html and "✗" in html
