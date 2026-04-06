# RSS Source Discovery Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily AI-driven pipeline that discovers, validates, scores, and reports new RSS feed candidates across 8 categories.

**Architecture:** A Python helper (`rss-source-discovery.py`) handles HTTP validation, scoring, dedup, and email. A shell wrapper (`scripts/rss-source-discovery.sh`) triggers a Claude Code session that performs web search discovery, calls the Python helper, and writes results. Semi-automatic promotion via `rss-promote-candidate.py`.

**Tech Stack:** Python 3.12 stdlib only (urllib, xml.etree, json, concurrent.futures, email), Claude Code CLI (`claude -p`), curl SMTP.

**Spec:** `docs/superpowers/specs/2026-04-06-rss-source-discovery-design.md`

---

## File Structure

```
global-news/
├── rss-source-discovery.py              # Python helper: validate, score, dedup, email
├── rss-promote-candidate.py             # CLI tool: promote candidate → news-sources-config.json
├── scripts/
│   └── rss-source-discovery.sh          # Cron wrapper → Claude Code session
├── config/
│   ├── discovered-rss.json              # Candidate registry (persistent state)
│   ├── rss-scorer-weights.json          # 5-dimension scoring weights
│   └── rss-discovery-categories.json    # 8 categories + search query templates
├── tests/
│   ├── test_rss_discovery.py            # Unit tests for discovery helper
│   └── test_rss_promote.py             # Unit tests for promotion tool
└── docs/superpowers/
    ├── specs/2026-04-06-rss-source-discovery-design.md
    └── plans/2026-04-06-rss-source-discovery.md  (this file)
```

---

### Task 1: Config Files — Categories + Weights + Empty Candidates Registry

**Files:**
- Create: `config/rss-discovery-categories.json`
- Create: `config/rss-scorer-weights.json`
- Create: `config/discovered-rss.json`

- [ ] **Step 1: Create config directory and categories file**

```bash
mkdir -p ~/global-news/config
```

Write `config/rss-discovery-categories.json`:
```json
{
  "categories": [
    {
      "id": "global_finance",
      "name": "全球财经",
      "languages": ["en", "cn"],
      "search_queries": [
        "best RSS feeds global finance news reliable 2025 2026",
        "高质量全球财经新闻 RSS feed 推荐"
      ]
    },
    {
      "id": "tech_ai",
      "name": "科技/AI",
      "languages": ["en", "cn"],
      "search_queries": [
        "best technology AI RSS feeds high quality 2025 2026",
        "优质科技人工智能新闻 RSS 推荐"
      ]
    },
    {
      "id": "china_depth",
      "name": "中国深度新闻",
      "languages": ["cn"],
      "search_queries": [
        "中国深度调查报道 RSS feed 推荐",
        "best Chinese in-depth news RSS feeds"
      ]
    },
    {
      "id": "hk_sea",
      "name": "香港/东南亚",
      "languages": ["en", "cn"],
      "search_queries": [
        "Hong Kong Southeast Asia news RSS feeds reliable",
        "香港东南亚新闻 RSS feed 推荐"
      ]
    },
    {
      "id": "europe",
      "name": "欧洲时政",
      "languages": ["en"],
      "search_queries": [
        "best European politics news RSS feeds reliable 2025 2026",
        "European news RSS BBC Guardian DW Reuters alternatives"
      ]
    },
    {
      "id": "north_america",
      "name": "北美时政",
      "languages": ["en"],
      "search_queries": [
        "best North America politics news RSS feeds 2025 2026",
        "reliable US Canada news RSS feeds high quality"
      ]
    },
    {
      "id": "healthcare",
      "name": "医药医疗前沿",
      "languages": ["en", "cn"],
      "search_queries": [
        "best healthcare biotech pharma RSS feeds 2025 2026",
        "医药医疗前沿新闻 RSS feed 推荐"
      ]
    },
    {
      "id": "vertical",
      "name": "专题/垂直",
      "languages": ["en", "cn"],
      "search_queries": [
        "best niche vertical RSS feeds Economist long-form analysis",
        "优质垂直领域深度分析 RSS 推荐 经济学人类"
      ]
    }
  ],
  "directory_urls": [
    "https://github.com/topics/rss-feeds",
    "https://github.com/plenaryapp/awesome-rss-feeds"
  ]
}
```

- [ ] **Step 2: Create scorer weights file**

Write `config/rss-scorer-weights.json`:
```json
{
  "reliability": 0.25,
  "freshness": 0.20,
  "content_quality": 0.20,
  "authority": 0.20,
  "uniqueness": 0.15
}
```

- [ ] **Step 3: Create empty candidates registry**

Write `config/discovered-rss.json`:
```json
{
  "version": 1,
  "last_discovery": null,
  "candidates": []
}
```

- [ ] **Step 4: Commit**

```bash
cd ~/global-news
git add config/rss-discovery-categories.json config/rss-scorer-weights.json config/discovered-rss.json
git commit -m "feat(discovery): add config files for RSS source discovery pipeline"
```

