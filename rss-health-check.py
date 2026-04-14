#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 健康检查监控
- 并行检查所有新闻源：HTTP可达 + 解析 + 文章数 + 新鲜度
- 连续失败3次自动切换至备用URL
- 支持 --email 发送告警邮件
"""

import urllib.request
import urllib.error
import json
import xml.etree.ElementTree as ET
import sys
import os
import time
import re
import subprocess
import tempfile
import base64
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

BJT = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "news-sources-config.json")
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_FILE = os.path.join(LOGS_DIR, "rss-health.json")
ENV_FILE = os.path.expanduser("~/.smtp.env")

FETCH_TIMEOUT = 10
FAIL_THRESHOLD = 3  # consecutive failures before auto-swap
DEFAULT_MAX_AGE_HOURS = 72
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ============================================================
# Fallback URLs (RSSHub mirrors for sources with known alternatives)
# ============================================================
FALLBACK_URLS = {
    "虎嗅":     "https://rsshub.rssforever.com/huxiu/article",
    "IT之家":   "https://rsshub.rssforever.com/ithome",
    "36氪":     "https://rsshub.rssforever.com/36kr/news",
    "少数派":   "https://rsshub.rssforever.com/sspai/matrix",
    "钛媒体":   "https://rsshub.rssforever.com/tmtpost/recommend",
    "界面新闻": "https://rsshub.rssforever.com/jiemian/list/4",
    "Solidot":  "https://rsshub.rssforever.com/solidot",
    "南方周末": "https://rsshub.rssforever.com/infzm/2",
}


def _parse_date_flexible(date_str):
    """Parse date string supporting RFC 2822, ISO 8601, and common variants."""
    s = date_str.strip()
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except Exception as e:
        logging.debug("parsedate_to_datetime failed for %r: %s", date_str, e)
    try:
        return datetime.fromisoformat(s)
    except Exception as e:
        logging.debug("fromisoformat failed for %r: %s", date_str, e)
    cleaned = re.sub(r'\s*([+-]\d{4})$', r' \1', s)
    if cleaned != s:
        try:
            return datetime.fromisoformat(cleaned)
        except Exception as e:
            logging.debug("fromisoformat failed for cleaned %r: %s", date_str, e)
    return None


def load_env(path):
    """Load KEY=VALUE from env file (like .stock-monitor.env)."""
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


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def swap_url_in_config(old_url, new_url):
    """Replace a URL in the config file via text substitution, preserving formatting.
    Uses atomic write (temp file + rename) to prevent corruption."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        text = f.read()
    # JSON-escape the URLs for safe matching
    old_json = json.dumps(old_url)  # includes surrounding quotes
    new_json = json.dumps(new_url)
    if old_json not in text:
        return False
    text = text.replace(old_json, new_json, 1)
    # Atomic write: write to temp file in same directory, then rename
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(CONFIG_FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, CONFIG_FILE)
    except Exception as e:
        logging.warning("Config write failed, cleaning up temp file: %s", e)
        os.unlink(tmp_path)
        raise
    return True


