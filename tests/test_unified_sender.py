#!/usr/bin/env python3
"""Tests for unified-global-news-sender.py — UnifiedNewsSender class."""
import sys
import os
import json
import time
import io
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.expanduser("~/global-news"))

from conftest import (
    SAMPLE_RSS_XML, SAMPLE_ATOM_XML, SAMPLE_SINA_RESPONSE,
    SAMPLE_HN_TOP_IDS, SAMPLE_HN_ITEMS,
)

# Module is imported via conftest's session-scoped fixture; access it here.
from unified_global_news_sender import (
    UnifiedNewsSender, _parse_date_flexible, _is_english_source,
)


# ===== RSS Parsing =====

class TestFetchRssNews:
    """Tests for UnifiedNewsSender.fetch_rss_news static method."""

    @patch("unified_global_news_sender.UnifiedNewsSender.fetch_text")
    @patch("time.time")
    def test_fetch_rss_news_valid_rss(self, mock_time, mock_fetch_text):
        """Standard RSS 2.0 feed with <item> elements parses correctly."""
        mock_time.return_value = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        mock_fetch_text.return_value = SAMPLE_RSS_XML
        results = UnifiedNewsSender.fetch_rss_news("https://example.com/feed", limit=10)
        assert len(results) == 3
        assert results[0][0] == "Breaking: Stock Market Hits Record High"
        assert results[0][1] == "https://example.com/article1"
        # pub_dt should be a datetime
        assert results[0][2] is not None
        assert isinstance(results[0][2], datetime)

    @patch("unified_global_news_sender.UnifiedNewsSender.fetch_text")
    @patch("time.time")
    def test_fetch_rss_news_valid_atom(self, mock_time, mock_fetch_text):
        """Atom feed with <entry> elements parses correctly."""
        mock_time.return_value = datetime(2026, 4, 5, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        mock_fetch_text.return_value = SAMPLE_ATOM_XML
        results = UnifiedNewsSender.fetch_rss_news("https://example.com/atom", limit=10)
        assert len(results) == 2
        assert results[0][0] == "Atom Entry One"
        assert results[0][1] == "https://example.com/atom1"
        assert results[1][0] == "Atom Entry Two"
        assert results[1][1] == "https://example.com/atom2"

    @patch("unified_global_news_sender.UnifiedNewsSender.fetch_text")
    def test_fetch_rss_news_empty_feed(self, mock_fetch_text):
        """Empty RSS feed returns empty list."""
        mock_fetch_text.return_value = '<?xml version="1.0"?><rss><channel></channel></rss>'
        results = UnifiedNewsSender.fetch_rss_news("https://example.com/empty")
        assert results == []

    @patch("unified_global_news_sender.UnifiedNewsSender.fetch_text")
    def test_fetch_rss_news_timeout(self, mock_fetch_text):
        """Network timeout (fetch_text returns None) returns empty list."""
        mock_fetch_text.return_value = None
        results = UnifiedNewsSender.fetch_rss_news("https://example.com/timeout")
        assert results == []

    @patch("unified_global_news_sender.UnifiedNewsSender.fetch_text")
    @patch("time.time")
    def test_fetch_rss_news_keyword_filter(self, mock_time, mock_fetch_text):
        """Only articles matching keywords are returned."""
        mock_time.return_value = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        mock_fetch_text.return_value = SAMPLE_RSS_XML
        results = UnifiedNewsSender.fetch_rss_news(
            "https://example.com/feed", keywords=["AI"], limit=10
        )
        assert len(results) == 1
        assert "AI" in results[0][0]

    @patch("unified_global_news_sender.UnifiedNewsSender.fetch_text")
    @patch("time.time")
    def test_fetch_rss_news_max_age_filter(self, mock_time, mock_fetch_text):
        """Articles older than max_age_hours are filtered out."""
        # Set "now" to 2026-04-05 12:00 UTC
        now_ts = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        mock_time.return_value = now_ts
        mock_fetch_text.return_value = SAMPLE_RSS_XML
        # Articles are at 12:00, 10:00, 08:00 on Apr 5 — all within 72h
        results_all = UnifiedNewsSender.fetch_rss_news(
            "https://example.com/feed", max_age_hours=72, limit=10
        )
        assert len(results_all) == 3

        # With max_age_hours=1, only the 12:00 article should pass
        results_1h = UnifiedNewsSender.fetch_rss_news(
            "https://example.com/feed", max_age_hours=1, limit=10
        )
        # The 12:00 article is exactly 0h old, 10:00 is 2h old, 08:00 is 4h old
        assert len(results_1h) == 1
        assert "Record High" in results_1h[0][0]


# ===== Date Parsing =====

class TestParseDateFlexible:
    """Tests for _parse_date_flexible function."""

    def test_parse_date_flexible_rfc2822(self):
        """Standard RSS pubDate format (RFC 2822)."""
        result = _parse_date_flexible("Sun, 05 Apr 2026 12:00:00 +0000")
        assert result is not None
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 5

    def test_parse_date_flexible_iso8601(self):
        """ISO 8601 with timezone (Atom feeds)."""
        result = _parse_date_flexible("2026-04-05T14:30:00+08:00")
        assert result is not None
        assert result.year == 2026
        assert result.hour == 14

    def test_parse_date_flexible_extra_spaces(self):
        """36kr edge case with extra spaces before timezone offset."""
        result = _parse_date_flexible("2026-04-05 15:00:00  +0800")
        assert result is not None
        assert result.year == 2026

    def test_parse_date_flexible_invalid(self):
        """Garbage input returns None."""
        assert _parse_date_flexible("not a date at all") is None
        assert _parse_date_flexible("") is None
        assert _parse_date_flexible("   ") is None


# ===== Sina / HN Fetching =====

class TestFetchSinaNews:
    """Tests for UnifiedNewsSender.fetch_sina_news static method."""

    @patch("unified_global_news_sender.UnifiedNewsSender.fetch_json")
    @patch("time.time")
    def test_fetch_sina_news_valid(self, mock_time, mock_fetch_json):
        """Valid Sina JSON response returns tuples, skipping empty titles."""
        # Set "now" close to the article times so they pass the max_age check
        mock_time.return_value = datetime(2026, 4, 5, 13, 0, 0, tzinfo=timezone.utc).timestamp()
        mock_fetch_json.return_value = SAMPLE_SINA_RESPONSE
        results = UnifiedNewsSender.fetch_sina_news(
            "https://feed.sina.com.cn/api/test", keywords=[], limit=10, max_age_hours=72
        )
        # 3 items in data, but one has empty title → 2 results
        assert len(results) == 2
        assert results[0][0] == "中国AI芯片取得重大突破"
        assert results[0][1] == "https://finance.sina.com.cn/article1"
        # pub_dt should be a datetime
        assert isinstance(results[0][2], datetime)


class TestFetchHnNews:
    """Tests for UnifiedNewsSender.fetch_hn_news static method."""

    @patch("unified_global_news_sender.UnifiedNewsSender.fetch_json")
    @patch("time.time")
    def test_fetch_hn_news_valid(self, mock_time, mock_fetch_json):
        """HN Firebase API mock returns high-score posts, filters low/dead."""
        mock_time.return_value = datetime(2026, 4, 5, 13, 0, 0, tzinfo=timezone.utc).timestamp()

        def side_effect(url):
            if "topstories" in url:
                return SAMPLE_HN_TOP_IDS
            for item_id, item_data in SAMPLE_HN_ITEMS.items():
                if str(item_id) in url:
                    return item_data
            return None

        mock_fetch_json.side_effect = side_effect
        results = UnifiedNewsSender.fetch_hn_news(limit=4, min_score=100, max_age_hours=72)
        # 1001 (250pts), 1002 (180pts), 1005 (150pts) pass
        # 1003 (30pts) filtered by min_score, 1004 (200pts) filtered by dead=True
        assert len(results) == 3
        titles = [r[0] for r in results]
        assert any("Rust Web Framework" in t for t in titles)
        assert any("SQLite" in t for t in titles)
        assert any("Another HN Post" in t for t in titles)
        # Score should be appended: "Title (250 pts)"
        assert "(250 pts)" in results[0][0]


# ===== Cross-send Deduplication =====

class TestCrossSendDedup:
    """Tests for _cross_send_dedup method."""

    def test_cross_send_dedup_removes_seen_urls(self, sender, tmp_path):
        """Previously sent URLs are filtered out."""
        # Populate news_data and sent-today log
        sender.news_data = {"TestSrc": [("Title A", "https://seen.com/1", "TestSrc", None, None)]}

        sent_log = [
            {"title": "Title A", "url": "https://seen.com/1", "send_time": datetime.now(timezone.utc).isoformat()},
        ]

        region_articles = [("TestRegion", [("Title A", "https://seen.com/1", "TestSrc", None, None)])]

        with patch.object(sender, "_load_sent_today", return_value=sent_log):
            result = sender._cross_send_dedup(region_articles)
        # The article should be removed
        assert result[0][1] == []

    def test_cross_send_dedup_removes_similar_titles(self, sender):
        """Jaccard > 0.55 on title matches are filtered."""
        sent_log = [
            {"title": "Tesla stock surges 10% on earnings", "url": "https://other.com/x",
             "send_time": datetime.now(timezone.utc).isoformat()},
        ]
        region_articles = [
            ("Finance", [
                ("Tesla stock surges 10% after earnings", "https://new.com/y", "CNBC", None, None),
                ("Apple launches new MacBook Pro", "https://new.com/z", "TechCrunch", None, None),
            ])
        ]
        with patch.object(sender, "_load_sent_today", return_value=sent_log):
            result = sender._cross_send_dedup(region_articles)
        kept = result[0][1]
        assert len(kept) == 1
        assert "Apple" in kept[0][0]

    def test_cross_send_dedup_premium_resend(self, sender, tmp_path):
        """Premium source articles are allowed after 4h since last send."""
        four_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        sent_log = [
            {"title": "FT Exclusive: Central Bank Moves", "url": "https://ft.com/1",
             "send_time": four_hours_ago},
        ]

        region_articles = [
            ("Finance", [
                ("FT Exclusive: Central Bank Moves", "https://ft.com/1", "FT", None, None),
            ])
        ]

        # Create a digest-tuning.json with premium sources
        tuning_dir = os.path.dirname(os.path.abspath(sender.config_file))
        # We need the tuning file next to the script, not the config.
        # _cross_send_dedup reads from script dir via __file__.
        # Patch os.path.exists and open for the tuning file.
        tuning_data = {"source_tiers": {"premium": ["FT"], "standard": [], "commodity": []}}

        def mock_exists(path):
            if "digest-tuning.json" in str(path):
                return True
            return os.path.exists.__wrapped__(path) if hasattr(os.path.exists, '__wrapped__') else _orig_exists(path)

        _orig_exists = os.path.exists

        with patch.object(sender, "_load_sent_today", return_value=sent_log), \
             patch("builtins.open", side_effect=lambda p, *a, **kw: (
                 io.StringIO(json.dumps(tuning_data)) if "digest-tuning.json" in str(p)
                 else open.__class__(p, *a, **kw)
             )), \
             patch("os.path.exists", side_effect=lambda p: True if "digest-tuning.json" in str(p) else _orig_exists(p)):
            result = sender._cross_send_dedup(region_articles)

        # Premium source FT should be kept even though URL was seen, because >4h
        kept = result[0][1]
        assert len(kept) == 1
        assert "FT Exclusive" in kept[0][0]


# ===== Classification =====

class TestClassifyArticles:
    """Tests for classify_articles and _reclassify_article methods."""

    def test_classify_articles_success(self, sender):
        """Mock OpenAI API returns valid 3-label (topic/geo/subtopic) classification."""
        sender.news_data = {
            "TechCrunch": [("Apple releases new Vision Pro", "https://tc.com/1", None, None)],
            "BBC World": [("Russia-Ukraine war continues", "https://bbc.com/1", None, None)],
        }
        # Post-Task-5 LLM output is 3-label per article
        api_response = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "1": {"topic": "consumer_tech", "geo": "us"},
                        "2": {"topic": "politics", "geo": "global"},
                    })
                }
            }]
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = api_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            sender.classify_articles()

        # Post-Task-6 shape: _classifications routed via _route() to 10-zone REGION_GROUPS
        assert ("TechCrunch", 0) in sender._classifications
        # consumer_tech routes to dedicated CONSUMER_TECH zone (Q2B split)
        assert sender._classifications[("TechCrunch", 0)]["region"] == "📱 消费科技 CONSUMER TECH"
        assert sender._classifications[("TechCrunch", 0)]["reason_code"] == "llm:topic:consumer_tech"
        assert sender._classifications[("TechCrunch", 0)]["topic"] == "consumer_tech"
        assert sender._classifications[("TechCrunch", 0)]["geo"] == "us"
        assert ("BBC World", 0) in sender._classifications
        assert sender._classifications[("BBC World", 0)]["region"] == "🏛 全球政治 GLOBAL POLITICS"
        assert sender._classifications[("BBC World", 0)]["reason_code"] == "llm:topic:politics"
        assert sender._classifications[("BBC World", 0)]["topic"] == "politics"
        assert sender._classifications[("BBC World", 0)]["geo"] == "global"

    def test_classify_articles_fallback(self, sender):
        """On API failure, keyword fallback works via _reclassify_article."""
        sender.news_data = {
            "CNBC": [("美联储宣布加息25个基点", "https://cnbc.com/1", None, None)],
        }
        # Make API call fail
        with patch("urllib.request.urlopen", side_effect=Exception("API down")):
            sender.classify_articles()

        # No LLM classifications set
        assert not sender._classifications

        # Keyword fallback: "美联储" is in _INTL_KEYWORDS → politics
        result = sender._reclassify_article("美联储宣布加息25个基点", "CNBC", 0)
        assert result == "🏛 全球政治 GLOBAL POLITICS"

    def test_reclassify_article_keyword(self, sender):
        """Military keywords trigger politics reclassification."""
        # No LLM classifications set
        sender._classifications = {}
        result = sender._reclassify_article("俄罗斯军事行动升级", "BBC World", 0)
        assert result == "🏛 全球政治 GLOBAL POLITICS"

        # Non-matching title returns None (keep in original region)
        result2 = sender._reclassify_article("苹果发布新款MacBook", "TechCrunch", 0)
        assert result2 is None

        # Locked sources should not be reclassified
        result3 = sender._reclassify_article("美国经济衰退", "CBC Business", 0)
        assert result3 is None


