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
import time
from datetime import datetime, timezone, timedelta

import rss_registry as _reg

BJT = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "logs", "production-source-log.jsonl")
ENV_FILE = os.path.expanduser("~/.stock-monitor.env")
SENDER_FILE = os.path.join(SCRIPT_DIR, "unified-global-news-sender.py")
PLAN_C_CATEGORIES = ("healthcare", "vertical", "global_south")  # categories with no dedicated board until 方案 C
SOURCES_FILE = os.path.join(SCRIPT_DIR, "news-sources-config.json")
ROTATION_MIN_GROUP = 3
ROTATION_WINDOW_DAYS = 30
ROTATION_MIN_ACTIVE_DAYS = 7
ROTATION_GRACE_DAYS = 30
# 比例阈值(组中位/2)在中位数附近是悬崖：2026-08-16 周报点名 RFI English(30.39%)，
# 同组 The Guardian World(31.37%) 安全——差半篇文章，判定却是"永久下线"vs"没事"。
# 要求同时跨过这个绝对边距，把噪声挡在外面。
ROTATION_MIN_MARGIN = 0.03
# 自身历史基线与门：组内垫底还不够，还得比它自己的上一个同长窗口明显更差。
# 2026-06-15 全池筛选口径变更让所有源集体腰斩（变更前 26 个源都是 100% 入选，
# 那阶段根本没在筛），只看组内相对会把全局变更误判成个别源变差。
ROTATION_MIN_SELF_DECLINE = 0.05
ROTATION_MIN_BASELINE_FETCHED = 10

# 复活探测：这些 reject_reason 关键词代表"质量不行被汰"，恢复了也不该回来。
# 用排除法而非白名单——将来出现新的技术性下线原因（如 dns-fail）会自动纳入探测。
# 定义在 rss_registry，与 rss-trial-manager 的 retry 守卫共用一份，避免口径漂移。
REVIVAL_QUALITY_MARKERS = _reg.QUALITY_REJECT_MARKERS
HEALTH_CHECK_FILE = os.path.join(SCRIPT_DIR, "rss-health-check.py")
REVIVAL_MAX_AGE_HOURS = 24 * 365  # 绕过 staleness 判定：复活探测只关心"解析得出文章"

# cron 是 `--timeout 300`（SIGKILL --kill-after 30），历史 duration 只有 4s。
# 每候选最多探 2 个 URL、每次 urlopen(timeout=15) —— 但那是单次 socket 超时，不是
# 总时长上限，getaddrinfo 也不受它约束。registry 里已有 13 条带 production 的
# rejected 源，一旦技术性下线累积到 9 条以上，最坏 2×9×15=270s 逼近超时，进程会在
# send_report_email 之前被杀 → 整周没有周报。60s 留足给 resolver + 邮件发送。
REVIVAL_PROBE_BUDGET_SECONDS = 60


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


@functools.lru_cache(maxsize=1)
def _load_region_map() -> dict:
    """source name → 邮件板块，取自 evaluate_digest.SOURCE_TO_REGION。

    读不到就返回 {}——豁免随之失效（回到只有比例+边距两道门），但周报照发。
    """
    try:
        import evaluate_digest
        return dict(evaluate_digest.SOURCE_TO_REGION)
    except Exception:
        return {}


def find_sole_region_sources(registry, region_map=None, exclude=()) -> set:
    """现役源里，哪些是自己那个邮件板块的唯一供给。

    只数 production：已离池的源留在映射表里会让板块显得"还有人撑着"
    （路由表死引用是这个系统反复出现的漂移，见 tests/test_region_rules_liveness.py）。
    """
    rmap = _load_region_map() if region_map is None else region_map
    skip = set(exclude)
    live = {s.get("name") for s in _reg.get_by_status(registry, "production")} - skip
    per_region = {}
    for name in live:
        region = rmap.get(name)
        if region:
            per_region.setdefault(region, []).append(name)
    return {names[0] for names in per_region.values() if len(names) == 1}


