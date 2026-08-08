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
import functools
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

# 复活探测：这些 reject_reason 关键词代表"质量不行被汰"，恢复了也不该回来。
# 用排除法而非白名单——将来出现新的技术性下线原因（如 dns-fail）会自动纳入探测。
REVIVAL_QUALITY_MARKERS = ("pool-cap", "rotation-group-laggard", "zombie",
                           "duplicate", "auto-removed")
HEALTH_CHECK_FILE = os.path.join(SCRIPT_DIR, "rss-health-check.py")
REVIVAL_MAX_AGE_HOURS = 24 * 365  # 绕过 staleness 判定：复活探测只关心"解析得出文章"


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
    """组内实测优胜劣汰：每个 category 内**入选率**垫底且明显低于同类的源 → 建议轮换。

    口径为入选率(selected/fetched)而非绝对入选数：`fetched` 由 news-sources-config
    的 per-source `limit` 配额决定（limit=3 → 30d 抓 ~99 篇，limit=6 → ~198），
    绝对入选数的天花板被配额锁死，按绝对数比中位会系统性误判小配额源垫底
    （2026-07-26 IEEE Spectrum 误报：44% 入选率却因 limit=3 被点名）。

    保多元：legacy(无 category)豁免；组内有数据源 <= min_group 整组豁免；每组最多标 1 个。
    去重：selected <= zombie_max 的归 A 僵尸，不在此重复。低频保护沿用 active_days/在岗宽限。
    """
    import collections
    agg = aggregate_by_source(filter_window(records, now, window_days))
    by_cat = collections.defaultdict(list)
    for s in _reg.get_by_status(registry, "production"):
        if s.get("category"):                      # legacy(无 category)豁免
            by_cat[s["category"]].append(s)

    def _rate(a: dict) -> float:
        return a["selected"] / a["fetched"] if a["fetched"] > 0 else 0.0

    out = []
    for cat in sorted(by_cat):
        live = [(s, agg[s["name"]]) for s in by_cat[cat]
                if agg.get(s["name"], {}).get("fetched", 0) > 0]
        if len(live) <= min_group:                 # 领域保底：组太小豁免
            continue
        rate_median = statistics.median(sorted(_rate(a) for _, a in live))
        median = statistics.median(sorted(a["selected"] for _, a in live))
        s, a = min(live, key=lambda x: _rate(x[1]))        # 组内入选率最低
        sel, ad, rate = a["selected"], a["active_days"], _rate(a)
        if sel <= zombie_max:                      # 归 A 僵尸，不重复
            continue
        if ad < min_active_days:                   # 低频样本保护
            continue
        t = tenure_days(s, now)
        if t is not None and t < grace_days:       # 在岗宽限
            continue
        if rate < rate_median / 2:                 # 明显低于同类
            out.append({"name": s["name"], "category": cat, "selected": sel,
                        "fetched": a["fetched"], "rate": rate,
                        "group_rate_median": rate_median,
                        "group_median": median, "group_size": len(live),
                        "tenure_days": t})
    return out


def find_revival_candidates(registry) -> list:
    """挑出值得探测是否复活的源：曾进过生产、且因技术原因（非质量原因）下线。

    两个条件都要满足：
      1. status=rejected 且带 production 字段（曾真正进过生产）——这排除了
         pool-cap 淘汰的 discovered 候选，它们从未进过生产。
      2. reject_reason 不含质量类关键词，且非空。
    """
    out = []
    for s in _reg.get_sources(registry):
        if s.get("status") != "rejected" or not s.get("production"):
            continue
        reason = (s.get("reject_reason") or "").strip().lower()
        if not reason:
            continue
        if any(m in reason for m in REVIVAL_QUALITY_MARKERS):
            continue
        out.append(s)
    return out