---

### Task 2: Python Helper — Validation and Scoring Engine

**Files:**
- Create: `rss-source-discovery.py`
- Test: `tests/test_rss_discovery.py`

This is the core engine. Claude calls it during the session. It provides 4 subcommands:
- `validate` — HTTP fetch + parse a list of candidate URLs (JSON stdin → JSON stdout)
- `score` — Apply 5-dimension scoring to validated candidates (JSON stdin → JSON stdout)
- `report` — Generate + send HTML email report
- `dedup` — Check candidates against existing sources + prior candidates

- [ ] **Step 1: Write failing tests for validation logic**

Write `tests/test_rss_discovery.py`:
```python
#!/usr/bin/env python3
"""Tests for rss-source-discovery.py — validation, scoring, dedup."""
import sys
import os
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util

# Import dashed-filename module
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rss_source_discovery", os.path.join(_repo, "rss-source-discovery.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

validate_feed = _mod.validate_feed
compute_scores = _mod.compute_scores
is_duplicate = _mod.is_duplicate
SCORE_THRESHOLD = _mod.SCORE_THRESHOLD


# ---- Validation ----

SAMPLE_RSS = '''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>Article 1</title><link>https://example.com/1</link>
<pubDate>Mon, 06 Apr 2026 10:00:00 +0000</pubDate>
<description>Full description here</description>
<dc:creator>Author Name</dc:creator></item>
<item><title>Article 2</title><link>https://example.com/2</link>
<pubDate>Mon, 06 Apr 2026 08:00:00 +0000</pubDate></item>
</channel></rss>'''

SAMPLE_RSS_EMPTY = '''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Empty</title></channel></rss>'''


class TestValidateFeed:
    def test_valid_rss(self):
        result = validate_feed("Test Feed", "https://example.com/rss", raw_bytes=SAMPLE_RSS.encode())
        assert result["parse_ok"] is True
        assert result["article_count"] == 2
        assert result["has_descriptions"] is True
        assert result["http_status"] == 200

    def test_empty_rss(self):
        result = validate_feed("Empty", "https://example.com/rss", raw_bytes=SAMPLE_RSS_EMPTY.encode())
        assert result["parse_ok"] is True
        assert result["article_count"] == 0
        assert result["error"] == "empty feed (0 articles)"

    def test_invalid_xml(self):
        result = validate_feed("Bad", "https://example.com/rss", raw_bytes=b"not xml at all")
        assert result["parse_ok"] is False
        assert "parse error" in result["error"].lower()

    def test_none_bytes(self):
        result = validate_feed("Null", "https://example.com/rss", raw_bytes=None)
        assert result["parse_ok"] is False


# ---- Scoring ----

class TestComputeScores:
    def test_perfect_feed(self):
        validation = {
            "http_status": 200, "parse_ok": True, "article_count": 25,
            "newest_age_hours": 1.0, "has_descriptions": True,
            "has_authors": True, "has_categories": True, "error": None,
        }
        weights = {"reliability": 0.25, "freshness": 0.20, "content_quality": 0.20, "authority": 0.20, "uniqueness": 0.15}
        scores = compute_scores(validation, authority=0.9, uniqueness=0.8, weights=weights)
        assert scores["reliability"] > 0.9
        assert scores["freshness"] > 0.9
        assert scores["content_quality"] > 0.8
        assert scores["final"] > 0.8

    def test_stale_feed(self):
        validation = {
            "http_status": 200, "parse_ok": True, "article_count": 5,
            "newest_age_hours": 200.0, "has_descriptions": False,
            "has_authors": False, "has_categories": False, "error": None,
        }
        weights = {"reliability": 0.25, "freshness": 0.20, "content_quality": 0.20, "authority": 0.20, "uniqueness": 0.15}
        scores = compute_scores(validation, authority=0.5, uniqueness=0.5, weights=weights)
        assert scores["freshness"] < 0.3
        assert scores["content_quality"] < 0.5
        assert scores["final"] < SCORE_THRESHOLD


# ---- Dedup ----

class TestIsDuplicate:
    def test_exact_url_match(self):
        existing = [{"url": "https://example.com/rss"}]
        assert is_duplicate("https://example.com/rss", existing) is True

    def test_trailing_slash(self):
        existing = [{"url": "https://example.com/rss/"}]
        assert is_duplicate("https://example.com/rss", existing) is True

    def test_different_url(self):
        existing = [{"url": "https://other.com/rss"}]
        assert is_duplicate("https://example.com/rss", existing) is False

    def test_same_domain_different_path(self):
        existing = [{"url": "https://example.com/feed1"}]
        assert is_duplicate("https://example.com/feed2", existing) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/global-news && python3 -m pytest tests/test_rss_discovery.py -v
```
Expected: ImportError or AttributeError — module functions don't exist yet.

- [ ] **Step 3: Implement rss-source-discovery.py**