def find_exempt_laggards(registry, records, now, *, region_map=None, **kw) -> list:
    """本该被标为轮换候选、但因是板块唯一供给而豁免的源。

    豁免绝不能是静默的：入选率垫底仍然是个信号，藏起来等于把"该补源了"这件事
    一起藏了。报告单列一节，附板块名，让人知道要先补源再谈淘汰。
    """
    sole = find_sole_region_sources(registry, region_map)
    if not sole:
        return []
    rmap = _load_region_map() if region_map is None else region_map
    raw = find_rotation_candidates(registry, records, now, region_map={}, **kw)
    return [dict(r, region=rmap.get(r["name"], "?")) for r in raw if r["name"] in sole]


def self_baseline_rate(records, source, now, *, window_days=30,
                       min_fetched=ROTATION_MIN_BASELINE_FETCHED):
    """源在"上一个同长窗口"[now-2w, now-w) 的入选率；样本不足返回 None。

    刻意用紧邻的上一个窗口而不是全历史：全历史会把 2026-06-15 变更之前那段
    "抓到就选"的 100% 混进基线，让每个源看起来都在暴跌。
    """
    lo, hi = now - timedelta(days=window_days * 2), now - timedelta(days=window_days)
    f = sel = 0
    for r in records:
        if r.get("source") != source:
            continue
        try:
            ts = parse_ts(r["ts"])
        except (KeyError, ValueError):
            continue
        if lo <= ts < hi:
            f += int(r.get("fetched", 0) or 0)
            sel += int(r.get("selected", 0) or 0)
    if f < min_fetched:
        return None
    return sel / f


def find_rotation_candidates(registry, records, now, *, window_days=30,
                             min_group=3, min_active_days=7, grace_days=30,
                             zombie_max=1, min_margin=ROTATION_MIN_MARGIN,
                             region_map=None, exclude=(),
                             min_self_decline=ROTATION_MIN_SELF_DECLINE) -> list:
    """组内实测优胜劣汰：每个 category 内**入选率**垫底且明显低于同类的源 → 建议轮换。

    口径为入选率(selected/fetched)而非绝对入选数：`fetched` 由 news-sources-config
    的 per-source `limit` 配额决定（limit=3 → 30d 抓 ~99 篇，limit=6 → ~198），
    绝对入选数的天花板被配额锁死，按绝对数比中位会系统性误判小配额源垫底
    （2026-07-26 IEEE Spectrum 误报：44% 入选率却因 limit=3 被点名）。

    判据是两道门：入选率 < 组内中位的一半，**且**比这个阈值再低 min_margin 个百分点。
    只有比例这一道时，中位数附近就是悬崖——2026-08-16 周报点名 RFI English(30.39%)，
    同组 The Guardian World(31.37%) 安全，两者差半篇文章，判定却是永久下线 vs 没事。

    保多元：legacy(无 category)豁免；组内有数据源 <= min_group 整组豁免；每组最多标 1 个；
    **组内垫底源若是某邮件板块的唯一供给则整组本轮不标**（棘轮效应：每淘汰一个就把组
    中位推高、机械造出下一个垫底，2026-08-17 demote Dawn Pakistan 后 CNA 当场从
    "被挡住"变成 −7.11pp，而 ASIA-PACIFIC 只有 CNA 一个源）。被这条挡下的源不会消失，
    由 find_exempt_laggards 单列进报告。
    去重：selected <= zombie_max 的归 A 僵尸，不在此重复。低频保护沿用 active_days/在岗宽限。
    """
    import collections
    skip = set(exclude)
    sole_region_sources = find_sole_region_sources(registry, region_map, exclude=skip)
    agg = aggregate_by_source(filter_window(records, now, window_days))
    by_cat = collections.defaultdict(list)
    for s in _reg.get_by_status(registry, "production"):
        if s.get("name") in skip:                  # 模拟"已被 demote"
            continue
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
        if s["name"] in sole_region_sources:       # 板块唯一供给：没替代源不能淘汰
            continue
        if sel <= zombie_max:                      # 归 A 僵尸，不重复
            continue
        if ad < min_active_days:                   # 低频样本保护
            continue
        t = tenure_days(s, now)
        if t is not None and t < grace_days:       # 在岗宽限
            continue
        if rate >= rate_median / 2 - min_margin:   # 未明显低于同类（比例 + 绝对边距）
            continue
        base = delta = None
        if min_self_decline is not None:
            base = self_baseline_rate(records, s["name"], now, window_days=window_days)
            if base is None:                       # 算不出基线 → 偏向留源
                continue                           # （demote 不可逆，缺证据不等于没问题）
            delta = rate - base
            if delta > -min_self_decline:          # 自身没明显退步 → 可能只是全局变更
                continue
        out.append({"name": s["name"], "category": cat, "selected": sel,
                    "fetched": a["fetched"], "rate": rate,
                    "group_rate_median": rate_median,
                    "group_median": median, "group_size": len(live),
                    "tenure_days": t,
                    "self_baseline_rate": base, "self_delta": delta})
    return out


