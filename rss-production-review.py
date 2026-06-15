#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RSS Production 源在岗质量复查 — 读 telemetry，判 A 僵尸/B 变质，发邮件报告。

仅生成报告，不执行 demote（demote 由 rss-demote-source.py 人工确认后执行）。
Spec: docs/superpowers/specs/2026-06-13-rss-production-quality-review-design.md
"""
import json
import os
import sys
import base64
import subprocess
import tempfile
import statistics
from datetime import datetime, timezone, timedelta

import rss_registry as _reg

BJT = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "logs", "production-source-log.jsonl")
ENV_FILE = os.path.expanduser("~/.stock-monitor.env")
SENDER_FILE = os.path.join(SCRIPT_DIR, "unified-global-news-sender.py")
PLAN_C_CATEGORIES = ("healthcare", "vertical", "global_south")  # categories with no dedicated board until 方案 C
ROTATION_MIN_GROUP = 3
ROTATION_WINDOW_DAYS = 30
ROTATION_MIN_ACTIVE_DAYS = 7
ROTATION_GRACE_DAYS = 30


def parse_ts(ts: str) -> datetime:
    """Parse a telemetry ISO timestamp (carries +08:00 offset)."""
    return datetime.fromisoformat(ts)


def load_records(log_path: str) -> list:
    """Read JSONL, skipping blank/malformed lines. Bare rows (no metadata) kept as-is."""
    out = []
    if not os.path.isfile(log_path):
        return out
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and d.get("source"):
                out.append(d)
    return out


def filter_window(records: list, now: datetime, days: int) -> list:
    """Keep records with ts >= now - days."""
    cutoff = now - timedelta(days=days)
    kept = []
    for r in records:
        try:
            if parse_ts(r["ts"]) >= cutoff:
                kept.append(r)
        except (KeyError, ValueError):
            continue
    return kept


def aggregate_by_source(records: list) -> dict:
    """Sum fetched/selected per source; active_days = distinct dates with fetched>0."""
    agg = {}
    days_seen = {}
    for r in records:
        src = r.get("source")
        if not src:
            continue
        a = agg.setdefault(src, {"fetched": 0, "selected": 0, "active_days": 0})
        a["fetched"] += int(r.get("fetched", 0) or 0)
        a["selected"] += int(r.get("selected", 0) or 0)
        if int(r.get("fetched", 0) or 0) > 0:
            days_seen.setdefault(src, set()).add(r.get("ts", "")[:10])
    for src, dates in days_seen.items():
        agg[src]["active_days"] = len(dates)
    return agg


def graduation_date(source: dict):
    """Return date a source graduated from trial, or None for legacy/non-trial sources."""
    t = source.get("trial")
    if isinstance(t, dict) and t.get("outcome") in ("graduated", "auto-graduated") and t.get("end_date"):
        try:
            return datetime.strptime(t["end_date"], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def tenure_days(source: dict, now: datetime):
    """Days since graduation; None if legacy (no graduation date → treated as long-tenured)."""
    g = graduation_date(source)
    if g is None:
        return None
    return (now.date() - g).days


def find_zombies(registry, records, now, *, window_days=30, grace_days=30,
                 min_active_days=7, max_selected=1) -> list:
    """A: production sources still publishing (fetched>0) but ~never selected.

    Skips: non-production, in-grace (tenure<grace_days), insufficient sample
    (active_days<min_active_days), and dead feeds (fetched==0 → health-check's job).
    """
    windowed = filter_window(records, now, window_days)
    agg = aggregate_by_source(windowed)
    zombies = []
    for s in _reg.get_by_status(registry, "production"):
        name = s.get("name")
        a = agg.get(name)
        if not a or a["fetched"] <= 0:            # dead/never-seen → not a zombie
            continue
        t = tenure_days(s, now)
        if t is not None and t < grace_days:       # in grace
            continue
        if a["active_days"] < min_active_days:      # insufficient sample (low-freq safety)
            continue
        if a["selected"] <= max_selected:
            zombies.append({
                "name": name,
                "category": s.get("category", "?"),
                "fetched": a["fetched"],
                "selected": a["selected"],
                "tenure_days": t,
            })
    return zombies


def median_or_none(xs: list):
    vals = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(vals) if vals else None


def _meta_series(records, source, field):
    return [r[field] for r in records if r.get("source") == source and field in r]


def find_degraded(registry, records, now, *, recent_days=7, baseline_days=60,
                  min_baseline=10, min_recent=5) -> list:
    """B: content-quality drift vs the source's OWN baseline (never absolute thresholds).

    recent = last recent_days; baseline = records in [now-baseline_days, now-recent_days).
    Capping the baseline at baseline_days stops ancient pre-degradation history from
    firing a warning long after the change has stabilised as the new normal. Warning only.
    """
    windowed = filter_window(records, now, baseline_days)
    cutoff = now - timedelta(days=recent_days)
    baseline_recs, recent_recs = [], []
    for r in windowed:
        try:
            ts = parse_ts(r["ts"])
        except (KeyError, ValueError):
            continue
        (recent_recs if ts >= cutoff else baseline_recs).append(r)

    out = []
    for s in _reg.get_by_status(registry, "production"):
        name = s.get("name")
        for field, check, label in (
            ("pct_with_desc",
             lambda b, r: b is not None and r is not None and b > 0.8 and r < 0.3,
             "desc-collapse"),
            ("avg_desc_len",
             lambda b, r: b is not None and r is not None and b > 0 and r < b * 0.4,
             "desc-len-shrink"),
            ("pct_with_author",
             lambda b, r: b is not None and r is not None and b > 0.5 and r < b * 0.5,
             "author-drop"),
        ):
            b_series = _meta_series(baseline_recs, name, field)
            r_series = _meta_series(recent_recs, name, field)
            if len(b_series) < min_baseline or len(r_series) < min_recent:
                continue
            b, r = median_or_none(b_series), median_or_none(r_series)
            if check(b, r):
                out.append({"name": name, "signal": field + ":" + label,
                            "baseline": round(b, 2), "recent": round(r, 2),
                            "detail": f"{field} {b:.2f} → {r:.2f}"})
    return out


def find_rotation_candidates(registry, records, now, *, window_days=30,
                             min_group=3, min_active_days=7, grace_days=30,
                             zombie_max=1) -> list:
    """组内实测优胜劣汰：每个 category 内 selected 垫底且明显低于同类的源 → 建议轮换。

    保多元：legacy(无 category)豁免；组内有数据源 <= min_group 整组豁免；每组最多标 1 个。
    去重：selected <= zombie_max 的归 A 僵尸，不在此重复。低频保护沿用 active_days/在岗宽限。
    """
    import collections
    agg = aggregate_by_source(filter_window(records, now, window_days))
    by_cat = collections.defaultdict(list)
    for s in _reg.get_by_status(registry, "production"):
        if s.get("category"):                      # legacy(无 category)豁免
            by_cat[s["category"]].append(s)

    out = []
    for cat in sorted(by_cat):
        live = [(s, agg[s["name"]]) for s in by_cat[cat]
                if agg.get(s["name"], {}).get("fetched", 0) > 0]
        if len(live) <= min_group:                 # 领域保底：组太小豁免
            continue
        median = statistics.median(sorted(a["selected"] for _, a in live))
        s, a = min(live, key=lambda x: x[1]["selected"])   # 组内最低
        sel, ad = a["selected"], a["active_days"]
        if sel <= zombie_max:                      # 归 A 僵尸，不重复
            continue
        if ad < min_active_days:                   # 低频样本保护
            continue
        t = tenure_days(s, now)
        if t is not None and t < grace_days:       # 在岗宽限
            continue
        if sel < median / 2:                       # 明显低于同类
            out.append({"name": s["name"], "category": cat, "selected": sel,
                        "group_median": median, "group_size": len(live),
                        "tenure_days": t})
    return out


def snapshot_rows(registry, records, now, *, window_days=30) -> list:
    """All production sources' 30d fetched/selected, for transparency in the report."""
    agg = aggregate_by_source(filter_window(records, now, window_days))
    rows = []
    for s in _reg.get_by_status(registry, "production"):
        a = agg.get(s.get("name"), {"fetched": 0, "selected": 0})
        rows.append({"name": s.get("name"), "category": s.get("category", "?"),
                     "fetched": a["fetched"], "selected": a["selected"]})
    rows.sort(key=lambda r: r["selected"])
    return rows


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_report_html(zombies, degraded, snapshot, now, plan_c_html="") -> str:
    """Full HTML report: A zombie candidates (with demote command), B warnings, pool snapshot."""
    ts = now.strftime("%Y-%m-%d %H:%M BJT")

    if zombies:
        z_rows = "".join(
            f"<tr><td>{_esc(z['name'])}</td><td>{_esc(z['category'])}</td>"
            f"<td style='text-align:center'>{z['fetched']}</td>"
            f"<td style='text-align:center'>{z['selected']}</td>"
            f"<td style='text-align:center'>{z['tenure_days'] if z['tenure_days'] is not None else 'legacy'}</td>"
            f"<td><code>python3 ~/global-news/rss-demote-source.py --name \"{_esc(z['name'])}\" "
            f"--reason \"zombie-30d-no-selected\"</code></td></tr>"
            for z in zombies)
        a_section = (f"<h3>🧟 A — 僵尸源候选（{len(zombies)}）建议 demote（确认后执行）</h3>"
                     "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                     "<tr style='background:#f3f4f6'><th>源</th><th>类别</th><th>30d 抓取</th>"
                     "<th>30d 入选</th><th>在岗天</th><th>确认后执行</th></tr>"
                     f"{z_rows}</table>")
    else:
        a_section = "<h3>🧟 A — 僵尸源候选</h3><p>无。</p>"

    if degraded:
        d_rows = "".join(
            f"<tr><td>{_esc(d['name'])}</td><td>{_esc(d['signal'])}</td>"
            f"<td style='text-align:center'>{_esc(d['detail'])}</td></tr>" for d in degraded)
        b_section = (f"<h3>⚠️ B — 内容变质预警（{len(degraded)}）仅供人工判断</h3>"
                     "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                     "<tr style='background:#fff8e1'><th>源</th><th>信号</th><th>基线 → 近期</th></tr>"
                     f"{d_rows}</table>")
    else:
        b_section = "<h3>⚠️ B — 内容变质预警</h3><p>无。</p>"

    snap_rows = "".join(
        f"<tr><td>{_esc(r['name'])}</td><td>{_esc(r['category'])}</td>"
        f"<td style='text-align:center'>{r['fetched']}</td>"
        f"<td style='text-align:center'>{r['selected']}</td></tr>" for r in snapshot)
    snap_section = ("<h3>📊 全池 30 天贡献快照</h3>"
                    "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                    "<tr style='background:#f3f4f6'><th>源</th><th>类别</th><th>30d 抓取</th><th>30d 入选</th></tr>"
                    f"{snap_rows}</table>")

    return (f"<h2>RSS Production 源在岗质量复查</h2><p>生成：{ts}</p>"
            f"{plan_c_html}{a_section}{b_section}{snap_section}")


def _plan_c_done(sender_path: str = SENDER_FILE) -> bool:
    """方案 C 是否已做：sender 里出现了科学/健康专属板块常量。"""
    try:
        with open(sender_path, encoding="utf-8") as f:
            return "REGION_SCI_HEALTH" in f.read()
    except Exception:
        return False


def plan_c_reminder_html(registry, records, now, *, window_days=30,
                         sender_path: str = SENDER_FILE) -> str:
    """周报里的"方案 C 待办"提醒条；C 做了（或无相关源）就返回空。

    展示暂无专属板块的 category（healthcare/vertical/global_south）源的 30d 入选量，
    帮判断它们够不够分量做方案 C。检测到 sender 出现 REGION_SCI_HEALTH 即自动消失。
    """
    if _plan_c_done(sender_path):
        return ""
    cat_of = {s.get("name"): s.get("category") for s in _reg.get_sources(registry)}
    agg = aggregate_by_source(filter_window(records, now, window_days))
    rows = [(n, cat_of.get(n), a["selected"]) for n, a in agg.items()
            if cat_of.get(n) in PLAN_C_CATEGORIES]
    if not rows:
        return ""
    rows.sort(key=lambda r: -r[2])
    total = sum(r[2] for r in rows)
    items = "；".join(f"{_esc(n)}({_esc(c)} {s}篇)" for n, c, s in rows[:8])
    return (
        "<div style='margin-top:16px;padding:12px 16px;background:#fff8e1;"
        "border-left:4px solid #f9a825;font-size:13px;line-height:1.6;'>"
        f"<strong>⏳ 方案 C 待办</strong>：healthcare/vertical/global_south 源暂无专属板块，"
        f"近 30 天共 <strong>{total}</strong> 篇入选、散在现有板块（如社会观察）。"
        "若想给它们建「科学/健康」「深度/专题」专属板块，扩 LLM topic 即可（见 spec §6）。"
        f"<br>相关源：{items}。"
        "<br><span style='color:#999;font-size:11px;'>做了方案 C（sender 出现 REGION_SCI_HEALTH）后此提醒自动消失。</span>"
        "</div>"
    )


def _load_env(path: str = ENV_FILE) -> dict:
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def send_report_email(html: str, subject: str, env_path: str = ENV_FILE) -> bool:
    """Send the HTML report via curl SMTP (same pattern as discovery)."""
    env = _load_env(env_path)
    mail_to = env.get("MAIL_TO", "")
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")
    if not all([mail_to, smtp_user, smtp_pass]):
        print("Missing SMTP credentials", file=sys.stderr)
        return False
    subject_b64 = base64.b64encode(subject.encode("utf-8")).decode("ascii")
    msg_id = f"<rss-prod-review-{datetime.now(BJT).strftime('%Y%m%d%H%M%S')}-{os.getpid()}@ec2.sinostor.com.cn>"
    content = (f'From: "RSS Pool Review" <{smtp_user}>\r\n'
               f"To: {mail_to}\r\nMessage-ID: {msg_id}\r\n"
               f"Subject: =?UTF-8?B?{subject_b64}?=\r\n"
               f"Content-Type: text/html; charset=UTF-8\r\nMIME-Version: 1.0\r\n\r\n{html}")
    fd, mail_file = tempfile.mkstemp(suffix=".eml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        r = subprocess.run(
            ["curl", "--silent", "--ssl-reqd", "--max-time", "30",
             "--url", f"smtps://{env.get('SMTP_SERVER', 'smtp.163.com')}:{env.get('SMTP_PORT', '465')}",
             "--user", f"{smtp_user}:{smtp_pass}", "--mail-from", smtp_user,
             "--mail-rcpt", mail_to, "--upload-file", mail_file],
            capture_output=True, text=True, timeout=45)
        if r.returncode == 0:
            print(f"Report email sent to {mail_to}")
            return True
        print(f"Email send failed: {r.stderr}", file=sys.stderr)
        return False
    finally:
        if os.path.exists(mail_file):
            os.unlink(mail_file)


def cmd_run(registry_path=None, log_path: str = LOG_PATH, now=None, send: bool = True) -> int:
    registry = _reg.load_registry(registry_path)
    if now is None:
        now = datetime.now(BJT)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=BJT)   # guard: aware-vs-naive compare would raise in filter_window
    records = load_records(log_path)
    zombies = find_zombies(registry, records, now)
    degraded = find_degraded(registry, records, now)
    snapshot = snapshot_rows(registry, records, now)
    plan_c_html = plan_c_reminder_html(registry, records, now)
    html = build_report_html(zombies, degraded, snapshot, now, plan_c_html)
    subject = (f"[RSS Pool 复查] {len(zombies)} 僵尸候选 / {len(degraded)} 变质预警 "
               f"— {now.strftime('%m月%d日')}")
    print(f"[prod-review] {len(zombies)} zombies, {len(degraded)} degraded, "
          f"{len(snapshot)} sources reviewed.")
    if send:
        if not send_report_email(html, subject):
            return 1
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        return cmd_run()
    print(f"Usage: {os.path.basename(__file__)} run", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