Write `rss-source-discovery.py`:
```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS Source Discovery Helper
Called by Claude Code session during discovery pipeline.
Subcommands: validate, score, report, dedup, promote-check
Stdlib only — no pip dependencies.
"""

import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import json
import sys
import os
import time
import re
import base64
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

BJT = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(SCRIPT_DIR, "config")
CANDIDATES_FILE = os.path.join(CONFIG_DIR, "discovered-rss.json")
WEIGHTS_FILE = os.path.join(CONFIG_DIR, "rss-scorer-weights.json")
SOURCES_FILE = os.path.join(SCRIPT_DIR, "news-sources-config.json")
ENV_FILE = os.path.expanduser("~/.smtp.env")

FETCH_TIMEOUT = 15
SCORE_THRESHOLD = 0.60
SCORE_EXCELLENT = 0.80
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ============================================================
# Validation
# ============================================================

def validate_feed(name: str, url: str, raw_bytes: bytes = None) -> dict:
    """Validate a single RSS/Atom feed. Returns validation dict.
    If raw_bytes is provided, skip HTTP fetch (for testing).
    """
    result = {
        "http_status": 0, "parse_ok": False, "article_count": 0,
        "newest_age_hours": None, "has_descriptions": False,
        "has_authors": False, "has_categories": False, "error": None,
    }

    # Step 1: Fetch
    if raw_bytes is None:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                raw_bytes = r.read()
                result["http_status"] = r.status
        except Exception as e:
            result["error"] = f"unreachable: {type(e).__name__}"
            return result
    else:
        result["http_status"] = 200

    if not raw_bytes:
        result["error"] = "empty response"
        return result

    # Step 2: Parse XML
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("latin-1")
        except Exception:
            result["error"] = "encoding error"
            return result

    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        result["error"] = "XML parse error"
        return result

    result["parse_ok"] = True

    # Step 3: Extract items
    atom_ns = "{http://www.w3.org/2005/Atom}"
    dc_ns = "{http://purl.org/dc/elements/1.1/}"
    items = root.findall(".//item")
    if not items:
        items = root.findall(f".//{atom_ns}entry")

    if not items:
        result["article_count"] = 0
        result["error"] = "empty feed (0 articles)"
        return result

    result["article_count"] = len(items)

    # Step 4: Check content richness
    desc_count = 0
    author_count = 0
    cat_count = 0
    now_ts = time.time()
    newest_ts = None

    for item in items:
        # Description
        desc = (item.findtext("description")
                or item.findtext(f"{atom_ns}summary")
                or item.findtext(f"{atom_ns}content")
                or "")
        if desc.strip():
            desc_count += 1

        # Author
        author = (item.findtext("author")
                  or item.findtext(f"{dc_ns}creator")
                  or item.findtext(f"{atom_ns}author/{atom_ns}name")
                  or "")
        if author.strip():
            author_count += 1

        # Category
        if item.find("category") is not None or item.find(f"{atom_ns}category") is not None:
            cat_count += 1

        # Freshness
        pub_str = (item.findtext("pubDate")
                   or item.findtext(f"{atom_ns}published")
                   or item.findtext(f"{atom_ns}updated")
                   or "")
        if pub_str.strip():
            dt = _parse_date(pub_str)
            if dt is not None:
                ts = dt.timestamp()
                if newest_ts is None or ts > newest_ts:
                    newest_ts = ts

    total = len(items)
    result["has_descriptions"] = desc_count > total * 0.5
    result["has_authors"] = author_count > total * 0.3
    result["has_categories"] = cat_count > total * 0.3

    if newest_ts is not None:
        result["newest_age_hours"] = round((now_ts - newest_ts) / 3600, 1)

    return result


def validate_feeds_parallel(candidates: list) -> list:
    """Validate multiple feeds in parallel. Returns list of (name, url, validation)."""
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for c in candidates:
            f = pool.submit(validate_feed, c["name"], c["url"])
            futures[f] = c
        for f in as_completed(futures):
            c = futures[f]
            try:
                val = f.result()
            except Exception as e:
                val = {"http_status": 0, "parse_ok": False, "article_count": 0,
                       "error": str(e), "newest_age_hours": None,
                       "has_descriptions": False, "has_authors": False, "has_categories": False}
            results.append({"name": c["name"], "url": c["url"], "validation": val,
                            "language": c.get("language", "en"), "category": c.get("category", ""),
                            "discovered_via": c.get("discovered_via", "unknown")})
    return results


def _parse_date(s: str):
    """Parse RSS/Atom date string. Returns datetime or None."""
    s = s.strip()
    if not s:
        return None
    # Try RFC 2822 (RSS pubDate)
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    # Try ISO 8601 (Atom)
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except Exception:
        pass
    return None


# ============================================================
# Scoring
# ============================================================

def compute_scores(validation: dict, authority: float, uniqueness: float,
                   weights: dict = None) -> dict:
    """Compute 5-dimension scores from validation results + AI judgments.
    authority and uniqueness are 0.0-1.0 floats provided by Claude.
    Returns dict with per-dimension scores + weighted final.
    """
    if weights is None:
        weights = load_weights()

    # Reliability: HTTP success + parse + has articles
    rel = 0.0
    if validation.get("parse_ok") and validation.get("http_status") == 200:
        count = validation.get("article_count", 0)
        if count >= 20:
            rel = 1.0
        elif count >= 5:
            rel = 0.8
        elif count >= 1:
            rel = 0.6
        else:
            rel = 0.2

    # Freshness: based on newest_age_hours
    fresh = 0.0
    age = validation.get("newest_age_hours")
    if age is not None:
        if age <= 6:
            fresh = 1.0
        elif age <= 24:
            fresh = 0.8
        elif age <= 48:
            fresh = 0.5
        elif age <= 168:
            fresh = 0.2
        else:
            fresh = 0.0

    # Content quality: descriptions, authors, categories
    cq = 0.0
    has_desc = 1.0 if validation.get("has_descriptions") else 0.0
    has_auth = 1.0 if validation.get("has_authors") else 0.0
    has_cat = 1.0 if validation.get("has_categories") else 0.0
    cq = has_desc * 0.5 + has_auth * 0.3 + has_cat * 0.2

    # Authority and uniqueness: passed in by Claude
    auth = max(0.0, min(1.0, authority))
    uniq = max(0.0, min(1.0, uniqueness))

    final = (
        rel * weights["reliability"]
        + fresh * weights["freshness"]
        + cq * weights["content_quality"]
        + auth * weights["authority"]
        + uniq * weights["uniqueness"]
    )

    return {
        "reliability": round(rel, 2),
        "freshness": round(fresh, 2),
        "content_quality": round(cq, 2),
        "authority": round(auth, 2),
        "uniqueness": round(uniq, 2),
        "final": round(final, 4),
    }


def load_weights() -> dict:
    """Load scoring weights from config file."""
    try:
        with open(WEIGHTS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"reliability": 0.25, "freshness": 0.20, "content_quality": 0.20,
                "authority": 0.20, "uniqueness": 0.15}


# ============================================================
# Dedup
# ============================================================

def _normalize_url(url: str) -> str:
    """Normalize URL for dedup: lowercase domain, strip trailing slash."""
    p = urlparse(url.strip().lower())
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}"


def is_duplicate(url: str, existing: list) -> bool:
    """Check if url is duplicate of any entry in existing list.
    existing: list of dicts with 'url' key.
    """
    norm = _normalize_url(url)
    for e in existing:
        if _normalize_url(e.get("url", "")) == norm:
            return True
    return False


def dedup_candidates(candidates: list, existing_sources: list, prior_candidates: list) -> list:
    """Remove candidates that duplicate existing sources or prior (rejected/promoted) candidates."""
    all_existing = existing_sources + [c for c in prior_candidates if c.get("promoted") or c.get("rejected")]
    results = []
    seen = set()
    for c in candidates:
        norm = _normalize_url(c["url"])
        if norm in seen:
            continue
        if is_duplicate(c["url"], all_existing):
            continue
        seen.add(norm)
        results.append(c)
    return results


# ============================================================
# Candidates file I/O
# ============================================================

def load_candidates() -> dict:
    """Load candidates registry."""
    try:
        with open(CANDIDATES_FILE) as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "last_discovery": None, "candidates": []}


def save_candidates(data: dict):
    """Atomic write candidates registry."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CANDIDATES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CANDIDATES_FILE)


def load_existing_sources() -> list:
    """Load current news sources as list of dicts with 'url' and 'name'."""
    try:
        with open(SOURCES_FILE) as f:
            cfg = json.load(f)
    except Exception:
        return []
    sources = []
    for section in ("sina_api", "rss_feeds", "hn_api"):
        for s in cfg.get("news_sources", {}).get(section, []):
            sources.append({"name": s.get("name", ""), "url": s.get("url", "")})
    return sources


# ============================================================
# Email report
# ============================================================

def _load_env(env_file: str) -> dict:
    """Load env vars from file."""
    env = {}
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    except Exception:
        pass
    return env


def generate_report_html(scored_candidates: list, existing_count: int) -> str:
    """Generate HTML email body for discovery report."""
    now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
    above_threshold = [c for c in scored_candidates if c.get("scores", {}).get("final", 0) >= SCORE_THRESHOLD]
    above_threshold.sort(key=lambda c: c["scores"]["final"], reverse=True)

    rows = []
    for i, c in enumerate(above_threshold[:15], 1):
        s = c["scores"]
        badge = "🟢" if s["final"] >= SCORE_EXCELLENT else "🟡"
        rows.append(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td>{badge} {_esc(c['name'])}</td>"
            f"<td><a href='{_esc(c['url'])}'>{_esc(c['url'][:60])}</a></td>"
            f"<td>{_esc(c.get('category', ''))}</td>"
            f"<td>{c.get('language', '')}</td>"
            f"<td><b>{s['final']:.2f}</b></td>"
            f"<td>{s['reliability']:.1f}/{s['freshness']:.1f}/{s['content_quality']:.1f}/{s['authority']:.1f}/{s['uniqueness']:.1f}</td>"
            f"<td>{c.get('validation', {}).get('article_count', '?')}</td>"
            f"</tr>"
        )

    table = "\n".join(rows) if rows else "<tr><td colspan='8'>No candidates above threshold</td></tr>"

    # Category breakdown
    cat_counts = {}
    for c in above_threshold:
        cat = c.get("category", "unknown")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    cat_summary = ", ".join(f"{k}: {v}" for k, v in sorted(cat_counts.items())) or "none"

    html = f"""<html><body style="font-family: -apple-system, sans-serif; max-width: 900px; margin: 0 auto;">
<h2>RSS Source Discovery Report — {now_bjt}</h2>
<p>Current pool: {existing_count} sources | New candidates above threshold: {len(above_threshold)} | Total scanned: {len(scored_candidates)}</p>
<p>Category breakdown: {cat_summary}</p>

<table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%; font-size: 13px;">
<tr style="background: #f0f0f0;">
<th>#</th><th>Name</th><th>URL</th><th>Category</th><th>Lang</th><th>Score</th><th>R/F/Q/A/U</th><th>Articles</th>
</tr>
{table}
</table>

<p style="color: #666; font-size: 12px; margin-top: 16px;">
Score breakdown: R=Reliability, F=Freshness, Q=Content Quality, A=Authority, U=Uniqueness<br>
🟢 = Excellent (≥{SCORE_EXCELLENT}) | 🟡 = Good (≥{SCORE_THRESHOLD})<br>
To promote: <code>python3 rss-promote-candidate.py --name "Feed Name" --limit 3</code>
</p>
</body></html>"""
    return html


def _esc(s: str) -> str:
    """HTML-escape a string."""
    import html as _html
    return _html.escape(str(s))


def send_report_email(html_body: str, candidate_count: int):
    """Send discovery report via curl SMTP."""
    env = _load_env(ENV_FILE)
    mail_to = env.get("MAIL_TO", "")
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")

    if not all([mail_to, smtp_user, smtp_pass]):
        print("WARNING: Missing email credentials, skipping report email", file=sys.stderr)
        return False

    now_bjt = datetime.now(BJT).strftime("%m/%d")
    subject = f"[RSS Discovery] {now_bjt}: {candidate_count} candidates found"
    subject_b64 = base64.b64encode(subject.encode("utf-8")).decode("ascii")

    mail_content = (
        f'From: "RSS Discovery" <{smtp_user}>\r\n'
        f"To: {mail_to}\r\n"
        f"Subject: =?UTF-8?B?{subject_b64}?=\r\n"
        f"Content-Type: text/html; charset=UTF-8\r\n"
        f"MIME-Version: 1.0\r\n"
        f"\r\n"
        f"{html_body}"
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".eml", delete=False, encoding="utf-8") as f:
        f.write(mail_content)
        mail_file = f.name

    try:
        result = subprocess.run(
            ["curl", "--silent", "--ssl-reqd", "--max-time", "30",
             "--url", f"smtps://{env.get('SMTP_SERVER', 'smtp.163.com')}:{env.get('SMTP_PORT', '465')}",
             "--user", f"{smtp_user}:{smtp_pass}",
             "--mail-from", smtp_user, "--mail-rcpt", mail_to,
             "--upload-file", mail_file],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode == 0:
            print(f"Report email sent to {mail_to}")
            return True
        else:
            print(f"Email failed: {result.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"Email error: {e}", file=sys.stderr)
        return False
    finally:
        os.unlink(mail_file)


# ============================================================
# CLI subcommands (called by Claude session via stdin/stdout JSON)
# ============================================================

def cmd_validate():
    """Read candidate list from stdin, validate feeds, write results to stdout."""
    candidates = json.load(sys.stdin)
    results = validate_feeds_parallel(candidates)
    json.dump(results, sys.stdout, indent=2, ensure_ascii=False)


def cmd_dedup():
    """Read candidate list from stdin, dedup against existing + prior, write filtered to stdout."""
    candidates = json.load(sys.stdin)
    existing = load_existing_sources()
    prior = load_candidates().get("candidates", [])
    filtered = dedup_candidates(candidates, existing, prior)
    json.dump(filtered, sys.stdout, indent=2, ensure_ascii=False)


def cmd_save():
    """Read scored candidates from stdin, merge into discovered-rss.json."""
    scored = json.load(sys.stdin)
    data = load_candidates()
    now = datetime.now(BJT).isoformat()
    data["last_discovery"] = now

    # Merge: update existing by URL, or append new
    existing_urls = {_normalize_url(c["url"]): i for i, c in enumerate(data["candidates"])}
    for c in scored:
        norm = _normalize_url(c["url"])
        if norm in existing_urls:
            idx = existing_urls[norm]
            data["candidates"][idx].update({
                "scores": c.get("scores", {}),
                "validation": c.get("validation", {}),
                "status": "scored",
            })
        else:
            data["candidates"].append({
                "name": c["name"],
                "url": c["url"],
                "language": c.get("language", "en"),
                "category": c.get("category", ""),
                "status": "scored",
                "discovered_at": now,
                "discovered_via": c.get("discovered_via", "unknown"),
                "scores": c.get("scores", {}),
                "validation": c.get("validation", {}),
                "promoted": False,
                "rejected": False,
                "reject_reason": None,
            })

    save_candidates(data)
    print(f"Saved {len(scored)} candidates to {CANDIDATES_FILE}")


def cmd_report():
    """Generate and send email report for recent candidates."""
    data = load_candidates()
    existing = load_existing_sources()
    scored = [c for c in data["candidates"] if c.get("scores") and not c.get("promoted") and not c.get("rejected")]
    html = generate_report_html(scored, len(existing))

    above = [c for c in scored if c.get("scores", {}).get("final", 0) >= SCORE_THRESHOLD]
    send_report_email(html, len(above))


def main():
    if len(sys.argv) < 2:
        print("Usage: rss-source-discovery.py <validate|dedup|save|report>", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "validate":
        cmd_validate()
    elif cmd == "dedup":
        cmd_dedup()
    elif cmd == "save":
        cmd_save()
    elif cmd == "report":
        cmd_report()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/global-news && python3 -m pytest tests/test_rss_discovery.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/global-news
git add rss-source-discovery.py tests/test_rss_discovery.py
git commit -m "feat(discovery): add RSS validation, scoring, dedup engine with tests"
```

