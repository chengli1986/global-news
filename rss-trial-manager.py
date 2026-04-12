#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS Trial Manager — auto-promote high-scoring candidates into 7-day trials.

Daily flow (called from rss-source-discovery.sh after discovery):
  1. If active trial: aggregate today's stats from trial-source-log.jsonl
     - If 7 days elapsed → send 7-day report email → mark trial ended
  2. If no active trial: pick highest-scoring candidate (score >= PROMOTE_THRESHOLD,
     not in history), add to news-sources-config.json, start trial

CLI:
  python3 rss-trial-manager.py run       — normal daily run
  python3 rss-trial-manager.py status    — print current trial state
  python3 rss-trial-manager.py remove    — remove active trial source from config
  python3 rss-trial-manager.py keep      — graduate active trial to permanent source
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(SCRIPT_DIR, "config")
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")

TRIAL_STATE_FILE = os.path.join(CONFIG_DIR, "trial-state.json")
CANDIDATES_FILE = os.path.join(CONFIG_DIR, "discovered-rss.json")
SOURCES_FILE = os.path.join(SCRIPT_DIR, "news-sources-config.json")
TRIAL_LOG_FILE = os.path.join(LOGS_DIR, "trial-source-log.jsonl")
ENV_FILE = os.path.expanduser("~/.stock-monitor.env")

PROMOTE_THRESHOLD = 0.90
TRIAL_DAYS = 7
AUTO_KEEP_MIN_SELECTED = 5  # auto-keep if ≥5 articles selected over 7-day trial
BJT = timezone(timedelta(hours=8))


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_env() -> dict:
    env: dict[str, str] = {}
    if not os.path.isfile(ENV_FILE):
        return env
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _today() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d")


