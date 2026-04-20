#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS Source Discovery — validation, scoring, dedup engine.

CLI subcommands: validate, dedup, save, report.
Each reads JSON from stdin or files and writes JSON to stdout or files.
Stdlib only — no pip dependencies.
"""

import json
import os
import re
import sys
import subprocess
import tempfile
import base64
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import rss_registry as _reg

# ============================================================
# Constants
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(SCRIPT_DIR, "config")
CANDIDATES_FILE = os.path.join(CONFIG_DIR, "discovered-rss.json")
WEIGHTS_FILE = os.path.join(CONFIG_DIR, "rss-scorer-weights.json")
SOURCES_FILE = os.path.join(SCRIPT_DIR, "news-sources-config.json")
ENV_FILE = os.path.expanduser("~/.stock-monitor.env")

FETCH_TIMEOUT = 15
MAX_POOL_SIZE = 50  # keep only top-N pending candidates; auto-reject the rest
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB cap to prevent memory exhaustion
SCORE_THRESHOLD = 0.60
SCORE_EXCELLENT = 0.90  # aligned with PROMOTE_THRESHOLD (rss-trial-manager.py): Excellent badge now means "will auto-promote to trial on next discovery run"
BJT = timezone(timedelta(hours=8))

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/elements/1.1/"


# ============================================================
# Utility
# ============================================================

def _parse_date_flexible(date_str: str) -> datetime | None:
    """Parse date string supporting RFC 2822, ISO 8601, and common variants."""
    s = date_str.strip()
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    # Handle Z suffix
    s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(s2)
    except Exception:
        pass
    cleaned = re.sub(r'\s*([+-]\d{4})$', r' \1', s)
    if cleaned != s:
        try:
            return datetime.fromisoformat(cleaned)
        except Exception:
            pass
    return None


def _normalize_url(url: str) -> str:
    """Normalize URL for dedup comparison: lowercase domain, strip trailing slash."""
    url = url.strip()
    # Split into scheme+authority and path
    match = re.match(r'^(https?://)([^/]+)(.*)', url, re.IGNORECASE)
    if not match:
        return url.rstrip("/").lower()
    scheme = match.group(1).lower()
    domain = match.group(2).lower()
    path = match.group(3).rstrip("/")
    return f"{scheme}{domain}{path}"


def load_env(path: str) -> dict:
    """Load KEY=VALUE from env file."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ============================================================
# Core functions
# ============================================================

def validate_feed(name: str, url: str, raw_bytes: bytes | None = None) -> dict:
    """Validate a single RSS/Atom feed.

    If raw_bytes provided, skip HTTP fetch (for testing).
    Returns dict with http_status, parse_ok, article_count, newest_age_hours,
    has_descriptions, has_authors, has_categories, error.
    """
    result = {
        "http_status": 0,
        "parse_ok": False,
        "article_count": 0,
        "newest_age_hours": None,
        "has_descriptions": False,
        "has_authors": False,
        "has_categories": False,
        "avg_description_length": 0,
        "error": None,
    }

    # Fetch if no raw_bytes
    if raw_bytes is None:
        # SSRF guard: only allow http/https schemes
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            result["error"] = f"rejected scheme: {parsed.scheme}"
            return result
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                result["http_status"] = resp.status
                raw_bytes = resp.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as e:
            result["http_status"] = e.code
            result["error"] = f"HTTP {e.code}"
            return result
        except Exception as e:
            result["error"] = str(e)
            return result
    else:
        result["http_status"] = 200

    # Parse XML
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as e:
        result["error"] = f"XML parse error: {e}"
        return result

    result["parse_ok"] = True

    # Detect RSS vs Atom
    items = []
    is_atom = root.tag == f"{{{ATOM_NS}}}feed" or root.tag == "feed"

    if is_atom:
        items = root.findall(f"{{{ATOM_NS}}}entry")
        if not items:
            items = root.findall("entry")
    else:
        # RSS 2.0
        items = root.findall(".//item")

    result["article_count"] = len(items)

    if len(items) == 0:
        result["error"] = "Empty feed — no items found"
        return result

    # Analyze items
    desc_count = 0
    author_count = 0
    cat_count = 0
    newest_dt = None
    desc_lengths: list[int] = []
    now = datetime.now(timezone.utc)

    for item in items:
        # Description
        if is_atom:
            desc_el = (
                item.find(f"{{{ATOM_NS}}}summary")
                or item.find(f"{{{ATOM_NS}}}content")
                or item.find("summary")
                or item.find("content")
            )
        else:
            desc_el = item.find("description")
        if desc_el is not None and desc_el.text and desc_el.text.strip():
            desc_count += 1
            # Strip HTML tags to measure plain-text content depth
            plain = re.sub(r"<[^>]+>", "", desc_el.text).strip()
            if plain:
                desc_lengths.append(len(plain))

        # Author
        auth_el = None
        if is_atom:
            auth_el = item.find(f"{{{ATOM_NS}}}author")
            if auth_el is None:
                auth_el = item.find("author")
        else:
            auth_el = item.find(f"{{{DC_NS}}}creator")
            if auth_el is None:
                auth_el = item.find("author")
        if auth_el is not None:
            # Atom author has sub-elements; RSS dc:creator has text
            if auth_el.text and auth_el.text.strip():
                author_count += 1
            elif auth_el.find(f"{{{ATOM_NS}}}name") is not None:
                author_count += 1
            elif auth_el.find("name") is not None:
                author_count += 1

        # Category
        if is_atom:
            cat_el = item.find(f"{{{ATOM_NS}}}category") or item.find("category")
        else:
            cat_el = item.find("category")
        if cat_el is not None:
            cat_count += 1

        # Date
        date_str = None
        if is_atom:
            for tag in [f"{{{ATOM_NS}}}published", f"{{{ATOM_NS}}}updated", "published", "updated"]:
                el = item.find(tag)
                if el is not None and el.text:
                    date_str = el.text
                    break
        else:
            for tag in ["pubDate", "dc:date", f"{{{DC_NS}}}date"]:
                el = item.find(tag)
                if el is not None and el.text:
                    date_str = el.text
                    break

        if date_str:
            dt = _parse_date_flexible(date_str)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if newest_dt is None or dt > newest_dt:
                    newest_dt = dt

    total = len(items)
    result["has_descriptions"] = (desc_count / total) > 0.5
    result["has_authors"] = (author_count / total) > 0.3
    result["has_categories"] = (cat_count / total) > 0.3
    result["avg_description_length"] = (
        int(sum(desc_lengths) / len(desc_lengths)) if desc_lengths else 0
    )

    if newest_dt is not None:
        age = (now - newest_dt).total_seconds() / 3600.0
        result["newest_age_hours"] = round(age, 2)

    return result