---

### Task 3: Promotion Tool

**Files:**
- Create: `rss-promote-candidate.py`
- Test: `tests/test_rss_promote.py`

- [ ] **Step 1: Write failing tests for promotion**

Write `tests/test_rss_promote.py`:
```python
#!/usr/bin/env python3
"""Tests for rss-promote-candidate.py."""
import sys
import os
import json
import pytest
import tempfile
import importlib.util

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rss_promote_candidate", os.path.join(_repo, "rss-promote-candidate.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

promote_candidate = _mod.promote_candidate


class TestPromoteCandidate:
    def test_promote_existing_candidate(self, tmp_path):
        # Setup candidates file
        candidates = {
            "version": 1, "last_discovery": None,
            "candidates": [
                {"name": "Test Feed", "url": "https://example.com/rss",
                 "language": "en", "category": "科技/AI", "status": "scored",
                 "scores": {"final": 0.85}, "validation": {"article_count": 20},
                 "promoted": False, "rejected": False, "reject_reason": None,
                 "discovered_at": "2026-04-06T00:00+08:00", "discovered_via": "ai_search"},
            ],
        }
        cand_file = tmp_path / "discovered-rss.json"
        cand_file.write_text(json.dumps(candidates), encoding="utf-8")

        # Setup sources config
        sources = {"news_sources": {"sina_api": [], "rss_feeds": [
            {"name": "Existing", "url": "https://existing.com/rss", "keywords": [], "limit": 3}
        ], "hn_api": []}}
        src_file = tmp_path / "news-sources-config.json"
        src_file.write_text(json.dumps(sources), encoding="utf-8")

        result = promote_candidate("Test Feed", limit=3, candidates_file=str(cand_file), sources_file=str(src_file))
        assert result is True

        # Verify candidate marked promoted
        updated = json.loads(cand_file.read_text())
        assert updated["candidates"][0]["promoted"] is True

        # Verify added to sources config
        updated_src = json.loads(src_file.read_text())
        names = [s["name"] for s in updated_src["news_sources"]["rss_feeds"]]
        assert "Test Feed" in names

    def test_promote_nonexistent_candidate(self, tmp_path):
        candidates = {"version": 1, "last_discovery": None, "candidates": []}
        cand_file = tmp_path / "discovered-rss.json"
        cand_file.write_text(json.dumps(candidates), encoding="utf-8")
        src_file = tmp_path / "news-sources-config.json"
        src_file.write_text(json.dumps({"news_sources": {"rss_feeds": []}}), encoding="utf-8")

        result = promote_candidate("Nonexistent", limit=3, candidates_file=str(cand_file), sources_file=str(src_file))
        assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/global-news && python3 -m pytest tests/test_rss_promote.py -v
```

