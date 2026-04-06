#!/usr/bin/env python3
"""Shared fixtures for global-news tests."""
import sys
import os
import json
import pytest
import importlib
import importlib.util
from datetime import datetime, timezone, timedelta

# Ensure the global-news root is on sys.path (resolve relative to this file, not ~/)
_global_news_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _global_news_dir not in sys.path:
    sys.path.insert(0, _global_news_dir)

# Register the dashed-filename module under an underscore name BEFORE any test
# module tries to import it.
_mod_file = os.path.join(_global_news_dir, "unified-global-news-sender.py")
if os.path.exists(_mod_file) and "unified_global_news_sender" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("unified_global_news_sender", _mod_file)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["unified_global_news_sender"] = _mod
    _spec.loader.exec_module(_mod)

BJT = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# Sample RSS XML (standard RSS 2.0)
# ---------------------------------------------------------------------------
SAMPLE_RSS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Breaking: Stock Market Hits Record High</title>
      <link>https://example.com/article1</link>
      <pubDate>Sun, 05 Apr 2026 12:00:00 +0000</pubDate>
    </item>
    <item>
      <title>New AI Model Released by OpenAI</title>
      <link>https://example.com/article2</link>
      <pubDate>Sun, 05 Apr 2026 10:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Weather Update: Sunny Skies Ahead</title>
      <link>https://example.com/article3</link>
      <pubDate>Sun, 05 Apr 2026 08:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

# ---------------------------------------------------------------------------
# Sample Atom feed
# ---------------------------------------------------------------------------
SAMPLE_ATOM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Test Feed</title>
  <entry>
    <title>Atom Entry One</title>
    <link href="https://example.com/atom1"/>
    <published>2026-04-05T14:00:00+00:00</published>
  </entry>
  <entry>
    <title>Atom Entry Two</title>
    <link href="https://example.com/atom2"/>
    <updated>2026-04-05T13:00:00+00:00</updated>
  </entry>
</feed>
"""

# ---------------------------------------------------------------------------
# Sample Sina API response
# ---------------------------------------------------------------------------
SAMPLE_SINA_RESPONSE = {
    "result": {
        "status": {"code": 0},
        "data": [
            {
                "title": "中国AI芯片取得重大突破",
                "url": "https://finance.sina.com.cn/article1",
                "ctime": str(int(datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp())),
            },
            {
                "title": "新能源汽车销量创新高",
                "url": "https://finance.sina.com.cn/article2",
                "ctime": str(int(datetime(2026, 4, 5, 10, 0, 0, tzinfo=timezone.utc).timestamp())),
            },
            {
                "title": "",
                "url": "https://finance.sina.com.cn/empty",
                "ctime": str(int(datetime(2026, 4, 5, 9, 0, 0, tzinfo=timezone.utc).timestamp())),
            },
        ],
    }
}

# ---------------------------------------------------------------------------
# Sample HN items
# ---------------------------------------------------------------------------
SAMPLE_HN_TOP_IDS = [1001, 1002, 1003, 1004, 1005]

SAMPLE_HN_ITEMS = {
    1001: {"title": "Show HN: A New Rust Web Framework", "url": "https://example.com/hn1", "score": 250, "time": int(datetime(2026, 4, 5, 11, 0, 0, tzinfo=timezone.utc).timestamp())},
    1002: {"title": "Why SQLite Is Great", "url": "https://example.com/hn2", "score": 180, "time": int(datetime(2026, 4, 5, 10, 0, 0, tzinfo=timezone.utc).timestamp())},
    1003: {"title": "Low Score Post", "url": "https://example.com/hn3", "score": 30, "time": int(datetime(2026, 4, 5, 9, 0, 0, tzinfo=timezone.utc).timestamp())},
    1004: {"title": "Dead Post", "url": "https://example.com/hn4", "score": 200, "time": int(datetime(2026, 4, 5, 8, 0, 0, tzinfo=timezone.utc).timestamp()), "dead": True},
    1005: {"title": "Another HN Post", "url": "https://example.com/hn5", "score": 150, "time": int(datetime(2026, 4, 5, 7, 0, 0, tzinfo=timezone.utc).timestamp())},
}

# ---------------------------------------------------------------------------
# Minimal config for UnifiedNewsSender
# ---------------------------------------------------------------------------
MINIMAL_CONFIG = {
    "news_sources": {
        "sina_api": [],
        "rss_feeds": [],
        "hn_api": [],
    }
}


@pytest.fixture
def minimal_config_file(tmp_path):
    """Write a minimal config file and return its path."""
    cfg_path = tmp_path / "news-sources-config.json"
    cfg_path.write_text(json.dumps(MINIMAL_CONFIG), encoding="utf-8")
    return str(cfg_path)


@pytest.fixture
def sender(minimal_config_file):
    """Create a UnifiedNewsSender instance with a minimal config (no external calls)."""
    os.environ.setdefault("OPENAI_API_KEY", "test-key-not-real")
    from unified_global_news_sender import UnifiedNewsSender
    return UnifiedNewsSender(config_file=minimal_config_file)