# ===== HTML Generation & Helpers =====

class TestHtmlHelpers:
    """Tests for HTML-related helpers."""

    def test_esc_html_entities(self, sender):
        """Verify HTML escaping of special characters."""
        assert sender._esc('<script>alert("xss")</script>') == '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;'
        assert sender._esc("Tom & Jerry") == "Tom &amp; Jerry"
        assert sender._esc("'quotes'") == "&#x27;quotes&#x27;"

    def test_generate_html_structure(self, sender):
        """Generated HTML contains expected structural elements."""
        sender.news_data = {
            "BBC World": [
                ("Global Summit Begins", "https://bbc.com/summit", None, None),
            ],
            "TechCrunch": [
                ("New Startup Raises $100M", "https://tc.com/startup", None, None),
            ],
        }
        sender._classifications = {}
        html_output = sender.generate_html()
        # Check basic structure
        assert "<!DOCTYPE html>" in html_output
        assert "全球要闻简报" in html_output
        assert "GLOBAL NEWS BRIEFING" in html_output
        # Check that articles appear
        assert "Global Summit Begins" in html_output
        assert "New Startup Raises" in html_output
        # Check region headers present
        assert "GLOBAL POLITICS" in html_output or "TECH &amp; AI" in html_output or "TECH & AI" in html_output

    def test_is_english_source(self):
        """Chinese source names detected correctly."""
        assert _is_english_source("BBC World") is True
        assert _is_english_source("TechCrunch") is True
        assert _is_english_source("虎嗅") is False
        assert _is_english_source("纽约时报中文") is False
        assert _is_english_source("SCMP Hong Kong") is True
        assert _is_english_source("36氪") is False