@functools.lru_cache(maxsize=1)
def _load_health_module():
    """加载 rss-health-check.py（文件名带连字符，不能直接 import）。

    同 scripts/benchmark_classifier_providers.py 的做法。该模块顶层只有常量与
    函数定义（外加 __main__ 守卫），加载无副作用。

    缓存一次即可——probe_revivals 每探一个 URL 都会走这条路径（默认 prober），
    N 个源不缓存就是最坏 2N+1 次重新 parse+exec 同一份源文件。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("rss_health_check", HEALTH_CHECK_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _default_prober(name: str, url: str) -> dict:
    """真实探测：复用 health-check 的 check_source，只取解析结果。"""
    health = _load_health_module()
    _, status = health.check_source(name, url, "rss", REVIVAL_MAX_AGE_HOURS)
    return status


def probe_revivals(registry, prober=None, *, fallbacks=None) -> list:
    """探测技术性下线的源是否恢复。只返回已恢复的。

    判据是 article_count >= 1 而非 status["ok"] —— ok 还含 staleness 判定，
    而刚恢复的源文章可能很旧，那不该算"没恢复"。

    每个源至多探两个 URL：registry 记的原址，以及它在 FALLBACK_URLS 里的镜像
    （若有）。原址活了就不探镜像。

    fail closed：prober 抛任何异常都记为未恢复，不向上抛。
    """
    if prober is None:
        prober = _default_prober
    if fallbacks is None:
        try:
            fallbacks = _load_health_module().FALLBACK_URLS
        except Exception:
            fallbacks = {}

    revived = []
    try:
        candidates = find_revival_candidates(registry)
    except Exception:
        return []                 # fail closed：畸形 registry 也不能让异常逃出
    for src in candidates:
        name = src.get("name", "")
        urls = [src.get("url", "")]
        fb = fallbacks.get(name)
        if fb and fb not in urls:
            urls.append(fb)
        for url in urls:
            if not url:
                continue
            try:
                status = prober(name, url)
                if not isinstance(status, dict):
                    continue      # prober 违反契约（非 dict）也算未恢复，不让 .get() 抛出
                if status.get("article_count", 0) >= 1:
                    revived.append({
                        "name": name,
                        "reason": src.get("reject_reason") or "",
                        "url": url,
                        "article_count": status.get("article_count", 0),
                        "newest_age_hours": status.get("newest_age_hours"),
                    })
                    break          # 原址活了就不探镜像
            except Exception:
                continue           # fail closed
    return revived


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


def build_report_html(zombies, degraded, snapshot, now, plan_c_html="", rotation=None) -> str:
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

    if rotation:
        r_rows = "".join(
            f"<tr><td>{_esc(r['name'])}</td><td>{_esc(r['category'])}</td>"
            f"<td style='text-align:center'>{r['rate']:.0%}</td>"
            f"<td style='text-align:center'>{r['group_rate_median']:.0%}</td>"
            f"<td style='text-align:center'>{r['selected']}/{r['fetched']}</td>"
            f"<td style='text-align:center'>{r['group_size']}</td>"
            f"<td><code>python3 ~/global-news/rss-demote-source.py --name \"{_esc(r['name'])}\" "
            f"--reason \"rotation-group-laggard\"</code></td></tr>"
            for r in rotation)
        rot_section = (f"<h3>♻️ 建议轮换（{len(rotation)}）组内入选率垫底，确认后 demote 换新源</h3>"
                       "<p style='color:#666;font-size:13px;margin:4px 0'>口径=入选率（selected/fetched）；"
                       "抓取量由 config 的 per-source <code>limit</code> 配额决定，绝对入选数不可跨配额比较。</p>"
                       "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                       "<tr style='background:#f3f4f6'><th>源</th><th>类别</th><th>30d 入选率</th>"
                       "<th>组内入选率中位</th><th>30d 入选/抓取</th><th>组大小</th><th>确认后执行</th></tr>"
                       f"{r_rows}</table>")
    else:
        rot_section = ""

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
            f"{plan_c_html}{a_section}{rot_section}{b_section}{snap_section}")


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
    rotation = find_rotation_candidates(registry, records, now)
    plan_c_html = plan_c_reminder_html(registry, records, now)
    html = build_report_html(zombies, degraded, snapshot, now, plan_c_html, rotation)
    subject = (f"[RSS Pool 复查] {len(zombies)} 僵尸 / {len(rotation)} 建议轮换 / "
               f"{len(degraded)} 变质 — {now.strftime('%m月%d日')}")
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