- [ ] **Step 3: Implement rss-promote-candidate.py**

Write `rss-promote-candidate.py`:
```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Promote a discovered RSS candidate into news-sources-config.json.
Usage: python3 rss-promote-candidate.py --name "Feed Name" [--limit 3]
"""

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CANDIDATES = os.path.join(SCRIPT_DIR, "config", "discovered-rss.json")
DEFAULT_SOURCES = os.path.join(SCRIPT_DIR, "news-sources-config.json")


def promote_candidate(name: str, limit: int = 3,
                      candidates_file: str = DEFAULT_CANDIDATES,
                      sources_file: str = DEFAULT_SOURCES) -> bool:
    """Promote a candidate by name. Returns True on success."""
    # Load candidates
    with open(candidates_file, encoding="utf-8") as f:
        cand_data = json.load(f)

    # Find candidate
    target = None
    target_idx = None
    for i, c in enumerate(cand_data.get("candidates", [])):
        if c["name"] == name and not c.get("promoted"):
            target = c
            target_idx = i
            break

    if target is None:
        print(f"Candidate '{name}' not found or already promoted", file=sys.stderr)
        return False

    # Load sources config
    with open(sources_file, encoding="utf-8") as f:
        src_data = json.load(f)

    # Add to rss_feeds
    new_entry = {
        "name": target["name"],
        "url": target["url"],
        "keywords": [],
        "limit": limit,
    }
    src_data.setdefault("news_sources", {}).setdefault("rss_feeds", []).append(new_entry)

    # Write sources config (atomic)
    tmp = sources_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(src_data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, sources_file)

    # Mark promoted in candidates
    cand_data["candidates"][target_idx]["promoted"] = True
    tmp = candidates_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cand_data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, candidates_file)

    print(f"Promoted '{name}' → {sources_file} (limit={limit})")
    return True


def main():
    parser = argparse.ArgumentParser(description="Promote RSS candidate to production")
    parser.add_argument("--name", required=True, help="Candidate feed name")
    parser.add_argument("--limit", type=int, default=3, help="Article limit (default: 3)")
    args = parser.parse_args()

    if not promote_candidate(args.name, args.limit):
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/global-news && python3 -m pytest tests/test_rss_promote.py -v
```