def annotate_successors(registry, records, now, rotation, **kw) -> list:
    """给每条轮换建议标出"执行后下一个垫底是谁"。

    棘轮效应是这套规则的固有性质：判据是**组内相对**的，淘汰垫底会把组中位推高，
    阈值(中位/2 - margin)跟着抬，于是当场造出下一个垫底。min_margin 挡的是单次
    噪声，挡不住这个连锁——2026-08-17 demote Dawn Pakistan 后 CNA 立刻从"被挡住"
    变成命中，就是同一个机制。

    这里不改判定，只把连锁摊开：按同一套规则重算"假设它已经走了"的下一轮，
    让人在按下不可逆命令之前先看见代价。successor=None 表示淘汰后该组缩到保底线
    以下（整组豁免）或次低者确实安全。
    """
    out = []
    for r in rotation:
        nxt = find_rotation_candidates(registry, records, now,
                                       exclude=(r["name"],), **kw)
        succ = next((x for x in nxt if x["category"] == r["category"]), None)
        out.append(dict(r,
                        successor=succ["name"] if succ else None,
                        successor_rate=succ["rate"] if succ else None))
    return out


def _normalize_feed_url(url: str) -> str:
    """比对用的 URL 规范化：去协议、去尾斜杠、小写。

    registry 里 `https://endpts.com/feed/` 与现役 config 里 `http://endpts.com/feed`
    是同一个源，字符串直接比对认不出来。
    """
    u = (url or "").strip().lower().rstrip("/")
    for scheme in ("https://", "http://"):
        if u.startswith(scheme):
            return u[len(scheme):]
    return u


def _load_live_feeds(sources_file: str = "") -> list:
    """读现役 config 的 rss_feeds。读不到就当空池——探测多报几条，不至于炸掉周报。"""
    try:
        with open(sources_file or SOURCES_FILE, encoding="utf-8") as f:
            return json.load(f).get("news_sources", {}).get("rss_feeds", [])
    except Exception:
        return []


def find_revival_candidates(registry, live_feeds=None) -> list:
    """挑出值得探测是否复活的源：曾进过生产、且因技术原因（非质量原因）下线。

    三个条件都要满足：
      1. status=rejected 且带 production 字段（曾真正进过生产）——这排除了
         pool-cap 淘汰的 discovered 候选，它们从未进过生产。
      2. reject_reason 不含质量类关键词，且非空。
      3. 名字和 URL 都不在现役 config 里。registry 允许同名多条（2026-08-16 实测
         30 组重名，Endpoints News 独占 4 条），一条 rejected 的幽灵孤儿会让一个
         从未离岗的源被报成"复活"——照报告去 promote 还会因为 URL 变体绕过
         rss-promote-candidate.py 的幂等检查，往池里塞进同源重复。
         live_feeds=None 表示自己去读 SOURCES_FILE；传 [] 才是"池子是空的"。
    """
    feeds = _load_live_feeds() if live_feeds is None else live_feeds
    live_names = {(f.get("name") or "").strip().lower() for f in feeds}
    live_urls = {_normalize_feed_url(f.get("url")) for f in feeds}

    out = []
    for s in _reg.get_sources(registry):
        if s.get("status") != "rejected" or not s.get("production"):
            continue
        reason = (s.get("reject_reason") or "").strip().lower()
        if not reason:
            continue
        if any(m in reason for m in REVIVAL_QUALITY_MARKERS):
            continue
        if (s.get("name") or "").strip().lower() in live_names:
            continue
        if _normalize_feed_url(s.get("url")) in live_urls:
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