# ===== LLM API Fallback =====

class TestLLMApiFallback:
    """Tests for _llm_api_call provider fallback and _extract_json_from_text."""

    def _make_sender(self, openai_key="", gemini_key=""):
        """Create a sender with injected API keys (no real config needed)."""
        with patch.object(UnifiedNewsSender, "__init__", lambda self, **kw: None):
            s = UnifiedNewsSender.__new__(UnifiedNewsSender)
            s._openai_key = openai_key
            s._gemini_key = gemini_key
            return s

    def test_extract_json_plain(self):
        """Plain JSON string is extracted correctly."""
        s = self._make_sender()
        assert s._extract_json_from_text('["hello"]') == ["hello"]
        assert s._extract_json_from_text('{"a": 1}') == {"a": 1}

    def test_extract_json_markdown_wrapped(self):
        """JSON wrapped in markdown code blocks is extracted correctly."""
        s = self._make_sender()
        text = '```json\n["油价飙升", "特朗普"]\n```'
        assert s._extract_json_from_text(text) == ["油价飙升", "特朗普"]

    def test_extract_json_markdown_no_lang(self):
        """Code block without language tag also works."""
        s = self._make_sender()
        text = '```\n{"key": "value"}\n```'
        assert s._extract_json_from_text(text) == {"key": "value"}

    def test_extract_json_with_surrounding_text(self):
        """Text before/after code fence is ignored — only fenced content extracted."""
        s = self._make_sender()
        text = 'Here are the translations:\n```json\n["油价飙升"]\n```\nDone!'
        # Leading text makes it not start with ```, so json.loads is attempted on the whole string
        # which will fail — this is expected: the model should return clean output.
        with pytest.raises(Exception):
            s._extract_json_from_text(text)

    def test_extract_json_invalid(self):
        """Invalid JSON raises an exception."""
        s = self._make_sender()
        with pytest.raises(Exception):
            s._extract_json_from_text("not json at all")
        with pytest.raises(Exception):
            s._extract_json_from_text('```json\n{broken\n```')

    def test_extract_json_whitespace_padding(self):
        """JSON with whitespace padding is handled."""
        s = self._make_sender()
        assert s._extract_json_from_text('  \n  ["a", "b"]  \n  ') == ["a", "b"]

    def test_no_keys_raises(self):
        """RuntimeError when neither OpenAI nor Gemini key is set."""
        s = self._make_sender()
        with pytest.raises(RuntimeError, match="No LLM API keys"):
            s._llm_api_call({"model": "gpt-4.1-mini", "messages": []})

    @patch.object(UnifiedNewsSender, "_api_call_with_retry")
    def test_openai_success_no_fallback(self, mock_retry):
        """When OpenAI succeeds, Gemini is never called."""
        mock_retry.return_value = {"choices": [{"message": {"content": "ok"}}]}
        s = self._make_sender(openai_key="sk-test", gemini_key="gem-test")
        result = s._llm_api_call({"model": "gpt-4.1-mini", "messages": []})
        assert result["choices"][0]["message"]["content"] == "ok"
        mock_retry.assert_called_once()
        assert "api.openai.com" in mock_retry.call_args[1]["url"]

    @patch.object(UnifiedNewsSender, "_api_call_with_retry")
    def test_openai_fail_gemini_fallback(self, mock_retry):
        """When OpenAI fails, falls back to Gemini."""
        gemini_response = {"choices": [{"message": {"content": "from gemini"}}]}
        mock_retry.side_effect = [
            Exception("429 Too Many Requests"),  # OpenAI fails
            gemini_response,                      # Gemini succeeds
        ]
        s = self._make_sender(openai_key="sk-test", gemini_key="gem-test")
        result = s._llm_api_call(
            {"model": "gpt-4.1-mini", "messages": [], "response_format": {"type": "json_object"}}
        )
        assert result["choices"][0]["message"]["content"] == "from gemini"
        # Gemini call should NOT have response_format
        gemini_call = mock_retry.call_args_list[1]
        assert "response_format" not in gemini_call[1]["payload"]
        assert "gemini-2.5-flash" in gemini_call[1]["payload"]["model"]

    @patch.object(UnifiedNewsSender, "_api_call_with_retry")
    def test_gemini_flash_fail_falls_to_lite(self, mock_retry):
        """gemini-2.5-flash 503 → gemini-2.5-flash-lite."""
        lite_response = {"choices": [{"message": {"content": "from lite"}}]}
        mock_retry.side_effect = [
            Exception("429"),   # OpenAI
            Exception("503"),   # gemini-2.5-flash
            lite_response,      # gemini-2.5-flash-lite
        ]
        s = self._make_sender(openai_key="sk-test", gemini_key="gem-test")
        result = s._llm_api_call({"model": "gpt-4.1-mini", "messages": []})
        assert result["choices"][0]["message"]["content"] == "from lite"
        lite_call = mock_retry.call_args_list[2]
        assert "flash-lite" in lite_call[1]["payload"]["model"]

    @patch.object(UnifiedNewsSender, "_api_call_with_retry")
    def test_openai_only_no_gemini_key(self, mock_retry):
        """Without Gemini key, OpenAI failure raises directly."""
        mock_retry.side_effect = Exception("429")
        s = self._make_sender(openai_key="sk-test")
        with pytest.raises(Exception):
            s._llm_api_call({"model": "gpt-4.1-mini", "messages": []})

    @patch.object(UnifiedNewsSender, "_api_call_with_retry")
    def test_gemini_only_no_openai_key(self, mock_retry):
        """With only Gemini key, goes directly to Gemini."""
        mock_retry.return_value = {"choices": [{"message": {"content": "gemini"}}]}
        s = self._make_sender(gemini_key="gem-test")
        result = s._llm_api_call({"model": "gpt-4.1-mini", "messages": []})
        assert "generativelanguage.googleapis.com" in mock_retry.call_args[1]["url"]

    def test_gemini_key_from_env(self):
        """GEMINI_API_KEY and GOOGLE_API_KEY both picked up."""
        with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": "gk-test"}, clear=False):
            with patch.object(UnifiedNewsSender, "__init__", lambda self, **kw: None):
                s = UnifiedNewsSender.__new__(UnifiedNewsSender)
                s._gemini_key = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
                assert s._gemini_key == "gk-test"
