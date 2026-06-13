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
