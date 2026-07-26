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
enforce_pool_cap = _mod.enforce_pool_cap
MAX_POOL_SIZE = _mod.MAX_POOL_SIZE
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
            "avg_description_length": 250,  # full-text RSS → content_depth=1.0
        }
        result = compute_scores(validation, authority=0.9, uniqueness=0.8)
        self.assertGreater(result["final"], 0.8)
        self.assertEqual(result["reliability"], 1.0)
        self.assertEqual(result["freshness"], 1.0)
        self.assertEqual(result["content_quality"], 1.0)
        self.assertEqual(result["content_depth"], 1.0)

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

    def test_same_publisher_keeps_highest_score(self):
        """Two feeds from the same publisher (MIT Tech Review: topnews.rss + feed/)
        should collapse to the higher-scoring one."""
        candidates = [
            {"name": "MIT Technology Review",
             "url": "https://www.technologyreview.com/feed/",
             "scores": {"final": 0.901}},
            {"name": "MIT Technology Review",
             "url": "https://www.technologyreview.com/topnews.rss",
             "scores": {"final": 0.905}},
        ]
        result = dedup_candidates(candidates, [], [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://www.technologyreview.com/topnews.rss")

    def test_same_publisher_case_insensitive(self):
        candidates = [
            {"name": "BBC News", "url": "https://bbc.com/a", "scores": {"final": 0.80}},
            {"name": "bbc news", "url": "https://bbc.com/b", "scores": {"final": 0.90}},
        ]
        result = dedup_candidates(candidates, [], [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://bbc.com/b")

    def test_drops_candidate_whose_publisher_already_in_pool(self):
        candidates = [
            {"name": "The Guardian World",
             "url": "https://theguardian.com/some-other-feed",
             "scores": {"final": 0.95}},
        ]
        existing = [{"name": "The Guardian World",
                     "url": "https://theguardian.com/world/rss"}]
        result = dedup_candidates(candidates, existing, [])
        self.assertEqual(len(result), 0)

    def test_different_publishers_with_similar_names_both_kept(self):
        candidates = [
            {"name": "Politico US", "url": "https://politico.com/us",
             "scores": {"final": 0.85}},
            {"name": "Politico Europe", "url": "https://politico.eu/feed",
             "scores": {"final": 0.91}},
        ]
        result = dedup_candidates(candidates, [], [])
        self.assertEqual(len(result), 2)


class TestEnforcePoolCap(unittest.TestCase):
    """Pool-cap logic restored after rss-registry migration dropped it (cdd7584)."""

    @staticmethod
    def _src(name, score, status="discovered"):
        return {
            "name": name,
            "url": f"https://{name.lower()}.com/feed",
            "status": status,
            "scores": {"final": score},
        }

    def test_no_prune_when_under_cap(self):
        reg = {"sources": [self._src(f"F{i}", 0.9 - i * 0.01) for i in range(5)]}
        pruned = enforce_pool_cap(reg, max_pool=50)
        self.assertEqual(pruned, 0)
        self.assertTrue(all(s["status"] == "discovered" for s in reg["sources"]))

    def test_prunes_lowest_when_over_cap(self):
        # 55 discovered, cap 50 → prune 5 lowest
        reg = {"sources": [self._src(f"F{i:02}", 1.0 - i * 0.01) for i in range(55)]}
        pruned = enforce_pool_cap(reg, max_pool=50)
        self.assertEqual(pruned, 5)
        # Top 50 (highest scores) remain discovered
        kept = [s for s in reg["sources"] if s["status"] == "discovered"]
        self.assertEqual(len(kept), 50)
        # Bottom 5 become rejected with reason pool-cap
        rejected = [s for s in reg["sources"] if s["status"] == "rejected"]
        self.assertEqual(len(rejected), 5)
        self.assertTrue(all(s.get("reject_reason") == "pool-cap" for s in rejected))
        # Lowest 5 scores are the pruned ones
        self.assertEqual({s["name"] for s in rejected},
                         {"F50", "F51", "F52", "F53", "F54"})

    def test_non_discovered_sources_not_touched(self):
        # Production + trialing + rejected not counted against cap
        reg = {"sources": (
            [self._src(f"F{i:02}", 1.0 - i * 0.01) for i in range(52)]
            + [self._src("Prod", 0.5, status="production")]
            + [self._src("Trial", 0.5, status="trialing")]
            + [self._src("OldReject", 0.5, status="rejected")]
        )}
        pruned = enforce_pool_cap(reg, max_pool=50)
        self.assertEqual(pruned, 2)
        self.assertEqual(next(s for s in reg["sources"] if s["name"] == "Prod")["status"], "production")
        self.assertEqual(next(s for s in reg["sources"] if s["name"] == "Trial")["status"], "trialing")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Same-feed-different-URL guard (2026-07-26: 端传媒 /feed/ vs /rss/ slipped in
# because both the URL pass and the publisher-name pass compare surface strings)
# ---------------------------------------------------------------------------
INITIUM_RSS = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Initium</title>
    <item><title>A</title><link>https://theinitium.com/20260726-a/</link></item>
    <item><title>B</title><link>https://theinitium.com/20260726-b/</link></item>
    <item><title>C</title><link>https://theinitium.com/20260726-c/</link></item>
  </channel>
</rss>
"""

GUARDIAN_SCIENCE_RSS = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Guardian Science</title>
    <item><title>S1</title><link>https://theguardian.com/science/s1</link></item>
    <item><title>S2</title><link>https://theguardian.com/science/s2</link></item>
  </channel>
</rss>
"""


class TestItemLinkExtraction(unittest.TestCase):
    def test_validate_feed_returns_item_links(self):
        result = validate_feed("Initium", "https://theinitium.com/rss/",
                               raw_bytes=INITIUM_RSS)
        self.assertEqual(
            result["item_links"],
            ["https://theinitium.com/20260726-a",
             "https://theinitium.com/20260726-b",
             "https://theinitium.com/20260726-c"],
        )


class TestSameFeedGuard(unittest.TestCase):
    def test_rejects_same_feed_under_different_url(self):
        """端传媒 case: same publisher, different URL path, identical items."""
        links = ["https://theinitium.com/20260726-a",
                 "https://theinitium.com/20260726-b",
                 "https://theinitium.com/20260726-c"]
        candidates = [{"name": "端传媒", "url": "https://theinitium.com/feed/",
                       "validation": {"item_links": links}}]
        existing = [{"name": "端傳媒 Initium Media", "url": "https://theinitium.com/rss/"}]
        result = dedup_candidates(candidates, existing, [],
                                  link_fetcher=lambda url: links)
        self.assertEqual(result, [])

    def test_keeps_different_sections_of_same_domain(self):
        """Guardian science vs world: same domain, disjoint items → both stay."""
        cand_links = ["https://theguardian.com/science/s1",
                      "https://theguardian.com/science/s2"]
        candidates = [{"name": "The Guardian Science",
                       "url": "https://www.theguardian.com/science/rss",
                       "validation": {"item_links": cand_links}}]
        existing = [{"name": "The Guardian World",
                     "url": "https://www.theguardian.com/world/rss"}]
        result = dedup_candidates(
            candidates, existing, [],
            link_fetcher=lambda url: ["https://theguardian.com/world/w1",
                                      "https://theguardian.com/world/w2"])
        self.assertEqual(len(result), 1)

    def test_fail_open_when_existing_feed_unfetchable(self):
        """Can't fetch the existing feed → don't block the candidate."""
        candidates = [{"name": "端传媒", "url": "https://theinitium.com/feed/",
                       "validation": {"item_links": ["https://theinitium.com/x"]}}]
        existing = [{"name": "端傳媒 Initium Media", "url": "https://theinitium.com/rss/"}]
        result = dedup_candidates(candidates, existing, [],
                                  link_fetcher=lambda url: None)
        self.assertEqual(len(result), 1)

    def test_skips_link_fetch_for_unrelated_domains(self):
        """Guard only fires within a domain — no network for unrelated candidates."""
        calls = []

        def _fetcher(url):
            calls.append(url)
            return []

        candidates = [{"name": "Nikkei", "url": "https://asia.nikkei.com/rss",
                       "validation": {"item_links": ["https://asia.nikkei.com/a"]}}]
        existing = [{"name": "Initium", "url": "https://theinitium.com/rss/"}]
        result = dedup_candidates(candidates, existing, [], link_fetcher=_fetcher)
        self.assertEqual(len(result), 1)
        self.assertEqual(calls, [])


class TestSaveStripsTransientFields(unittest.TestCase):
    def test_item_links_not_persisted_to_registry(self):
        """item_links are a within-run dedup signal, not registry state.

        They go stale the moment the feed publishes again, and 20 URLs per
        candidate would bloat a registry that already holds ~270 entries.
        """
        import io
        import json as _json
        from unittest import mock

        entry = {"name": "X", "url": "https://x.com/feed", "status": "discovered",
                 "validation": {"parse_ok": True, "article_count": 3,
                                "item_links": ["https://x.com/1", "https://x.com/2"]}}
        saved = {}

        def _fake_save(data, path=None):
            saved.update(data)

        with mock.patch.object(_mod.sys, "stdin", io.StringIO(_json.dumps([entry]))), \
             mock.patch.object(_mod._reg, "load_registry", lambda *a, **k: {"version": 1, "sources": []}), \
             mock.patch.object(_mod._reg, "save_registry", _fake_save):
            _mod.cmd_save()

        stored = saved["sources"][0]
        self.assertNotIn("item_links", stored["validation"])
        self.assertEqual(stored["validation"]["article_count"], 3)
