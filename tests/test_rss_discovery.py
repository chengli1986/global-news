#!/usr/bin/env python3
"""Unit tests for rss-source-discovery.py."""
import os
import sys
import importlib
import importlib.util
import unittest
from datetime import datetime, timezone, timedelta

# Import the dashed-filename module via importlib
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rss_source_discovery", os.path.join(_repo, "rss-source-discovery.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

validate_feed = _mod.validate_feed
compute_scores = _mod.compute_scores
is_duplicate = _mod.is_duplicate
dedup_candidates = _mod.dedup_candidates
SCORE_THRESHOLD = _mod.SCORE_THRESHOLD

# ---------------------------------------------------------------------------
# Sample RSS XML fixtures
# ---------------------------------------------------------------------------
VALID_RSS = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Article One</title>
      <link>https://example.com/1</link>
      <description>First article description</description>
      <dc:creator>Alice</dc:creator>
      <category>Tech</category>
      <pubDate>Sun, 05 Apr 2026 12:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Article Two</title>
      <link>https://example.com/2</link>
      <description>Second article description</description>
      <dc:creator>Bob</dc:creator>
      <category>Science</category>
      <pubDate>Sun, 05 Apr 2026 10:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

EMPTY_RSS = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Empty Feed</title>
  </channel>
</rss>
"""

INVALID_XML = b"not xml at all <><>"


class TestValidateFeed(unittest.TestCase):
    def test_valid_rss(self):
        result = validate_feed("Test", "https://example.com/feed", raw_bytes=VALID_RSS)
        self.assertTrue(result["parse_ok"])
        self.assertEqual(result["article_count"], 2)
        self.assertTrue(result["has_descriptions"])
        self.assertTrue(result["has_authors"])
        self.assertTrue(result["has_categories"])
        self.assertIsNone(result["error"])

    def test_empty_rss(self):
        result = validate_feed("Empty", "https://example.com/empty", raw_bytes=EMPTY_RSS)
        self.assertEqual(result["article_count"], 0)
        self.assertIn("empty feed", result["error"].lower())

    def test_invalid_xml(self):
        result = validate_feed("Bad", "https://example.com/bad", raw_bytes=INVALID_XML)
        self.assertFalse(result["parse_ok"])

    def test_none_bytes(self):
        # No raw_bytes and no mock — HTTP will fail
        result = validate_feed("NoBytes", "https://invalid.test.example/feed", raw_bytes=None)
        self.assertFalse(result["parse_ok"])


class TestComputeScores(unittest.TestCase):
    def test_perfect_feed(self):
        validation = {
            "parse_ok": True,
            "article_count": 25,
            "newest_age_hours": 2.0,
            "has_descriptions": True,
            "has_authors": True,
            "has_categories": True,
        }
        result = compute_scores(validation, authority=0.9, uniqueness=0.8)
        self.assertGreater(result["final"], 0.8)
        self.assertEqual(result["reliability"], 1.0)
        self.assertEqual(result["freshness"], 1.0)
        self.assertEqual(result["content_quality"], 1.0)

    def test_stale_feed(self):
        validation = {
            "parse_ok": True,
            "article_count": 3,
            "newest_age_hours": 200.0,
            "has_descriptions": False,
            "has_authors": False,
            "has_categories": False,
        }
        result = compute_scores(validation, authority=0.5, uniqueness=0.3)
        self.assertLess(result["final"], SCORE_THRESHOLD)


class TestIsDuplicate(unittest.TestCase):
    def test_exact_url_match(self):
        existing = [{"url": "https://example.com/rss"}]
        self.assertTrue(is_duplicate("https://example.com/rss", existing))

    def test_trailing_slash(self):
        existing = [{"url": "https://example.com/rss"}]
        self.assertTrue(is_duplicate("https://example.com/rss/", existing))

    def test_different_url(self):
        existing = [{"url": "https://example.com/rss"}]
        self.assertFalse(is_duplicate("https://other.com/rss", existing))

    def test_same_domain_different_path(self):
        existing = [{"url": "https://example.com/rss"}]
        self.assertFalse(is_duplicate("https://example.com/atom", existing))


class TestDedupCandidates(unittest.TestCase):
    def test_removes_existing(self):
        candidates = [
            {"name": "A", "url": "https://example.com/feed"},
            {"name": "B", "url": "https://other.com/feed"},
        ]
        existing = [{"name": "Existing", "url": "https://example.com/feed"}]
        result = dedup_candidates(candidates, existing, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "B")

    def test_removes_prior_promoted(self):
        candidates = [{"name": "A", "url": "https://example.com/feed"}]
        prior = [{"url": "https://example.com/feed", "promoted": True, "rejected": False}]
        result = dedup_candidates(candidates, [], prior)
        self.assertEqual(len(result), 0)

    def test_removes_prior_rejected(self):
        candidates = [{"name": "A", "url": "https://example.com/feed"}]
        prior = [{"url": "https://example.com/feed", "promoted": False, "rejected": True}]
        result = dedup_candidates(candidates, [], prior)
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()
