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


def test_collect_legacy_source_keeps_its_region():
    """向后兼容：老源 LLM 无 region(None) → 留源默认组。"""
    s = _sender()
    s.news_data = {"BBC World": [("Global summit held", "u1", None, None)]}
    s._classifications = {("BBC World", 0): {"region": None, "reason_code": "x"}}
    result = dict(s._collect_region_articles())
    assert any(a[2] == "BBC World" for a in result[_mod.REGION_POLITICS])


def test_collect_new_source_routed_by_llm_label():
    """新源有 LLM region → 进该组，不再 fall through 其他区。"""
    s = _sender()
    s.news_data = {"STAT News": [("New cancer drug approved", "u1", None, None)]}
    s._classifications = {("STAT News", 0): {"region": _mod.REGION_AI_FRONTIER, "reason_code": "llm"}}
    result = dict(s._collect_region_articles())
    assert any(a[2] == "STAT News" for a in result[_mod.REGION_AI_FRONTIER])
    assert all(a[2] != "STAT News" for a in result.get(_mod.REGION_OTHER, []))


def test_collect_new_source_no_label_goes_other():
    """新源无 LLM 标签 + 无手工默认 → 兜底其他区。"""
    s = _sender()
    s.news_data = {"STAT News": [("Some unclassified item", "u1", None, None)]}
    s._classifications = {}
    result = dict(s._collect_region_articles())
    assert any(a[2] == "STAT News" for a in result[_mod.REGION_OTHER])


def test_collect_returns_all_known_regions_plus_other():
    """返回值包含全部 REGION_GROUPS 板块 + REGION_OTHER（保持顺序）。"""
    s = _sender()
    titles = [rt for rt, _ in s._collect_region_articles()]
    expected = [rt for rt, _ in US.REGION_GROUPS] + [_mod.REGION_OTHER]
    assert titles == expected