def _default_url_resolver(url: str) -> str:
    """跟随重定向，返回最终 URL。发的是 GET，但不 read() body——靠 with 关闭连接。"""
    import urllib.request
    health = _load_health_module()
    req = urllib.request.Request(url, headers=health.HEADERS)
    with urllib.request.urlopen(req, timeout=health.FETCH_TIMEOUT) as r:
        return r.url or url


def _resolve_final_url(url: str, resolver=None) -> str:
    """解析 url 的落地地址。任何失败都退回原 url——绝不返回 None/空串。

    存在的理由：2026-08-08 首次实网探测发现 endpts.com/feed/ 是 301 永久重定向
    到 endpoints.news/feed/。周报只报 registry 里的旧 URL，人照着恢复入池就把
    死域名写回了 registry。
    """
    if resolver is None:
        resolver = _default_url_resolver
    try:
        final = resolver(url)
    except Exception:
        return url
    return final or url


def probe_revivals(registry, prober=None, *, fallbacks=None, resolver=None,
                   budget_seconds=REVIVAL_PROBE_BUDGET_SECONDS, clock=time.monotonic,
                   live_feeds=None) -> list:
    """探测技术性下线的源是否恢复。只返回已恢复的。

    判据是 article_count >= 1 而非 status["ok"] —— ok 还含 staleness 判定，
    而刚恢复的源文章可能很旧，那不该算"没恢复"。

    每个源至多探两个 URL：registry 记的原址，以及它在 FALLBACK_URLS 里的镜像
    （若有）。原址活了就不探镜像。

    fail closed：prober 抛任何异常都记为未恢复，不向上抛。

    总时长受 budget_seconds 限制（用 time.monotonic 而非 time.time，避免系统时钟
    调整干扰）——超预算就跳过剩余候选，不抛异常，只 print 一行说明（cron 日志能
    收到，不占周报版面）。这是硬约束：这个附加功能绝不能把周报的 cron 超时预算
    吃光，导致 send_report_email 之前进程被杀、整周没有周报。
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
        candidates = find_revival_candidates(registry, live_feeds=live_feeds)
    except Exception:
        return []                 # fail closed：畸形 registry 也不能让异常逃出
    deadline = clock() + budget_seconds
    for i, src in enumerate(candidates):
        if clock() >= deadline:
            remaining = len(candidates) - i
            print(f"[prod-review] revival probe budget ({budget_seconds}s) exceeded, "
                  f"skipping remaining {remaining} candidate(s)")
            break
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
                        "final_url": _resolve_final_url(url, resolver),
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


IRREVERSIBLE_HTML = (
    "<p style='background:#fff4e5;border-left:4px solid #f59e0b;padding:8px 12px;"
    "margin:10px 0;font-size:13px'>⚠️ <b>下面的 demote 命令不可逆</b>：registry 里 "
    "<code>rejected</code> 是终态（<code>rss-demote-source.py</code> 拒绝反向操作），"
    "且 <code>zombie</code> / <code>rotation-group-laggard</code> 都在 "
    "<code>QUALITY_REJECT_MARKERS</code> 里——复活探测会<b>主动跳过</b>它们，源恢复了也不会自己回来。"
    "要回池只能重新走 discovery/trial。</p>")


def _self_trend_cell(r: dict) -> str:
    """轮换建议里的"自身趋势"列：上一同长窗口的入选率 → 现在。

    摆出来是为了让人看见淘汰理由不只是"组内比别人差"，而是"它自己也在退步"——
    这两件事在 2026-06-15 那种全池口径变更下会完全脱钩。
    """
    base = r.get("self_baseline_rate")
    if base is None:
        return "—"
    d = r.get("self_delta")
    arrow = f"（{d*100:+.0f}pp）" if isinstance(d, (int, float)) else ""
    return f"{base:.0%} → {r['rate']:.0%}{arrow}"


def _successor_cell(r: dict) -> str:
    """轮换建议里的"执行后下一个垫底"列。

    没跑过 annotate_successors 时显示"—"而不是骗人的"无"：区分"算过且确实没有"
    与"根本没算"。
    """
    if "successor" not in r:
        return "—"
    if r["successor"] is None:
        return "<span style='color:#16a34a'>无</span>"
    rate = r.get("successor_rate")
    tail = f"（{rate:.0%}）" if isinstance(rate, (int, float)) else ""
    return f"<b style='color:#b91c1c'>{_esc(r['successor'])}</b>{tail}"


def build_report_html(zombies, degraded, snapshot, now, plan_c_html="", rotation=None,
                      revival_html_block="", exempt=None) -> str:
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
            f"<td style='text-align:center'>{_self_trend_cell(r)}</td>"
            f"<td style='text-align:center'>{_successor_cell(r)}</td>"
            f"<td><code>python3 ~/global-news/rss-demote-source.py --name \"{_esc(r['name'])}\" "
            f"--reason \"rotation-group-laggard\"</code></td></tr>"
            for r in rotation)
        rot_section = (f"<h3>♻️ 建议轮换（{len(rotation)}）组内入选率垫底，确认后 demote 换新源</h3>"
                       "<p style='color:#666;font-size:13px;margin:4px 0'>口径=入选率（selected/fetched）；"
                       "抓取量由 config 的 per-source <code>limit</code> 配额决定，绝对入选数不可跨配额比较。"
                       "<br>两道门都要过：<b>组内明显垫底</b> 且 <b>自身相对上一个同长窗口也在跌</b>"
                       "——只看组内相对，会把 2026-06-15 那种全池口径变更误判成个别源变差。</p>"
                       "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                       "<tr style='background:#f3f4f6'><th>源</th><th>类别</th><th>30d 入选率</th>"
                       "<th>组内入选率中位</th><th>30d 入选/抓取</th><th>组大小</th>"
                       "<th>自身趋势</th><th>执行后下一个垫底</th><th>确认后执行</th></tr>"
                       f"{r_rows}</table>")
    else:
        rot_section = ""

    warn_section = IRREVERSIBLE_HTML if (zombies or rotation) else ""

    if exempt:
        e_rows = "".join(
            f"<tr><td>{_esc(x['name'])}</td><td>{_esc(x['category'])}</td>"
            f"<td>{_esc(x.get('region', '?'))}</td>"
            f"<td style='text-align:center'>{x['rate']:.0%}</td>"
            f"<td style='text-align:center'>{x['group_rate_median']:.0%}</td>"
            f"<td style='text-align:center'>{x['selected']}/{x['fetched']}</td></tr>"
            for x in exempt)
        exempt_section = (
            f"<h3>🛡️ 板块唯一源豁免（{len(exempt)}）入选率够得上轮换，但没有替代源</h3>"
            "<p style='color:#666;font-size:13px;margin:4px 0'>这些源是所列邮件板块的"
            "<b>唯一供给</b>，淘汰即该板块无源级保障，故本轮不建议 demote，也不给命令。"
            "要处理请<b>先补一个同板块的源</b>（discovery/trial 走完），下轮它们会自动回到"
            "建议轮换里。<b>不补源就长期挂在这里</b>——这条豁免只挡刀，不解决入选率低本身。</p>"
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            "<tr style='background:#e8f5e9'><th>源</th><th>类别</th><th>邮件板块</th>"
            "<th>30d 入选率</th><th>组内入选率中位</th><th>30d 入选/抓取</th></tr>"
            f"{e_rows}</table>")
    else:
        exempt_section = ""

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
            f"{revival_html_block}{plan_c_html}{warn_section}{a_section}{rot_section}{exempt_section}"
            f"{b_section}{snap_section}")


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


def revival_html(revived: list) -> str:
    """周报里的"已下线源复活"提醒条；无源恢复则返回空串（不占版面）。"""
    if not revived:
        return ""
    cells = []
    for r in revived:
        url = r.get("url", "")
        final = r.get("final_url") or url
        if final != url:
            # href 用双引号：final 来自 urllib 跟随重定向后的落地地址，完全由外部
            # 服务端 Location 头决定，_esc() 不转义单引号——单引号属性会被
            # Location: https://x/f'%20onmouseover='y 这类值撬开变成属性注入。
            url_cell = (f'<a href="{_esc(final)}">{_esc(final)}</a>'
                        f"<br><span style='color:#b26500;font-size:11px'>⤴ 跳转自 "
                        f"{_esc(url)}——恢复入池请用上面的新地址</span>")
        else:
            url_cell = f'<a href="{_esc(url)}">{_esc(url)}</a>'
        age = r.get("newest_age_hours")
        cells.append(
            f"<tr><td>{_esc(r.get('name', ''))}</td>"
            f"<td style='font-size:11px;color:#666'>{_esc(r.get('reason', ''))}</td>"
            f"<td>{url_cell}</td>"
            f"<td style='text-align:center'>{r.get('article_count', 0)}</td>"
            f"<td style='text-align:center'>"
            f"{('%.0fh' % age) if age is not None else '—'}</td></tr>")
    rows = "".join(cells)
    return (
        "<div style='margin-top:16px;padding:12px 16px;background:#e8f5e9;"
        "border-left:4px solid #43a047;font-size:13px;line-height:1.6;'>"
        f"<strong>♻️ 已下线源复活检测（{len(revived)}）</strong>："
        "以下曾因技术原因下线的源现在又能抓到文章了。"
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse;margin-top:8px;background:#fff'>"
        "<tr style='background:#f3f4f6'><th>源</th><th>当初下线原因</th>"
        "<th>活着的 URL</th><th>抓到</th><th>最新</th></tr>"
        f"{rows}</table>"
        "<br><span style='color:#666;font-size:11px;'>恢复入池需手工把 registry 里该源的 "
        "<code>status</code> 从 <code>rejected</code> 改回，"
        "<code>rss-promote-candidate.py</code> 只接受 <code>discovered</code> 状态。"
        "恢复前请重新确认当初下线的口径判断是否仍成立。</span>"
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
    rotation = annotate_successors(registry, records, now,
                                   find_rotation_candidates(registry, records, now))
    exempt = find_exempt_laggards(registry, records, now)
    plan_c_html = plan_c_reminder_html(registry, records, now)
    try:
        revived = probe_revivals(registry)
        revival_block = revival_html(revived)
    except Exception:
        # 复活探测/渲染是附加功能，绝不能拖垮周报这封每周唯一的例行邮件。
        revived = []
        revival_block = ""
        print("[prod-review] revival probe/render failed, skipping revival section")
    html = build_report_html(zombies, degraded, snapshot, now, plan_c_html, rotation,
                             revival_block, exempt)
    subject = (f"[RSS Pool 复查] {len(zombies)} 僵尸 / {len(rotation)} 建议轮换 / "
               f"{len(degraded)} 变质 — {now.strftime('%m月%d日')}")
    print(f"[prod-review] {len(zombies)} zombies, {len(degraded)} degraded, "
          f"{len(snapshot)} sources reviewed, {len(revived)} revived, "
          f"{len(exempt)} exempt (sole source of a digest region).")
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