- [ ] **Step 5: Commit**

```bash
cd ~/global-news
git add rss-promote-candidate.py tests/test_rss_promote.py
git commit -m "feat(discovery): add candidate promotion tool with tests"
```

---

### Task 4: Claude Code Session Wrapper

**Files:**
- Create: `scripts/rss-source-discovery.sh`

This is the cron entry point. It launches a Claude Code session with a prompt that instructs Claude to perform the 4-stage discovery pipeline.

- [ ] **Step 1: Write the wrapper script**

Write `scripts/rss-source-discovery.sh`:
```bash
#!/bin/bash
# RSS Source Discovery — Claude Code session driver
# Cron: daily 03:30 BJT via cron-wrapper.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

cleanup() {
    local pids
    pids=$(jobs -p 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "$LOG_PREFIX Cleaning up child processes..."
        kill $pids 2>/dev/null || true
        sleep 2
        kill -9 $pids 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "$LOG_PREFIX Starting RSS source discovery..."

# Use Max plan auth
unset ANTHROPIC_API_KEY
unset CLAUDECODE

cd "$REPO_DIR"

CATEGORIES_FILE="$REPO_DIR/config/rss-discovery-categories.json"
SOURCES_FILE="$REPO_DIR/news-sources-config.json"

if [ ! -f "$CATEGORIES_FILE" ]; then
    echo "$LOG_PREFIX ERROR: $CATEGORIES_FILE not found"
    exit 1
fi

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

# RSS Source Discovery Task

You are an RSS feed discovery agent. Your job: find high-quality RSS feeds across 8 categories, validate them, score them, and generate a report.

## Current State
- Existing sources: $(python3 -c "import json; d=json.load(open('$SOURCES_FILE')); print(sum(len(d['news_sources'][k]) for k in d['news_sources']))")
- Categories file: $CATEGORIES_FILE
- Helper script: $REPO_DIR/rss-source-discovery.py (subcommands: validate, dedup, save, report)

## Your Steps

### Step 1: Read current state
- Read $CATEGORIES_FILE for category definitions and search queries
- Read $SOURCES_FILE to know what sources already exist
- Read config/discovered-rss.json for prior candidates

### Step 2: Discover candidates (dual-channel)
For EACH of the 8 categories in the categories file:
1. Use web search with the provided search queries to find RSS feed URLs
2. Extract actual feed URLs (ending in /rss, /feed, .xml, or containing 'rss', 'feed', 'atom')
3. Collect: name, url, language, category, discovered_via='ai_search'

Also check the directory_urls in the categories file for curated RSS lists.

### Step 3: Dedup
Pipe your candidate list (JSON array) through:
\`\`\`
echo '<json_array>' | python3 rss-source-discovery.py dedup
\`\`\`
This removes candidates that match existing sources or prior rejected/promoted candidates.

### Step 4: Validate
Pipe the deduped list through:
\`\`\`
echo '<json_array>' | python3 rss-source-discovery.py validate
\`\`\`
This fetches each feed and checks parse success, article count, freshness, content richness.

### Step 5: Score
For each validated candidate (parse_ok=true, article_count > 0):
- Assess **authority** (0.0-1.0): Is this a well-known media outlet or respected publication?
- Assess **uniqueness** (0.0-1.0): How much does this overlap with our existing sources?

Then call compute_scores via Python:
\`\`\`python
python3 -c \"
import json, sys
sys.path.insert(0, '$REPO_DIR')
import importlib.util
spec = importlib.util.spec_from_file_location('m', '$REPO_DIR/rss-source-discovery.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
candidates = json.load(open('/tmp/rss-validated.json'))
for c in candidates:
    v = c['validation']
    if v.get('parse_ok') and v.get('article_count', 0) > 0:
        c['scores'] = m.compute_scores(v, authority=<YOUR_SCORE>, uniqueness=<YOUR_SCORE>, weights=m.load_weights())
json.dump(candidates, open('/tmp/rss-scored.json', 'w'), indent=2, ensure_ascii=False)
\"
\`\`\`

### Step 6: Save + Report
\`\`\`
cat /tmp/rss-scored.json | python3 rss-source-discovery.py save
python3 rss-source-discovery.py report
\`\`\`

### Step 7: Push
\`\`\`
cd $REPO_DIR && git add config/discovered-rss.json && git diff --cached --quiet || git commit -m 'data(discovery): update RSS candidates' && git push
\`\`\`

## Constraints
- Maximum 30 minutes for this session
- Target: 5-15 new candidates per run
- Only recommend feeds that parse successfully and have recent articles
- Authority score: 0.9+ for major outlets (BBC, NYT, Reuters), 0.7 for established blogs, 0.5 for small/unknown
- Uniqueness score: 0.9 if covers topic/region not in current pool, 0.5 if overlaps partially, 0.2 if heavily overlaps
"

# 30-minute timeout + 30s grace
CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
timeout --kill-after=30 1800 "$CLAUDE_BIN" -p --model sonnet "$PROMPT" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    echo "$LOG_PREFIX RSS discovery TIMED OUT after 30 minutes"
elif [ $EXIT_CODE -ne 0 ]; then
    echo "$LOG_PREFIX RSS discovery failed (exit code: $EXIT_CODE)"
else
    echo "$LOG_PREFIX RSS discovery finished successfully"
fi

# Ensure any commits are pushed
cd "$REPO_DIR"
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")
LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
if [ -n "$REMOTE_HEAD" ] && [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
    echo "$LOG_PREFIX Pushing new commits..."
    git push 2>&1 || echo "$LOG_PREFIX WARNING: git push failed"
fi
```

