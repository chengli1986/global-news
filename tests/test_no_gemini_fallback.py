"""Gemini fallback removed from the sender (2026-09-21, Google key retired).

The chain is OpenAI -> existing in-process degradation (keyword routing for
classification, English titles for translation). No second provider, no
Google key read anywhere in the cron path.
"""

import os
import sys
from unittest.mock import patch

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from unified_global_news_sender import UnifiedNewsSender  # noqa: E402

FORBIDDEN = ("generativelanguage", "GEMINI_API_KEY", "GOOGLE_API_KEY", "gemini-2.5", "_gemini_key")


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


def _bare_sender(openai_key=""):
    with patch.object(UnifiedNewsSender, "__init__", lambda self, **kw: None):
        s = UnifiedNewsSender.__new__(UnifiedNewsSender)
        s._openai_key = openai_key
        return s


def test_sender_source_has_no_gemini_path():
    src = _read("unified-global-news-sender.py")
    hits = [tok for tok in FORBIDDEN if tok in src]
    assert not hits, hits


def test_cron_wrapper_exports_no_google_key():
    src = _read("global-news-cron-wrapper.sh")
    assert "GEMINI_API_KEY" not in src and "GOOGLE_API_KEY" not in src


def test_benchmark_script_removed():
    assert not os.path.exists(os.path.join(REPO, "scripts", "benchmark_classifier_providers.py"))


def test_real_sender_has_no_gemini_attr():
    s = UnifiedNewsSender()
    assert not hasattr(s, "_gemini_key")


@patch.object(UnifiedNewsSender, "_api_call_with_retry")
def test_openai_failure_raises_without_second_provider(mock_retry):
    mock_retry.side_effect = Exception("429")
    s = _bare_sender(openai_key="sk-test")
    with pytest.raises(Exception, match="429"):
        s._llm_api_call({"model": "gpt-4.1-mini", "messages": []})
    assert mock_retry.call_count == 1
    assert "api.openai.com" in mock_retry.call_args[1]["url"]


def test_no_openai_key_raises_runtime_error():
    s = _bare_sender()
    with pytest.raises(RuntimeError, match="No LLM API key"):
        s._llm_api_call({"model": "gpt-4.1-mini", "messages": []})