def load_state():
    if os.path.isfile(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ============================================================
# Per-source health check
# ============================================================

def check_source(name, url, source_type, max_age_hours):
    """
    Check a single source. Returns (name, status_dict).
    status_dict: {"ok": bool, "error": str|None, "article_count": int, "newest_age_hours": float|None}
    """
    result = {"ok": False, "error": None, "article_count": 0, "newest_age_hours": None}

    # Step 1: HTTP fetch
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            raw = r.read()
    except Exception as e:
        result["error"] = f"unreachable: {type(e).__name__}"
        return name, result

    if not raw or len(raw) == 0:
        result["error"] = "empty response"
        return name, result

    # Step 2 & 3: Parse and extract articles
    now_ts = time.time()

    if source_type == "json":
        # Sina API format
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            result["error"] = "JSON parse error"
            return name, result

        articles = data.get("result", {}).get("data", [])
        if not articles:
            result["error"] = "empty feed (0 articles)"
            return name, result

        result["article_count"] = len(articles)

        # Check freshness via ctime (unix timestamp)
        newest_ts = None
        for item in articles:
            ctime = item.get("ctime", "")
            if ctime:
                try:
                    ts = int(ctime)
                    if newest_ts is None or ts > newest_ts:
                        newest_ts = ts
                except (ValueError, TypeError):
                    pass

        if newest_ts is not None:
            age_hours = (now_ts - newest_ts) / 3600
            result["newest_age_hours"] = round(age_hours, 1)
            if age_hours > max_age_hours:
                result["error"] = f"stale feed (newest {age_hours:.0f}h, max {max_age_hours}h)"
                return name, result

    else:
        # RSS/Atom XML
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except Exception:
                result["error"] = "encoding error"
                return name, result

        try:
            # Strip illegal XML control characters (e.g. \x1e from 36氪 feed)
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError:
            result["error"] = "XML parse error"
            return name, result

        atom_ns = "{http://www.w3.org/2005/Atom}"
        items = root.findall(".//item")
        if not items:
            items = root.findall(f".//{atom_ns}entry")

        if not items:
            result["error"] = "empty feed (0 articles)"
            return name, result

        result["article_count"] = len(items)

        # Check freshness
        newest_ts = None
        for item in items:
            pub_str = (item.findtext("pubDate")
                       or item.findtext(f"{atom_ns}published")
                       or item.findtext(f"{atom_ns}updated")
                       or "")
            if pub_str.strip():
                dt = _parse_date_flexible(pub_str)
                if dt is not None:
                    ts = dt.timestamp()
                    if newest_ts is None or ts > newest_ts:
                        newest_ts = ts

        if newest_ts is not None:
            age_hours = (now_ts - newest_ts) / 3600
            result["newest_age_hours"] = round(age_hours, 1)
            if age_hours > max_age_hours:
                result["error"] = f"stale feed (newest {age_hours:.0f}h, max {max_age_hours}h)"
                return name, result

    # All checks passed
    result["ok"] = True
    return name, result


# ============================================================
# Main monitor logic
# ============================================================

def run_checks():
    """Run health checks on all sources. Returns (results, config, state)."""
    config = load_config()
    state = load_state()
    now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")

    # Build task list
    tasks = []
    sources = config.get("news_sources", {})

    for src in sources.get("sina_api", []):
        name = src.get("name", "Unknown")
        url = src.get("url", "")
        max_age = src.get("max_age_hours", DEFAULT_MAX_AGE_HOURS)
        tasks.append((name, url, "json", max_age))

    for src in sources.get("rss_feeds", []):
        name = src.get("name", "Unknown")
        url = src.get("url", "")
        max_age = src.get("max_age_hours", DEFAULT_MAX_AGE_HOURS)
        tasks.append((name, url, "rss", max_age))

    # Parallel check
    results = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(check_source, name, url, stype, max_age): name
            for name, url, stype, max_age in tasks
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                _, res = future.result()
                results[name] = res
            except Exception as e:
                results[name] = {"ok": False, "error": f"exception: {e}", "article_count": 0, "newest_age_hours": None}

    # Update state & handle failures/recoveries
    swapped = []   # [(name, old_url, new_url)]
    reverted = []  # [(name, fallback_url, original_url)]
    for name, res in results.items():
        entry = state.get(name, {"consecutive_fails": 0, "last_check": None, "last_error": None, "swapped_from": None})

        if res["ok"]:
            entry["consecutive_fails"] = 0
            entry["last_error"] = None
        else:
            entry["consecutive_fails"] = entry.get("consecutive_fails", 0) + 1
            entry["last_error"] = res["error"]

        entry["last_check"] = now_bjt
        state[name] = entry

        # Auto-swap at threshold (only if not already swapped)
        if not res["ok"] and entry["consecutive_fails"] >= FAIL_THRESHOLD and name in FALLBACK_URLS:
            if entry.get("swapped_from") is not None:
                continue
            fallback_url = FALLBACK_URLS[name]
            old_url = _get_current_url(config, name)
            if old_url and swap_url_in_config(old_url, fallback_url):
                entry["swapped_from"] = old_url
                swapped.append((name, old_url, fallback_url))

        # Auto-revert: if source is healthy AND was previously swapped, try reverting
        # We probe the original URL to see if it's back before reverting
        if res["ok"] and entry.get("swapped_from"):
            original_url = entry["swapped_from"]
            current_url = _get_current_url(config, name)
            if current_url and current_url != original_url:
                # Probe the original URL before reverting
                _, probe = check_source(name, original_url, "rss", DEFAULT_MAX_AGE_HOURS)
                if probe["ok"]:
                    if swap_url_in_config(current_url, original_url):
                        reverted.append((name, current_url, original_url))
                        entry["swapped_from"] = None

    # Save updated state
    save_state(state)

    return results, state, swapped, reverted


def _get_current_url(config, source_name):
    """Find the current URL for a source by name. Returns URL or None."""
    sources = config.get("news_sources", {})
    for section in ("sina_api", "rss_feeds"):
        for src in sources.get(section, []):
            if src.get("name") == source_name:
                return src["url"]
    return None


# ============================================================
# Reporting
# ============================================================

def format_console_report(results, state, swapped, reverted):
    """Plain text report for console/cron log."""
    now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
    lines = [f"RSS 健康检查报告 - {now_bjt}", ""]

    # Problem sources
    problems = []
    for name, res in sorted(results.items()):
        if not res["ok"]:
            entry = state.get(name, {})
            fails = entry.get("consecutive_fails", 0)
            error = res.get("error", "unknown")
            swap_info = ""
            for sname, old, new in swapped:
                if sname == name:
                    swap_info = f" → 已自动切换至 {new}"
                    break
            if fails >= FAIL_THRESHOLD:
                problems.append(f"  ❌ {name}: {error} (连续失败 {fails}次){swap_info}")
            else:
                problems.append(f"  ⚠️  {name}: {error} (连续失败 {fails}次)")

    healthy_count = sum(1 for r in results.values() if r["ok"])
    total = len(results)

    if problems:
        lines.append(f"⚠️  问题源 ({len(problems)}):")
        lines.extend(problems)
        lines.append("")

    lines.append(f"✅ 健康源 ({healthy_count}/{total})")

    # Show article counts for healthy sources
    lines.append("")
    lines.append("详细:")
    for name, res in sorted(results.items()):
        age_str = ""
        if res.get("newest_age_hours") is not None:
            age_str = f", 最新 {res['newest_age_hours']:.0f}h前"
        if res["ok"]:
            lines.append(f"  ✅ {name}: {res['article_count']}篇{age_str}")
        else:
            lines.append(f"  ❌ {name}: {res.get('error', 'unknown')}{age_str}")

    if swapped:
        lines.append("")
        lines.append("🔄 自动切换:")
        for name, old, new in swapped:
            lines.append(f"  {name}: {old} → {new}")

    if reverted:
        lines.append("")
        lines.append("↩️  自动恢复:")
        for name, fallback, original in reverted:
            lines.append(f"  {name}: {fallback} → {original} (原始URL已恢复)")

    lines.append("")
    lines.append(f"状态文件: {STATE_FILE}")
    return "\n".join(lines)


def send_alert_email(body):
    """Send alert email via curl SMTP (same pattern as aws-health-monitor.sh)."""
    env = load_env(ENV_FILE)
    mail_to = env.get("MAIL_TO", "")
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")

    if not all([mail_to, smtp_user, smtp_pass]):
        print("❌ 缺少邮件凭证 (MAIL_TO/SMTP_USER/SMTP_PASS)", file=sys.stderr)
        return False

    now_bjt = datetime.now(BJT).strftime("%m月%d日 %H:%M")
    subject = f"🔍 RSS健康检查 - {now_bjt}"
    subject_b64 = base64.b64encode(subject.encode("utf-8")).decode("ascii")

    mail_content = (
        f'From: "RSS监控" <{smtp_user}>\r\n'
        f"To: {mail_to}\r\n"
        f"Subject: =?UTF-8?B?{subject_b64}?=\r\n"
        f"Content-Type: text/plain; charset=UTF-8\r\n"
        f"MIME-Version: 1.0\r\n"
        f"\r\n"
        f"{body}"
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".eml", delete=False, encoding="utf-8") as f:
        f.write(mail_content)
        mail_file = f.name

    try:
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
            print(f"📧 告警邮件已发送至 {mail_to}")
            return True
        else:
            print(f"❌ 邮件发送失败: {result.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"❌ 邮件发送异常: {e}", file=sys.stderr)
        return False
    finally:
        os.unlink(mail_file)


# ============================================================
# Entry point
# ============================================================

def main():
    send_email = "--email" in sys.argv

    print(f"🔍 RSS 健康检查开始 - {datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')}")
    print(f"   配置文件: {CONFIG_FILE}")
    print(f"   状态文件: {STATE_FILE}")
    print()

    results, state, swapped, reverted = run_checks()
    report = format_console_report(results, state, swapped, reverted)
    print(report)

    # Only flag as "has_problems" if a source has failed ≥2 consecutive times.
    # Single failures are transient network blips — don't wake the cron alert.
    has_problems = any(
        not r["ok"] and state.get(name, {}).get("consecutive_fails", 0) >= 2
        for name, r in results.items()
    )
    has_changes = len(swapped) > 0 or len(reverted) > 0

    if send_email and (has_problems or has_changes):
        print()
        send_alert_email(report)
    elif send_email:
        print("\n✅ 全部健康，无需发送告警邮件")

    # Exit code: 0 = all ok or only 1 transient failure, 1 = ≥2 consecutive failures
    sys.exit(0 if not has_problems else 1)


if __name__ == "__main__":
    main()
