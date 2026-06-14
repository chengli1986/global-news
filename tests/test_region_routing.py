#!/usr/bin/env python3
"""Tests for article-level region routing (方案 B)."""
import os, importlib.util
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "unified_global_news_sender", os.path.join(_repo, "unified-global-news-sender.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
US = _mod.UnifiedNewsSender


def _sender():
    os.environ.setdefault("OPENAI_API_KEY", "test-not-real")
    s = US.__new__(US)              # 不跑 __init__（避免读配置/网络）
    s.news_data = {}
    s._classifications = {}
    return s


def test_legacy_source_default_unchanged():
    s = _sender()
    # BBC World 在手工 REGION_GROUPS 的 POLITICS 组
    assert s._source_default_region("BBC World") == _mod.REGION_POLITICS


def test_new_source_default_is_other():
    s = _sender()
    # STAT News 不在任何手工清单 → 兜底 REGION_OTHER（原行为是 REGION_GROUPS[0]=AI前沿）
    assert s._source_default_region("STAT News") == _mod.REGION_OTHER