- [ ] **Step 2: Make executable**

```bash
chmod +x ~/global-news/scripts/rss-source-discovery.sh
```

- [ ] **Step 3: Verify script syntax**

```bash
bash -n ~/global-news/scripts/rss-source-discovery.sh && echo "OK"
```

- [ ] **Step 4: Commit**

```bash
cd ~/global-news
git add scripts/rss-source-discovery.sh
git commit -m "feat(discovery): add Claude Code session wrapper for RSS discovery"
```

---

### Task 5: Cron Integration

**Files:**
- Modify: crontab

- [ ] **Step 1: Add cron entry**

```bash
(crontab -l; echo '# RSS source discovery — daily 03:30 BJT (19:30 UTC)
30 19 * * * ~/cron-wrapper.sh --name rss-discovery --timeout 2400 --lock -- ~/global-news/scripts/rss-source-discovery.sh >> ~/logs/rss-discovery.log 2>&1') | crontab -
```

- [ ] **Step 2: Verify cron entry**

```bash
crontab -l | grep rss-discovery
```
Expected: shows the new cron line.

- [ ] **Step 3: Commit (no file change — cron is system-level)**

No git commit needed for crontab. But log the addition:
```bash
echo "Added rss-discovery cron: daily 03:30 BJT (19:30 UTC), 40min timeout"
```

---

### Task 6: End-to-End Smoke Test

- [ ] **Step 1: Run all unit tests**

```bash
cd ~/global-news && python3 -m pytest tests/test_rss_discovery.py tests/test_rss_promote.py -v
```
Expected: all tests PASS.

- [ ] **Step 2: Test validation subcommand manually**

```bash
cd ~/global-news
echo '[{"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "language": "en", "category": "欧洲时政"}]' | python3 rss-source-discovery.py validate | python3 -m json.tool | head -20
```
Expected: JSON output with `parse_ok: true`, `article_count > 0`.

- [ ] **Step 3: Test dedup subcommand**

```bash
cd ~/global-news
echo '[{"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"}]' | python3 rss-source-discovery.py dedup
```
Expected: empty array `[]` (BBC World already in sources).

- [ ] **Step 4: Test with a new feed**

```bash
cd ~/global-news
echo '[{"name": "STAT News", "url": "https://www.statnews.com/feed/", "language": "en", "category": "医药医疗前沿"}]' | python3 rss-source-discovery.py validate | python3 -m json.tool | head -20
```
Expected: JSON with validation results for a feed not in current pool.

- [ ] **Step 5: Final commit + push**

```bash
cd ~/global-news && git push
```