def validate_feeds_parallel(candidates: list) -> list:
    """Validate multiple feeds in parallel using ThreadPoolExecutor."""
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_idx = {}
        for i, c in enumerate(candidates):
            future = executor.submit(
                validate_feed,
                c.get("name", ""),
                c.get("url", ""),
                c.get("raw_bytes"),
            )
            future_to_idx[future] = i

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                validation = future.result()
            except Exception as e:
                validation = {
                    "http_status": 0,
                    "parse_ok": False,
                    "article_count": 0,
                    "newest_age_hours": None,
                    "has_descriptions": False,
                    "has_authors": False,
                    "has_categories": False,
                    "avg_description_length": 0,
                    "error": str(e),
                }
            entry = {**candidates[idx], "validation": validation, "_order": idx}
            results.append(entry)

    # Preserve original order
    results.sort(key=lambda x: x.pop("_order", 0))
    return results


def compute_scores(validation: dict, authority: float, uniqueness: float,
                   weights: dict | None = None) -> dict:
    """Compute 5-dimension scores + final weighted score.

    Returns dict with reliability, freshness, content_quality, content_depth,
    authority, uniqueness, and final scores.

    Weight rationale (v2):
      reliability   0.10  — demoted: acts as gate (broken feeds score ~0), not differentiator
      freshness     0.15  — reduced; cadence-adjusted for low-frequency specialty publishers
      content_quality 0.25 — elevated; now includes content_depth sub-signal
      authority     0.30  — elevated: strongest editorial-quality signal
      uniqueness    0.20  — elevated: rewards coverage gaps in existing pool
    """
    default_weights = {
        "reliability": 0.10,
        "freshness": 0.15,
        "content_quality": 0.25,
        "authority": 0.30,
        "uniqueness": 0.20,
    }
    if weights is None:
        try:
            if os.path.isfile(WEIGHTS_FILE):
                with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # Validate: all required keys present and numeric
                for k in default_weights:
                    if not isinstance(loaded.get(k), (int, float)):
                        raise ValueError(f"invalid or missing weight: {k}")
                weights = loaded
            else:
                weights = default_weights
        except Exception:
            weights = default_weights

    # Reliability — gate + soft signal
    # Broken feeds (parse_ok=False) get 0.2; live feeds with articles get 0.8-1.0.
    # At weight=0.10 this penalises broken feeds by at most 0.08 vs a perfect feed.
    if validation.get("parse_ok") and validation.get("article_count", 0) >= 20:
        reliability = 1.0
    elif validation.get("parse_ok") and validation.get("article_count", 0) >= 5:
        reliability = 0.8
    elif validation.get("parse_ok") and validation.get("article_count", 0) >= 1:
        reliability = 0.6
    else:
        reliability = 0.2

    # Freshness — cadence-aware
    # Low-frequency publishers (article_count <= 10, e.g. weekly specialty journals)
    # use a gentler decay so a 3-day-old article isn't penalised like breaking news.
    age = validation.get("newest_age_hours")
    article_count = validation.get("article_count", 0)
    is_low_frequency = article_count <= 10

    if age is None:
        freshness = 0.0
    elif age <= 6:
        freshness = 1.0
    elif age <= 24:
        freshness = 1.0 if is_low_frequency else 0.8
    elif age <= 48:
        freshness = 0.8 if is_low_frequency else 0.5
    elif age <= 96:
        freshness = 0.5 if is_low_frequency else 0.2
    elif age <= 168:
        freshness = 0.3 if is_low_frequency else 0.0
    else:
        freshness = 0.0

    # Content quality — metadata presence only (3 signals, normalised to 1.0)
    # content_depth (RSS description length) is computed separately as an operational
    # flag but does NOT affect the score. Rationale: description length reflects a
    # publication's RSS formatting policy, not editorial quality. Good sources like
    # Foreign Policy or 端传媒 use short RSS teasers by design; penalising them here
    # conflates RSS format with content quality. Usability is validated during trial
    # (3-day selected count) instead.
    #   has_descriptions → 0.50  (most important: any summary at all)
    #   has_authors      → 0.31  (bylines signal editorial accountability)
    #   has_categories   → 0.19  (taxonomy signals structured editorial workflow)
    avg_desc_len = validation.get("avg_description_length", 0)
    if avg_desc_len >= 200:
        content_depth = 1.0
    elif avg_desc_len >= 100:
        content_depth = 0.6
    elif avg_desc_len >= 50:
        content_depth = 0.3
    else:
        content_depth = 0.0

    content_quality = (
        (0.50 if validation.get("has_descriptions") else 0.0)
        + (0.31 if validation.get("has_authors") else 0.0)
        + (0.19 if validation.get("has_categories") else 0.0)
    )

    # Clamp authority and uniqueness
    authority = max(0.0, min(1.0, float(authority)))
    uniqueness = max(0.0, min(1.0, float(uniqueness)))

    # Weighted final
    final = (
        weights.get("reliability", 0.10) * reliability
        + weights.get("freshness", 0.15) * freshness
        + weights.get("content_quality", 0.25) * content_quality
        + weights.get("authority", 0.30) * authority
        + weights.get("uniqueness", 0.20) * uniqueness
    )

    return {
        "reliability": round(reliability, 2),
        "freshness": round(freshness, 2),
        "content_quality": round(content_quality, 2),
        "content_depth": round(content_depth, 2),
        "authority": round(authority, 2),
        "uniqueness": round(uniqueness, 2),
        "final": round(final, 3),
    }


