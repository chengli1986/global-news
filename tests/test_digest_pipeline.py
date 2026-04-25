#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone, timedelta
from digest_pipeline import bigrams, jaccard_similarity, deduplicate, rank_and_select

NOW = datetime(2026, 3, 27, 12, 0, 0, tzinfo=timezone.utc)

def test_bigrams_english():
    assert bigrams("hello") == {"he", "el", "ll", "lo"}

def test_bigrams_chinese():
    result = bigrams("中国新闻")
    assert "中国" in result and "国新" in result and len(result) == 3

def test_jaccard_identical():
    assert jaccard_similarity("breaking news today", "breaking news today") == 1.0

def test_jaccard_similar():
    assert jaccard_similarity("Tesla stock surges 10%", "Tesla stock surges 12%") > 0.6

def test_jaccard_different():
    assert jaccard_similarity("Apple launches new iPhone", "Russia Ukraine war update") < 0.2

def test_deduplicate_removes_near_dupes():
    articles = [
        {"title": "Tesla stock surges 10% on earnings", "pub_dt": NOW, "source": "CNBC", "url": "u1", "region": "Tech"},
        {"title": "Tesla stock surges 10% after earnings", "pub_dt": NOW - timedelta(hours=1), "source": "BBC", "url": "u2", "region": "Tech"},
        {"title": "Apple launches new MacBook Pro", "pub_dt": NOW, "source": "TC", "url": "u3", "region": "Tech"},
    ]
    result = deduplicate(articles, threshold=0.55)
    titles = [a["title"] for a in result]
    assert len(result) == 2
    assert "Tesla stock surges 10% on earnings" in titles
    assert "Apple launches new MacBook Pro" in titles

def test_deduplicate_keeps_all_when_different():
    articles = [
        {"title": "中国GDP增长5.2%", "pub_dt": NOW, "source": "A", "url": "u1", "region": "China"},
        {"title": "美联储维持利率不变", "pub_dt": NOW, "source": "B", "url": "u2", "region": "US"},
        {"title": "日本央行加息25bp", "pub_dt": NOW, "source": "C", "url": "u3", "region": "Asia"},
    ]
    assert len(deduplicate(articles, threshold=0.55)) == 3

def test_rank_and_select_respects_max():
    tuning = {
        "max_total_articles": 3, "freshness_weight": 0.30,
        "source_tiers": {"premium": ["FT"], "standard": ["BBC"], "commodity": ["Sina"]},
        "tier_boost": {"premium": 1.5, "standard": 1.0, "commodity": 0.8},
        "region_quotas": {"Tech": {"min": 1, "max": 5}, "Finance": {"min": 1, "max": 5}},
    }
    articles = [{"title": f"Art {i}", "pub_dt": NOW - timedelta(hours=i), "source": "FT", "url": f"u{i}", "region": "Tech"} for i in range(10)]
    assert len(rank_and_select(articles, tuning, now=NOW)) <= 3

def test_rank_and_select_enforces_region_min():
    tuning = {
        "max_total_articles": 10, "freshness_weight": 0.30,
        "source_tiers": {"premium": [], "standard": ["A", "B"], "commodity": []},
        "tier_boost": {"premium": 1.5, "standard": 1.0, "commodity": 0.8},
        "region_quotas": {"Tech": {"min": 2, "max": 8}, "Finance": {"min": 2, "max": 8}},
    }
    articles = [{"title": f"Tech {i}", "pub_dt": NOW, "source": "A", "url": f"t{i}", "region": "Tech"} for i in range(8)]
    articles += [{"title": f"Finance {i}", "pub_dt": NOW - timedelta(hours=5), "source": "B", "url": f"f{i}", "region": "Finance"} for i in range(3)]
    result = rank_and_select(articles, tuning, now=NOW)
    assert sum(1 for a in result if a["region"] == "Finance") >= 2