def _atomic_write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── state I/O ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.isfile(TRIAL_STATE_FILE):
        return {"active_trial": None, "history": []}
    with open(TRIAL_STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    _atomic_write(TRIAL_STATE_FILE, state)


# ── candidates ────────────────────────────────────────────────────────────────

def get_promotable_candidates(state: dict) -> list:
    """Return candidates score >= threshold, not yet tried, sorted by score desc."""
    if not os.path.isfile(CANDIDATES_FILE):
        return []
    with open(CANDIDATES_FILE, encoding="utf-8") as f:
        data = json.load(f)

    tried_urls = {h["url"] for h in state.get("history", [])}
    active = state.get("active_trial")
    if active:
        tried_urls.add(active["url"])

    candidates = data.get("candidates", [])
    promotable = [
        c for c in candidates
        if c.get("scores", {}).get("final", 0) >= PROMOTE_THRESHOLD
        and not c.get("promoted")
        and not c.get("rejected")
        and c.get("url", "") not in tried_urls
    ]
    return sorted(promotable, key=lambda x: x.get("scores", {}).get("final", 0), reverse=True)


# ── news-sources-config.json management ──────────────────────────────────────

def add_trial_to_config(candidate: dict) -> None:
    """Append trial source to rss_feeds in news-sources-config.json."""
    with open(SOURCES_FILE, encoding="utf-8") as f:
        config = json.load(f)

    entry = {
        "name": candidate["name"],
        "url": candidate["url"],
        "keywords": [],
        "limit": 3,
        "trial": True,
    }
    config["news_sources"]["rss_feeds"].append(entry)
    _atomic_write(SOURCES_FILE, config)


def remove_trial_from_config(source_name: str) -> bool:
    """Remove trial source from rss_feeds. Returns True if found and removed."""
    with open(SOURCES_FILE, encoding="utf-8") as f:
        config = json.load(f)

    feeds = config["news_sources"]["rss_feeds"]
    original_len = len(feeds)
    config["news_sources"]["rss_feeds"] = [
        s for s in feeds if s.get("name") != source_name
    ]
    if len(config["news_sources"]["rss_feeds"]) < original_len:
        _atomic_write(SOURCES_FILE, config)
        return True
    return False


def graduate_trial_in_config(source_name: str) -> bool:
    """Remove trial=True flag from source (graduates to permanent)."""
    with open(SOURCES_FILE, encoding="utf-8") as f:
        config = json.load(f)

    found = False
    for s in config["news_sources"]["rss_feeds"]:
        if s.get("name") == source_name and s.get("trial"):
            del s["trial"]
            found = True
            break
    if found:
        _atomic_write(SOURCES_FILE, config)
    return found


# ── stats aggregation ─────────────────────────────────────────────────────────

def aggregate_today_stats(source_name: str) -> dict:
    """Read trial-source-log.jsonl, sum today's fetched/selected for source_name."""
    today = _today()
    fetched_total = 0
    selected_total = 0

    if not os.path.isfile(TRIAL_LOG_FILE):
        return {"date": today, "fetched": 0, "selected": 0}

    with open(TRIAL_LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("source") != source_name:
                continue
            entry_date = entry.get("ts", "")[:10]
            if entry_date != today:
                continue
            fetched_total += entry.get("fetched", 0)
            selected_total += entry.get("selected", 0)

    return {"date": today, "fetched": fetched_total, "selected": selected_total}


# ── report generation ─────────────────────────────────────────────────────────

def generate_report_html(trial: dict) -> str:
    name = _html_escape(trial["name"])
    url = _html_escape(trial["url"])
    score = trial.get("candidate_score", 0)
    start = trial.get("start_date", "?")
    end = trial.get("end_date", "?")
    stats = trial.get("daily_stats", [])

    total_fetched = sum(d.get("fetched", 0) for d in stats)
    total_selected = sum(d.get("selected", 0) for d in stats)

    rows = ""
    for d in stats:
        date = _html_escape(d.get("date", ""))
        fetched = d.get("fetched", 0)
        selected = d.get("selected", 0)
        rows += f"""
        <tr>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;">{date}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:center;">{fetched}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:center;">{selected}</td>
        </tr>"""

    now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")

    return f"""MIME-Version: 1.0
Content-Type: text/html; charset=utf-8
Subject: [RSS试用报告] {trial['name']} — 7天试用结束
From: RSS Trial Manager <no-reply@163.com>

<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#333;">

<h2 style="color:#1a1a2e;border-bottom:2px solid #e8e8e8;padding-bottom:8px;">
  📊 RSS 试用源 7 天报告
</h2>

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f8f9fa;border-radius:6px;padding:16px;margin-bottom:20px;">
  <tr>
    <td style="padding:4px 0;">
      <strong>源名称：</strong>{name}
    </td>
  </tr>
  <tr>
    <td style="padding:4px 0;">
      <strong>RSS URL：</strong>
      <a href="{url}" style="color:#0066cc;">{url}</a>
    </td>
  </tr>
  <tr>
    <td style="padding:4px 0;">
      <strong>发现评分：</strong>{score:.3f}
      （reliability / freshness / content_quality / authority / uniqueness）
    </td>
  </tr>
  <tr>
    <td style="padding:4px 0;">
      <strong>试用期：</strong>{start} → {end}（{TRIAL_DAYS} 天）
    </td>
  </tr>
</table>

<h3 style="color:#333;margin-top:0;">每日贡献统计</h3>
<table width="100%" cellpadding="0" cellspacing="0"
       style="border:1px solid #ddd;border-radius:4px;border-collapse:collapse;">
  <thead>
    <tr style="background:#1a1a2e;color:#fff;">
      <th style="padding:8px 12px;text-align:left;">日期</th>
      <th style="padding:8px 12px;text-align:center;">抓取文章数</th>
      <th style="padding:8px 12px;text-align:center;">入选摘要数</th>
    </tr>
  </thead>
  <tbody>{rows}
  </tbody>
  <tfoot>
    <tr style="background:#f0f0f0;font-weight:bold;">
      <td style="padding:8px 12px;">7 天合计</td>
      <td style="padding:8px 12px;text-align:center;">{total_fetched}</td>
      <td style="padding:8px 12px;text-align:center;">{total_selected}</td>
    </tr>
  </tfoot>
</table>

<div style="margin-top:24px;padding:16px;background:#fff8e1;border-left:4px solid #f9a825;border-radius:0 4px 4px 0;">
  <strong>📋 下一步操作</strong><br><br>
  请根据 7 天实际邮件体验判断是否保留此源：<br><br>
  <code style="background:#f0f0f0;padding:2px 6px;border-radius:3px;">
    python3 ~/global-news/rss-trial-manager.py keep
  </code>
  &nbsp;→&nbsp; 正式纳入（去掉试用标记）<br><br>
  <code style="background:#f0f0f0;padding:2px 6px;border-radius:3px;">
    python3 ~/global-news/rss-trial-manager.py remove
  </code>
  &nbsp;→&nbsp; 删除此源
</div>

<p style="color:#999;font-size:12px;margin-top:20px;">
  生成时间：{now_str}<br>
  RSS Trial Manager · global-news
</p>
</body>
</html>"""


def send_auto_decision_email(trial: dict, kept: bool, total_selected: int) -> bool:
    """Send auto-decision notification email (keep or remove)."""
    env = _load_env()
    smtp_user = env.get("SMTP_USER_163", env.get("SMTP_USER", ""))
    smtp_pass = env.get("SMTP_PASS_163", env.get("SMTP_PASS", ""))
    mail_to = env.get("REPORT_EMAIL", env.get("MAIL_TO", smtp_user))

    if not smtp_user or not smtp_pass:
        print("ERROR: SMTP credentials not found in env", file=sys.stderr)
        return False

    name = _html_escape(trial["name"])
    url = _html_escape(trial["url"])
    score = trial.get("candidate_score", 0)
    start = trial.get("start_date", "?")
    stats = trial.get("daily_stats", [])
    now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
    decision_color = "#2e7d32" if kept else "#c62828"
    decision_label = "✅ 自动保留" if kept else "❌ 自动移除"
    decision_reason = (
        f"7 天内共 {total_selected} 篇入选（门槛 ≥ {AUTO_KEEP_MIN_SELECTED}）"
        if kept else
        f"7 天内仅 {total_selected} 篇入选（门槛 ≥ {AUTO_KEEP_MIN_SELECTED}）"
    )

    rows = ""
    for d in stats:
        rows += (
            f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee;'>{_html_escape(d.get('date',''))}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:center;'>{d.get('fetched',0)}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:center;'>{d.get('selected',0)}</td></tr>"
        )
    total_fetched = sum(d.get("fetched", 0) for d in stats)

    html = f"""MIME-Version: 1.0
Content-Type: text/html; charset=utf-8
Subject: [RSS试用] {trial['name']} — {decision_label}
From: RSS Trial Manager <{smtp_user}>
To: {mail_to}

<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#333;">
<h2 style="border-bottom:2px solid #e8e8e8;padding-bottom:8px;">📊 RSS 试用结果</h2>
<div style="padding:12px 16px;background:{decision_color};color:#fff;border-radius:4px;font-size:16px;font-weight:bold;margin-bottom:20px;">
  {decision_label} — {name}
</div>
<p style="color:#555;">{decision_reason}</p>
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f9fa;border-radius:6px;padding:16px;margin-bottom:20px;">
  <tr><td style="padding:4px 0;"><strong>RSS URL：</strong><a href="{url}">{url}</a></td></tr>
  <tr><td style="padding:4px 0;"><strong>发现评分：</strong>{score:.3f}</td></tr>
  <tr><td style="padding:4px 0;"><strong>试用期：</strong>{start} → {_today()}（{TRIAL_DAYS} 天）</td></tr>
</table>
<h3>每日贡献统计</h3>
<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #ddd;border-collapse:collapse;">
  <thead><tr style="background:#1a1a2e;color:#fff;">
    <th style="padding:8px 12px;text-align:left;">日期</th>
    <th style="padding:8px 12px;text-align:center;">抓取</th>
    <th style="padding:8px 12px;text-align:center;">入选</th>
  </tr></thead>
  <tbody>{rows}</tbody>
  <tfoot><tr style="background:#f0f0f0;font-weight:bold;">
    <td style="padding:8px 12px;">合计</td>
    <td style="padding:8px 12px;text-align:center;">{total_fetched}</td>
    <td style="padding:8px 12px;text-align:center;">{total_selected}</td>
  </tr></tfoot>
</table>
<p style="color:#999;font-size:12px;margin-top:20px;">生成时间：{now_str} · RSS Trial Manager</p>
</body></html>"""

    fd, mail_file = tempfile.mkstemp(suffix=".eml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
        result = subprocess.run(
            ["curl", "--silent", "--ssl-reqd", "--max-time", "30",
             "--url", "smtps://smtp.163.com:465",
             "--user", f"{smtp_user}:{smtp_pass}",
             "--mail-from", smtp_user,
             "--mail-rcpt", mail_to,
             "--upload-file", mail_file],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode == 0:
            print(f"Auto-decision email sent to {mail_to}")
            return True
        print(f"Email failed: {result.stderr}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Email error: {e}", file=sys.stderr)
        return False
    finally:
        if os.path.exists(mail_file):
            os.unlink(mail_file)


def send_report_email(trial: dict) -> bool:
    env = _load_env()
    smtp_user = env.get("SMTP_USER_163", "")
    smtp_pass = env.get("SMTP_PASS_163", "")
    mail_to = env.get("REPORT_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        print("ERROR: SMTP credentials not found in env", file=sys.stderr)
        return False

    html = generate_report_html(trial)
    # Replace placeholder From header with real address
    html = html.replace("no-reply@163.com", smtp_user)
    # Inject To: header (RFC 2822 requires it; missing causes spam filtering)
    html = html.replace(f"From: RSS Trial Manager <{smtp_user}>",
                        f"From: RSS Trial Manager <{smtp_user}>\nTo: {mail_to}")

    fd, mail_file = tempfile.mkstemp(suffix=".eml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
        result = subprocess.run(
            [
                "curl", "--silent", "--ssl-reqd",
                "--max-time", "30",
                "--url", f"smtps://smtp.163.com:465",
                "--user", f"{smtp_user}:{smtp_pass}",
                "--mail-from", smtp_user,
                "--mail-rcpt", mail_to,
                "--upload-file", mail_file,
            ],
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
        if os.path.exists(mail_file):
            os.unlink(mail_file)


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_run() -> None:
    """Normal daily run: update stats, check expiry, promote next candidate."""
    state = load_state()
    today = _today()
    active = state.get("active_trial")

    if active:
        # --- Update today's stats ---
        stats = active.setdefault("daily_stats", [])
        existing_dates = {d["date"] for d in stats}
        if today not in existing_dates:
            day_stats = aggregate_today_stats(active["name"])
            stats.append(day_stats)
            print(f"[trial-manager] {active['name']}: day {len(stats)}/{TRIAL_DAYS}"
                  f" — fetched={day_stats['fetched']} selected={day_stats['selected']}")
        else:
            # Update existing entry for today (re-run scenario)
            day_stats = aggregate_today_stats(active["name"])
            for d in stats:
                if d["date"] == today:
                    d.update(day_stats)
                    break
            print(f"[trial-manager] {active['name']}: updated today's stats"
                  f" — fetched={day_stats['fetched']} selected={day_stats['selected']}")

        active["daily_stats"] = stats

        # --- Check if trial has run its course ---
        start = datetime.strptime(active["start_date"], "%Y-%m-%d").replace(tzinfo=BJT)
        elapsed_days = (datetime.now(BJT) - start).days
        if elapsed_days >= TRIAL_DAYS and not active.get("auto_decided"):
            total_selected = sum(d.get("selected", 0) for d in stats)
            kept = total_selected >= AUTO_KEEP_MIN_SELECTED
            if kept:
                graduate_trial_in_config(active["name"])
                active["outcome"] = "auto-graduated"
                print(f"[trial-manager] Auto-keep '{active['name']}' "
                      f"({total_selected} selected >= {AUTO_KEEP_MIN_SELECTED})")
            else:
                remove_trial_from_config(active["name"])
                active["outcome"] = "auto-removed"
                print(f"[trial-manager] Auto-remove '{active['name']}' "
                      f"({total_selected} selected < {AUTO_KEEP_MIN_SELECTED})")
            active["auto_decided"] = True
            active["end_date"] = today
            state.setdefault("history", []).append(active)
            state["active_trial"] = None
            save_state(state)
            send_auto_decision_email(active, kept, total_selected)
            return

        save_state(state)
        return

    # --- No active trial: promote next candidate ---
    candidates = get_promotable_candidates(state)
    if not candidates:
        print("[trial-manager] No promotable candidates (score >= "
              f"{PROMOTE_THRESHOLD}). Nothing to do.")
        return

    best = candidates[0]
    score = best.get("scores", {}).get("final", 0)
    print(f"[trial-manager] Promoting '{best['name']}' (score={score:.3f}) to trial...")

    add_trial_to_config(best)

    new_trial = {
        "name": best["name"],
        "url": best["url"],
        "category": best.get("category", ""),
        "language": best.get("language", "en"),
        "candidate_score": round(score, 3),
        "start_date": today,
        "end_date": None,
        "report_sent": False,
        "daily_stats": [],
    }
    state["active_trial"] = new_trial
    save_state(state)

    print(f"[trial-manager] '{best['name']}' added to news-sources-config.json "
          f"(trial=true). Trial runs until "
          f"{(datetime.now(BJT) + timedelta(days=TRIAL_DAYS)).strftime('%Y-%m-%d')}.")


def cmd_status() -> None:
    """Print current trial state."""
    state = load_state()
    active = state.get("active_trial")
    if not active:
        print("No active trial.")
        candidates = get_promotable_candidates(state)
        print(f"Next in queue: {len(candidates)} candidates with score >= {PROMOTE_THRESHOLD}")
        if candidates:
            best = candidates[0]
            print(f"  → '{best['name']}' score={best.get('scores',{}).get('final',0):.3f}")
        return

    today = _today()
    start = datetime.strptime(active["start_date"], "%Y-%m-%d").replace(tzinfo=BJT)
    elapsed = (datetime.now(BJT) - start).days
    stats = active.get("daily_stats", [])
    total_fetched = sum(d.get("fetched", 0) for d in stats)
    total_selected = sum(d.get("selected", 0) for d in stats)

    print(f"Active trial: {active['name']}")
    print(f"  URL:     {active['url']}")
    print(f"  Score:   {active['candidate_score']:.3f}")
    print(f"  Started: {active['start_date']}  (day {elapsed+1}/{TRIAL_DAYS})")
    print(f"  Stats:   {total_fetched} fetched, {total_selected} selected over {len(stats)} days")
    print(f"  Report:  {'sent' if active.get('report_sent') else 'pending'}")


def cmd_remove() -> None:
    """Remove active trial source from news config and close trial as rejected."""
    state = load_state()
    active = state.get("active_trial")
    if not active:
        print("No active trial to remove.")
        return

    removed = remove_trial_from_config(active["name"])
    if removed:
        print(f"Removed '{active['name']}' from news-sources-config.json.")
    else:
        print(f"WARNING: '{active['name']}' not found in config (may already be removed).")

    active["end_date"] = _today()
    active["outcome"] = "removed"
    state.setdefault("history", []).append(active)
    state["active_trial"] = None
    save_state(state)
    print(f"Trial closed as 'removed'. History updated.")


def cmd_keep() -> None:
    """Graduate active trial to permanent source (remove trial flag)."""
    state = load_state()
    active = state.get("active_trial")
    if not active:
        print("No active trial to graduate.")
        return

    graduated = graduate_trial_in_config(active["name"])
    if graduated:
        print(f"'{active['name']}' graduated to permanent source.")
    else:
        print(f"WARNING: '{active['name']}' trial flag not found in config.")

    active["end_date"] = _today()
    active["outcome"] = "graduated"
    state.setdefault("history", []).append(active)
    state["active_trial"] = None
    save_state(state)
    print("Trial closed as 'graduated'. Source is now permanent.")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        cmd_run()
    elif cmd == "status":
        cmd_status()
    elif cmd == "remove":
        cmd_remove()
    elif cmd == "keep":
        cmd_keep()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Usage: rss-trial-manager.py [run|status|remove|keep]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