def is_duplicate(url: str, existing: list) -> bool:
    """Check if url is a duplicate of any URL in existing list."""
    norm = _normalize_url(url)
    for item in existing:
        if _normalize_url(item.get("url", "")) == norm:
            return True
    return False


def dedup_candidates(candidates: list, existing_sources: list,
                     prior_candidates: list) -> list:
    """Remove duplicates against existing sources and prior promoted/rejected candidates."""
    # Build set of normalized existing URLs
    existing_urls = set()
    for src in existing_sources:
        existing_urls.add(_normalize_url(src.get("url", "")))

    # Prior candidates that were promoted or rejected
    prior_urls = set()
    for pc in prior_candidates:
        if pc.get("promoted") or pc.get("rejected"):
            prior_urls.add(_normalize_url(pc.get("url", "")))

    result = []
    seen = set()
    for c in candidates:
        norm = _normalize_url(c.get("url", ""))
        if norm in existing_urls or norm in prior_urls or norm in seen:
            continue
        seen.add(norm)
        result.append(c)

    return result


def load_candidates() -> dict:
    """Load candidates from rss-registry.json (discovered status only), shaped as legacy format."""
    registry = _reg.load_registry()
    discovered = _reg.get_by_status(registry, "discovered")
    return {"candidates": discovered}


def save_candidates(data: dict) -> None:
    """Deprecated: use rss_registry.upsert_source() directly. No-op stub kept for compatibility."""
    pass


