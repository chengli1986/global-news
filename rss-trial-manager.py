#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS Trial Manager — auto-promote high-scoring candidates into TRIAL_DAYS-day trials.

Daily flow (called from rss-source-discovery.sh after discovery):
  1. If active trial: aggregate today's stats from trial-source-log.jsonl
     - If 3 days elapsed → send 3-day report email → mark trial ended
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

SOURCES_FILE = os.path.join(SCRIPT_DIR, "news-sources-config.json")

import rss_registry as _reg
TRIAL_LOG_FILE = os.path.join(LOGS_DIR, "trial-source-log.jsonl")
HEALTH_STATE_FILE = os.path.join(LOGS_DIR, "rss-health.json")
ENV_FILE = os.path.expanduser("~/.stock-monitor.env")

PROMOTE_THRESHOLD = 0.90
TRIAL_DAYS = 3
AUTO_KEEP_MIN_SELECTED = 3  # auto-keep if ≥3 articles selected over 3-day trial (stricter signal/time ratio than old 5/7)
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


def _clear_health_state_for(source_name: str) -> None:
    """Remove a source's entry from rss-health.json.

    Called when a trial source is de-registered (auto-rejected or manually
    removed). Without this, stale `consecutive_fails` from the trial window
    would persist, and a future re-trial of the same name (or a monitoring
    dashboard query) would still see failures that belonged to a prior run.
    """
    if not os.path.isfile(HEALTH_STATE_FILE):
        return
    try:
        with open(HEALTH_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return
    if source_name in state:
        del state[source_name]
        try:
            _atomic_write(HEALTH_STATE_FILE, state)
        except Exception:
            pass


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
        _clear_health_state_for(source_name)
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

SCORE_DIMENSIONS = {
    "reliability": ("可靠性", "Feed 能否稳定访问、解析成功、文章数量充足（≥10 篇视为满分）"),
    "freshness": ("时效性", "最新文章距当前的时间，1.5h 内满分，超过 24h 得分趋近 0"),
    "content_quality": ("内容质量", "是否包含描述文本、作者、分类标签，描述越详细分越高"),
    "content_depth": ("内容深度", "描述文本平均长度，反映文章是否有实质内容而非标题党"),
    "authority": ("权威度", "基于来源知名度的固定评分，The Guardian 等主流媒体约 0.88"),
    "uniqueness": ("独特性", "与现有 40 个源在话题/地区/语言上的差异化程度，越互补越高"),
}


def _load_candidate_detail(url: str) -> dict:
    """Load candidate detail from rss-registry.json by URL."""
    try:
        registry = _reg.load_registry()
        return _reg.get_by_url(registry, url) or {}
    except Exception:
        return {}


def _build_score_rows(scores: dict) -> str:
    rows = ""
    dim_order = ["reliability", "freshness", "content_quality", "content_depth", "authority", "uniqueness"]
    for dim in dim_order:
        val = scores.get(dim)
        if val is None:
            continue
        label, explanation = SCORE_DIMENSIONS.get(dim, (dim, ""))
        bar_width = int(val * 80)
        bar_color = "#2e7d32" if val >= 0.9 else "#f57c00" if val >= 0.7 else "#c62828"
        rows += f"""
        <tr>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;width:90px;"><strong>{label}</strong></td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;width:50px;text-align:center;font-weight:bold;color:{bar_color};">{val:.2f}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">
            <div style="background:#eee;border-radius:3px;height:8px;margin-bottom:4px;">
              <div style="background:{bar_color};border-radius:3px;height:8px;width:{bar_width}px;"></div>
            </div>
            <span style="color:#666;font-size:12px;">{_html_escape(explanation)}</span>
          </td>
        </tr>"""
    return rows


def _build_stats_rows(stats: list, normal_daily_limit: int = 3) -> str:
    rows = ""
    for d in stats:
        date = _html_escape(d.get("date", ""))
        fetched = d.get("fetched", 0)
        selected = d.get("selected", 0)
        rate = f"{selected/fetched*100:.0f}%" if fetched > 0 else "—"
        anomaly = fetched > normal_daily_limit * 2
        note = " <span style='color:#f57c00;font-size:11px;'>*</span>" if anomaly else ""
        rows += (
            f"<tr>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;'>{date}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:center;'>{fetched}{note}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:center;'>{selected}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:center;color:#555;'>{rate}</td>"
            f"</tr>"
        )
    return rows


def generate_report_html(trial: dict) -> str:
    name = _html_escape(trial["name"])
    url = _html_escape(trial["url"])
    score = trial.get("candidate_score", 0)
    start = trial.get("start_date", "?")
    end = trial.get("end_date", "?")
    stats = trial.get("daily_stats", [])

    total_fetched = sum(d.get("fetched", 0) for d in stats)
    total_selected = sum(d.get("selected", 0) for d in stats)
    overall_rate = f"{total_selected/total_fetched*100:.0f}%" if total_fetched > 0 else "—"
    has_anomaly = any(d.get("fetched", 0) > 6 for d in stats)

    candidate = _load_candidate_detail(trial.get("url", ""))
    scores = candidate.get("scores", {})
    validation = candidate.get("validation", {})
    score_rows = _build_score_rows(scores) if scores else ""
    stats_rows = _build_stats_rows(stats)
    now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")

    validation_html = ""
    if validation:
        art_count = validation.get("article_count", "?")
        newest_h = validation.get("newest_age_hours")
        newest_h = newest_h if newest_h is not None else "?"
        avg_desc = validation.get("avg_description_length")
        avg_desc = avg_desc if avg_desc is not None else "?"
        newest_h_str = f"{newest_h:.1f}" if isinstance(newest_h, (int, float)) else "未知"
        avg_desc_str = f"{avg_desc:.0f}" if isinstance(avg_desc, (int, float)) else "未知"
        validation_html = f"""
<h3 style="color:#333;margin-top:24px;">发现时质量快照</h3>
<p style="color:#555;font-size:13px;margin-top:0;">以下数据为 RSS 源首次被发现时的实测结果，用于衡量其入选试用的客观依据。</p>
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f9fa;border-radius:6px;padding:12px 16px;">
  <tr>
    <td style="padding:4px 8px;"><strong>可用文章数：</strong>{art_count} 篇（RSS feed 当时可抓取的总量）</td>
  </tr>
  <tr>
    <td style="padding:4px 8px;"><strong>最新文章时效：</strong>{newest_h_str}h 前发布（越低说明更新越及时）</td>
  </tr>
  <tr>
    <td style="padding:4px 8px;"><strong>平均描述长度：</strong>{avg_desc_str} 字符（反映文章是否有实质摘要，而非空标题）</td>
  </tr>
</table>"""

    anomaly_note = ""
    if has_anomaly:
        anomaly_note = """
<p style="color:#f57c00;font-size:12px;margin-top:8px;">
  * 标注日当天抓取数显著偏高，可能是该天 cron 多次触发或 feed 发布了大量补发文章，不代表常态。
</p>"""

    score_section = ""
    if score_rows:
        score_section = f"""
<h3 style="color:#333;margin-top:24px;">发现评分解析（综合 {score:.3f}）</h3>
<p style="color:#555;font-size:13px;margin-top:0;">
  发现评分由 RSS 源发现系统在首次检测时自动计算，决定是否进入试用队列（门槛 ≥ 0.90）。
</p>
<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #ddd;border-radius:4px;border-collapse:collapse;">
  <thead>
    <tr style="background:#1a1a2e;color:#fff;">
      <th style="padding:8px 10px;text-align:left;">维度</th>
      <th style="padding:8px 10px;text-align:center;">得分</th>
      <th style="padding:8px 10px;text-align:left;">含义</th>
    </tr>
  </thead>
  <tbody>{score_rows}
  </tbody>
</table>"""

    return f"""MIME-Version: 1.0
Content-Type: text/html; charset=utf-8
Subject: [RSS试用报告] {trial['name']} — {TRIAL_DAYS}天试用结束
From: RSS Trial Manager <no-reply@163.com>

<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:20px;color:#333;">

<h2 style="color:#1a1a2e;border-bottom:2px solid #e8e8e8;padding-bottom:8px;">
  📊 RSS 试用源 {TRIAL_DAYS} 天报告
</h2>

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f8f9fa;border-radius:6px;padding:16px;margin-bottom:20px;">
  <tr><td style="padding:4px 0;"><strong>源名称：</strong>{name}</td></tr>
  <tr><td style="padding:4px 0;"><strong>RSS URL：</strong>
    <a href="{url}" style="color:#0066cc;">{url}</a></td></tr>
  <tr><td style="padding:4px 0;"><strong>类别：</strong>{_html_escape(trial.get("category","?"))} / {_html_escape(trial.get("language","?"))}</td></tr>
  <tr><td style="padding:4px 0;"><strong>试用期：</strong>{start} → {end}（{TRIAL_DAYS} 天）</td></tr>
</table>

<h3 style="color:#333;margin-top:0;">{TRIAL_DAYS} 天贡献统计</h3>
<p style="color:#555;font-size:13px;margin-top:0;">
  <strong>抓取数</strong>：每次 news 发送时（每日 3 次）从该源拉取的文章数，配置上限为 3 篇/次。<br>
  <strong>入选数</strong>：通过 LLM 分类过滤、去重、配额竞争后真正出现在摘要邮件中的文章数。<br>
  入选率反映该源在竞争 100 篇文章配额时的实际贡献价值，并非越高越好，而是要稳定。
</p>
<table width="100%" cellpadding="0" cellspacing="0"
       style="border:1px solid #ddd;border-radius:4px;border-collapse:collapse;">
  <thead>
    <tr style="background:#1a1a2e;color:#fff;">
      <th style="padding:8px 12px;text-align:left;">日期</th>
      <th style="padding:8px 12px;text-align:center;">抓取数</th>
      <th style="padding:8px 12px;text-align:center;">入选数</th>
      <th style="padding:8px 12px;text-align:center;">入选率</th>
    </tr>
  </thead>
  <tbody>{stats_rows}
  </tbody>
  <tfoot>
    <tr style="background:#f0f0f0;font-weight:bold;">
      <td style="padding:8px 12px;">{TRIAL_DAYS} 天合计</td>
      <td style="padding:8px 12px;text-align:center;">{total_fetched}</td>
      <td style="padding:8px 12px;text-align:center;">{total_selected}</td>
      <td style="padding:8px 12px;text-align:center;">{overall_rate}</td>
    </tr>
  </tfoot>
</table>
{anomaly_note}

{validation_html}

{score_section}

<div style="margin-top:24px;padding:16px;background:#fff8e1;border-left:4px solid #f9a825;border-radius:0 4px 4px 0;">
  <strong>📋 下一步操作</strong><br><br>
  请根据 {TRIAL_DAYS} 天实际邮件体验判断是否保留此源：<br><br>
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
    total_fetched = sum(d.get("fetched", 0) for d in stats)
    overall_rate = f"{total_selected/total_fetched*100:.0f}%" if total_fetched > 0 else "—"
    now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
    decision_color = "#2e7d32" if kept else "#c62828"
    decision_label = "✅ 自动保留" if kept else "❌ 自动移除"

    # Plain-language explanation of the decision
    if kept:
        decision_reason = (
            f"{TRIAL_DAYS} 天内共 <strong>{total_selected}</strong> 篇文章成功入选摘要邮件，"
            f"超过保留门槛（≥ {AUTO_KEEP_MIN_SELECTED} 篇）。"
            f"说明该源在与其他 40 个源的配额竞争中持续有贡献，内容质量达标。"
        )
    else:
        decision_reason = (
            f"{TRIAL_DAYS} 天内仅 <strong>{total_selected}</strong> 篇文章入选摘要邮件，"
            f"低于保留门槛（≥ {AUTO_KEEP_MIN_SELECTED} 篇）。"
            f"说明该源内容与现有源重叠度高，或质量未达 LLM 分类标准，贡献不足。"
        )

    # Score breakdown
    candidate = _load_candidate_detail(trial.get("url", ""))
    scores = candidate.get("scores", {})
    score_rows = _build_score_rows(scores) if scores else ""

    score_section = ""
    if score_rows:
        score_section = f"""
<h3 style="color:#333;margin-top:24px;">发现评分解析（综合 {score:.3f}）</h3>
<p style="color:#555;font-size:13px;margin-top:0;">
  发现评分决定了该源是否有资格进入试用队列（门槛 ≥ 0.90）。
  综合评分高并不等于实际贡献大——试用期才是真实检验。
</p>
<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #ddd;border-radius:4px;border-collapse:collapse;">
  <thead>
    <tr style="background:#1a1a2e;color:#fff;">
      <th style="padding:8px 10px;text-align:left;">维度</th>
      <th style="padding:8px 10px;text-align:center;">得分</th>
      <th style="padding:8px 10px;text-align:left;">含义</th>
    </tr>
  </thead>
  <tbody>{score_rows}</tbody>
</table>"""

    stats_rows = _build_stats_rows(stats)
    has_anomaly = any(d.get("fetched", 0) > 6 for d in stats)
    anomaly_note = ""
    if has_anomaly:
        anomaly_note = (
            "<p style='color:#f57c00;font-size:12px;margin-top:6px;'>"
            "* 标注日抓取数偏高，可能是当天 cron 多次触发或 feed 集中补发，不代表常态。</p>"
        )

    html = f"""MIME-Version: 1.0
Content-Type: text/html; charset=utf-8
Subject: [RSS试用] {trial['name']} — {decision_label}
From: RSS Trial Manager <{smtp_user}>
To: {mail_to}

<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:20px;color:#333;">

<h2 style="border-bottom:2px solid #e8e8e8;padding-bottom:8px;">📊 RSS {TRIAL_DAYS} 天试用结果</h2>

<div style="padding:14px 16px;background:{decision_color};color:#fff;border-radius:4px;font-size:17px;font-weight:bold;margin-bottom:16px;">
  {decision_label} — {name}
</div>

<p style="color:#444;line-height:1.7;margin-bottom:20px;">{decision_reason}</p>

<table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f9fa;border-radius:6px;padding:14px 16px;margin-bottom:20px;">
  <tr><td style="padding:4px 0;"><strong>RSS URL：</strong><a href="{url}" style="color:#0066cc;">{url}</a></td></tr>
  <tr><td style="padding:4px 0;"><strong>发现评分：</strong>{score:.3f}（发现系统综合打分，≥ 0.90 才进入试用队列）</td></tr>
  <tr><td style="padding:4px 0;"><strong>类别：</strong>{_html_escape(trial.get("category","?"))} / {_html_escape(trial.get("language","?"))}</td></tr>
  <tr><td style="padding:4px 0;"><strong>试用期：</strong>{start} → {_today()}（{TRIAL_DAYS} 天）</td></tr>
</table>

<h3 style="margin-bottom:4px;">{TRIAL_DAYS} 天贡献统计</h3>
<p style="color:#555;font-size:13px;margin-top:0;">
  <strong>抓取数</strong>：每次 news 发送（每日 3 次）从该源拉取的文章数，每次上限 3 篇。<br>
  <strong>入选数</strong>：经 LLM 分类 + 去重 + 地区配额竞争后真正出现在邮件中的文章数。<br>
  入选率衡量该源在 40 个源中的实际竞争力，稳定且高于 50% 是理想状态。
</p>
<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #ddd;border-collapse:collapse;">
  <thead><tr style="background:#1a1a2e;color:#fff;">
    <th style="padding:8px 12px;text-align:left;">日期</th>
    <th style="padding:8px 12px;text-align:center;">抓取数</th>
    <th style="padding:8px 12px;text-align:center;">入选数</th>
    <th style="padding:8px 12px;text-align:center;">入选率</th>
  </tr></thead>
  <tbody>{stats_rows}</tbody>
  <tfoot><tr style="background:#f0f0f0;font-weight:bold;">
    <td style="padding:8px 12px;">{TRIAL_DAYS} 天合计</td>
    <td style="padding:8px 12px;text-align:center;">{total_fetched}</td>
    <td style="padding:8px 12px;text-align:center;">{total_selected}</td>
    <td style="padding:8px 12px;text-align:center;">{overall_rate}</td>
  </tr></tfoot>
</table>
{anomaly_note}

{score_section}

<p style="color:#999;font-size:12px;margin-top:24px;">生成时间：{now_str} · RSS Trial Manager · global-news</p>
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
    registry = _reg.load_registry()
    today = _today()
    active = _reg.get_active_trial(registry)

    if active:
        # Update today's stats
        day_stats = aggregate_today_stats(active["name"])
        _reg.update_trial_stats(registry, active["name"], day_stats)
        _reg.save_registry(registry)
        active = _reg.get_active_trial(registry)  # re-read after update

        trial = active["trial"]
        stats = trial.get("daily_stats", [])
        print(f"[trial-manager] {active['name']}: day {len(stats)}/{TRIAL_DAYS}"
              f" — fetched={day_stats['fetched']} selected={day_stats['selected']}")

        # Check if trial has run its course
        start = datetime.strptime(trial["start_date"], "%Y-%m-%d").replace(tzinfo=BJT)
        elapsed_days = (datetime.now(BJT) - start).days
        if elapsed_days >= TRIAL_DAYS and not trial.get("auto_decided"):
            total_selected = sum(d.get("selected", 0) for d in stats)
            kept = total_selected >= AUTO_KEEP_MIN_SELECTED
            outcome = "auto-graduated" if kept else "auto-removed"
            if kept:
                graduate_trial_in_config(active["name"])
                _reg.set_production_config(registry, active["name"], keywords=[], limit=3)
                print(f"[trial-manager] Auto-keep '{active['name']}' "
                      f"({total_selected} selected >= {AUTO_KEEP_MIN_SELECTED})")
            else:
                remove_trial_from_config(active["name"])
                print(f"[trial-manager] Auto-remove '{active['name']}' "
                      f"({total_selected} selected < {AUTO_KEEP_MIN_SELECTED})")
            _reg.end_trial(registry, active["name"], outcome=outcome, kept=kept, today=today)
            _reg.save_registry(registry)
            send_auto_decision_email(active, kept, total_selected)
        return

    # No active trial: promote next candidate
    candidates = _reg.get_promotable(registry, PROMOTE_THRESHOLD)
    if not candidates:
        print(f"[trial-manager] No promotable candidates (score >= {PROMOTE_THRESHOLD}). Nothing to do.")
        return

    best = candidates[0]
    score = (best.get("scores") or {}).get("final", 0)
    print(f"[trial-manager] Promoting '{best['name']}' (score={score:.3f}) to trial...")

    add_trial_to_config(best)
    _reg.start_trial(registry, best, today)
    _reg.save_registry(registry)

    print(f"[trial-manager] '{best['name']}' added to news-sources-config.json "
          f"(trial=true). Trial runs until "
          f"{(datetime.now(BJT) + timedelta(days=TRIAL_DAYS)).strftime('%Y-%m-%d')}.")


def cmd_status() -> None:
    """Print current trial state."""
    registry = _reg.load_registry()
    active = _reg.get_active_trial(registry)
    if not active:
        print("No active trial.")
        candidates = _reg.get_promotable(registry, PROMOTE_THRESHOLD)
        print(f"Next in queue: {len(candidates)} candidates with score >= {PROMOTE_THRESHOLD}")
        if candidates:
            best = candidates[0]
            print(f"  → '{best['name']}' score={(best.get('scores') or {}).get('final', 0):.3f}")
        return

    trial = active["trial"]
    start = datetime.strptime(trial["start_date"], "%Y-%m-%d").replace(tzinfo=BJT)
    elapsed = (datetime.now(BJT) - start).days
    stats = trial.get("daily_stats", [])
    total_fetched = sum(d.get("fetched", 0) for d in stats)
    total_selected = sum(d.get("selected", 0) for d in stats)

    print(f"Active trial: {active['name']}")
    print(f"  URL:     {active['url']}")
    print(f"  Score:   {trial['candidate_score']:.3f}")
    print(f"  Started: {trial['start_date']}  (day {elapsed+1}/{TRIAL_DAYS})")
    print(f"  Stats:   {total_fetched} fetched, {total_selected} selected over {len(stats)} days")
    print(f"  Report:  {'sent' if trial.get('report_sent') else 'pending'}")


def cmd_remove() -> None:
    """Remove active trial source from news config and close trial as rejected."""
    registry = _reg.load_registry()
    active = _reg.get_active_trial(registry)
    if not active:
        print("No active trial to remove.")
        return

    removed = remove_trial_from_config(active["name"])
    if removed:
        print(f"Removed '{active['name']}' from news-sources-config.json.")
    else:
        print(f"WARNING: '{active['name']}' not found in config (may already be removed).")

    _reg.end_trial(registry, active["name"], outcome="removed", kept=False, today=_today(), auto_decided=False)
    _reg.save_registry(registry)
    print("Trial closed as 'removed'. Registry updated.")


def cmd_keep() -> None:
    """Graduate active trial to permanent source (remove trial flag)."""
    registry = _reg.load_registry()
    active = _reg.get_active_trial(registry)
    if not active:
        print("No active trial to graduate.")
        return

    graduated = graduate_trial_in_config(active["name"])
    if graduated:
        print(f"'{active['name']}' graduated to permanent source.")
    else:
        print(f"WARNING: '{active['name']}' trial flag not found in config.")

    _reg.end_trial(registry, active["name"], outcome="graduated", kept=True, today=_today(), auto_decided=False)
    _reg.set_production_config(registry, active["name"], keywords=[], limit=3)
    _reg.save_registry(registry)
    print("Trial closed as 'graduated'. Source is now permanent.")


def cmd_retry() -> None:
    """Reset a previously auto-removed trial source back to 'discovered' so it
    can be re-trialled.

    Use case: a source was auto-rejected because its feed was down during the
    trial window, or because the 4-stage classifier happened to drop all its
    articles into quota-trimmed regions. Once the transient cause is resolved,
    `retry <name>` clears the trial metadata and makes the source eligible for
    promotion again at the next discovery run.

    Only sources with trial.outcome == 'auto-removed' can be retried. Sources
    rejected for pool-cap or duplicate_publisher reasons are discovery-level
    decisions, not trial-level, and shouldn't use this path.

    Usage: rss-trial-manager.py retry "<source name>"
    """
    if len(sys.argv) < 3:
        print("Usage: rss-trial-manager.py retry \"<source name>\"", file=sys.stderr)
        sys.exit(1)
    name = sys.argv[2]

    # Re-read REGISTRY_FILE from the module at call time so tests can patch it.
    registry_path = _reg.REGISTRY_FILE
    registry = _reg.load_registry(registry_path)
    target = next(
        (s for s in _reg.get_sources(registry) if s.get("name") == name),
        None,
    )
    if target is None:
        print(f"ERROR: source '{name}' not found in registry.", file=sys.stderr)
        sys.exit(1)

    prior_trial = target.get("trial") or {}
    if target.get("status") != "rejected" or prior_trial.get("outcome") != "auto-removed":
        print(f"ERROR: '{name}' is not retry-eligible "
              f"(status={target.get('status')}, outcome={prior_trial.get('outcome')}). "
              f"Only auto-removed trials can be retried.", file=sys.stderr)
        sys.exit(1)

    # Preserve the old trial run as history on the source for audit trail
    history = target.setdefault("trial_history", [])
    history.append(prior_trial)

    # Reset for re-entry into the trial queue
    target["status"] = "discovered"
    target["trial"] = None
    target.pop("reject_reason", None)

    _reg.save_registry(registry, registry_path)
    print(f"Reset '{name}' → status=discovered, trial cleared ({len(history)} prior trial run(s) archived).")
    print("It will be re-considered at the next rss-trial-manager run "
          "if its score remains ≥ PROMOTE_THRESHOLD.")


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
    elif cmd == "retry":
        cmd_retry()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Usage: rss-trial-manager.py [run|status|remove|keep|retry <name>]",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