def load_existing_sources() -> list:
    """Flatten all active sources into list of {name, url}.

    Includes sources from news-sources-config.json AND sources in registry
    with production/trialing status, to prevent re-discovering active sources.
    """
    sources = []
    seen_urls: set[str] = set()

    # From news-sources-config.json
    if os.path.isfile(SOURCES_FILE):
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        ns = config.get("news_sources", {})
        for section in ns.values():
            if isinstance(section, list):
                for s in section:
                    url_norm = _normalize_url(s.get("url", ""))
                    if url_norm and url_norm not in seen_urls:
                        sources.append(s)
                        seen_urls.add(url_norm)

    # Also include registry production/trialing sources
    try:
        registry = _reg.load_registry()
        for s in _reg.get_by_status(registry, "production", "trialing"):
            url_norm = _normalize_url(s.get("url", ""))
            if url_norm and url_norm not in seen_urls:
                sources.append({"name": s["name"], "url": s["url"]})
                seen_urls.add(url_norm)
    except Exception:
        pass

    return sources


def generate_report_html(scored_candidates: list, existing_count: int) -> str:
    """Generate HTML table of candidates above SCORE_THRESHOLD, sorted by final score."""
    above = [c for c in scored_candidates
             if c.get("scores", {}).get("final", 0) >= SCORE_THRESHOLD
             and not c.get("promoted") and not c.get("rejected")]
    above.sort(key=lambda x: x.get("scores", {}).get("final", 0), reverse=True)
    above = above[:15]

    if not above:
        return "<p>No new candidates above threshold.</p>"

    now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")

    rows = []
    for c in above:
        scores = c.get("scores", {})
        score = scores.get("final", 0)
        if score >= SCORE_EXCELLENT:
            badge = '<span style="background:#22c55e;color:#fff;padding:2px 8px;border-radius:4px;">Excellent</span>'
        else:
            badge = '<span style="background:#eab308;color:#fff;padding:2px 8px;border-radius:4px;">Good</span>'

        # R/F/Q/A/U breakdown
        breakdown = "/".join(
            str(scores.get(d, 0))
            for d in ("reliability", "freshness", "content_quality", "authority", "uniqueness")
        )
        article_count = c.get("validation", {}).get("article_count", "")

        # content_depth operational flag — does NOT affect score, shown as trial reminder
        depth = scores.get("content_depth", 1.0)
        if depth == 0.0:
            depth_flag = '<span style="color:#dc2626;font-size:11px;" title="RSS descriptions empty — likely paywall stub. Monitor selected count during trial.">⚠️ RSS空</span>'
        elif depth <= 0.3:
            depth_flag = '<span style="color:#d97706;font-size:11px;" title="RSS descriptions short (teaser only). Good sources like Foreign Policy use this style by design — watch selected count in trial.">⚠️ RSS短</span>'
        else:
            depth_flag = ""

        rows.append(
            f"<tr>"
            f"<td>{_html_escape(c.get('name', ''))}{(' ' + depth_flag) if depth_flag else ''}</td>"
            f"<td><a href=\"{_html_escape(c.get('url', ''))}\">{_html_escape(c.get('url', ''))}</a></td>"
            f"<td>{_html_escape(c.get('category', ''))}</td>"
            f"<td>{_html_escape(c.get('language', ''))}</td>"
            f"<td style='text-align:center'>{score:.3f}</td>"
            f"<td style='text-align:center'>{badge}</td>"
            f"<td style='text-align:center;font-family:monospace;'>{breakdown}</td>"
            f"<td style='text-align:center'>{article_count}</td>"
            f"</tr>"
        )

    legend_html = """\
<div style="margin:16px 0;padding:12px 16px;background:#f8fafc;border-left:4px solid #64748b;font-family:sans-serif;font-size:13px;line-height:1.7;">
  <strong>如何读这份报告</strong><br>
  <strong>Score（总分）</strong> 由 5 个维度加权得出：
  <span title="可访问性：feed 能否正常解析、有多少文章"><u>R</u>eliability</span>（10%）·
  <span title="更新频率：最新文章距今多久，考虑了低频专业媒体的发布节奏"><u>F</u>reshness</span>（15%）·
  <span title="内容元数据：是否有摘要/署名/分类，不含RSS摘要长度"><u>Q</u>uality</span>（25%）·
  <span title="权威性：AI 基于对出版物的知识打分，最重要的信号"><u>A</u>uthority</span>（30%）·
  <span title="唯一性：这个源能带来现有池子里没有的覆盖角度吗"><u>U</u>niqueness</span>（20%）<br>
  <strong>⚠️ RSS短 / RSS空</strong>：该源 RSS 摘要较短或为空，系编辑/付费墙策略所致，<em>不影响评分</em>。
  进入 trial 后如果 3 天 selected 篇数过少会自然淘汰。<br>
  <strong>晋级流程</strong>：Score ≥ 0.90 → 进入 3 天 trial（实测系统每天 selected 数）→ 3 天内 ≥ 3 篇入选 → 自动加入正式源
</div>"""

    html = f"""\
<h2>RSS Source Discovery Report</h2>
<p>Generated: {now_bjt} | Existing sources: {existing_count} | New candidates shown: {len(above)}</p>
{legend_html}
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;">
<tr style="background:#f3f4f6;">
  <th>Name</th><th>URL</th><th>Category</th><th>Language</th><th>Score</th><th>Rating</th><th>R/F/Q/A/U</th><th>Articles</th>
</tr>
{''.join(rows)}
</table>
"""
    return html


def _html_escape(s: str) -> str:
    """Minimal HTML escaping."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def send_report_email(html_body: str, candidate_count: int) -> bool:
    """Send discovery report email via curl SMTP."""
    env = load_env(ENV_FILE)
    mail_to = env.get("MAIL_TO", "")
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")

    if not all([mail_to, smtp_user, smtp_pass]):
        print("Missing email credentials (MAIL_TO/SMTP_USER/SMTP_PASS)", file=sys.stderr)
        return False

    now_bjt = datetime.now(BJT).strftime("%m月%d日 %H:%M")
    subject = f"🔍 RSS Discovery — {candidate_count} candidates — {now_bjt}"
    subject_b64 = base64.b64encode(subject.encode("utf-8")).decode("ascii")
    msg_id = f"<rss-discovery-{datetime.now(BJT).strftime('%Y%m%d%H%M%S')}-{os.getpid()}@ec2.sinostor.com.cn>"

    mail_content = (
        f'From: "RSS Discovery" <{smtp_user}>\r\n'
        f"To: {mail_to}\r\n"
        f"Message-ID: {msg_id}\r\n"
        f"Subject: =?UTF-8?B?{subject_b64}?=\r\n"
        f"Content-Type: text/html; charset=UTF-8\r\n"
        f"MIME-Version: 1.0\r\n"
        f"\r\n"
        f"{html_body}"
    )

    fd, mail_file = tempfile.mkstemp(suffix=".eml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(mail_content)
        result = subprocess.run(
            [
                "curl", "--silent", "--ssl-reqd",
                "--max-time", "30",
                "--url", f"smtps://{env.get('SMTP_SERVER', 'smtp.163.com')}:{env.get('SMTP_PORT', '465')}",
                "--user", f"{smtp_user}:{smtp_pass}",
                "--mail-from", smtp_user,
                "--mail-rcpt", mail_to,
                "--upload-file", mail_file,
            ],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode == 0:
            print(f"Report email sent to {mail_to} (Message-ID: {msg_id})")
            return True
        else:
            print(f"Email send failed: {result.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"Email send error: {e}", file=sys.stderr)
        return False
    finally:
        if os.path.exists(mail_file):
            os.unlink(mail_file)


# ============================================================
# CLI subcommands
# ============================================================

def cmd_validate():
    """Read JSON array from stdin, validate feeds, write JSON to stdout."""
    candidates = json.load(sys.stdin)
    results = validate_feeds_parallel(candidates)
    json.dump(results, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def cmd_dedup():
    """Read JSON array from stdin, dedup against existing+prior, write JSON to stdout."""
    candidates = json.load(sys.stdin)
    existing = load_existing_sources()
    prior_data = load_candidates()
    prior = prior_data.get("candidates", [])
    result = dedup_candidates(candidates, existing, prior)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def cmd_save():
    """Read JSON array from stdin, merge into rss-registry.json."""
    new_entries = json.load(sys.stdin)
    registry = _reg.load_registry()
    added = 0
    for entry in new_entries:
        if _reg.upsert_source(registry, entry):
            added += 1
    _reg.save_registry(registry)
    print(f"[cmd_save] Added {added}/{len(new_entries)} new candidates to registry.")


def cmd_report():
    """Generate and send report email for scored, non-promoted, non-rejected candidates."""
    data = load_candidates()
    candidates = data.get("candidates", [])
    existing = load_existing_sources()
    html = generate_report_html(candidates, len(existing))
    count = len([c for c in candidates
                 if c.get("scores", {}).get("final", 0) >= SCORE_THRESHOLD
                 and not c.get("promoted") and not c.get("rejected")])
    if count > 0:
        if not send_report_email(html, count):
            sys.exit(1)
    else:
        print("No candidates above threshold to report.")


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
        print(f"Unknown subcommand: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
