#!/usr/bin/env python3
"""Registry for all RSS sources: discovered → trialing → production/rejected.

Replaces config/discovered-rss.json + config/trial-state.json.
news-sources-config.json is still updated as a side-effect of graduation
(it is the operational config used by the sender and health-check).
"""
import json
import os
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_FILE = os.path.join(SCRIPT_DIR, "config", "rss-registry.json")


def _atomic_write(path: str, data: dict) -> None:
    dir_ = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_registry(path: str = REGISTRY_FILE) -> dict:
    if not os.path.isfile(path):
        return {"version": 1, "sources": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_registry(data: dict, path: str = REGISTRY_FILE) -> None:
    _atomic_write(path, data)


def get_sources(registry: dict) -> list[dict]:
    return registry.get("sources", [])


def get_by_status(registry: dict, *statuses: str) -> list[dict]:
    status_set = set(statuses)
    return [s for s in get_sources(registry) if s.get("status") in status_set]


def get_by_url(registry: dict, url: str) -> dict | None:
    norm = url.rstrip("/").lower()
    for s in get_sources(registry):
        if s.get("url", "").rstrip("/").lower() == norm:
            return s
    return None


def get_active_trial(registry: dict) -> dict | None:
    for s in get_sources(registry):
        if s.get("status") == "trialing":
            return s
    return None


def get_trial_history(registry: dict) -> list[dict]:
    """All sources that have been trialed (trial.end_date is set)."""
    return [
        s for s in get_sources(registry)
        if s.get("trial") and s["trial"].get("end_date")
    ]


def get_promotable(registry: dict, threshold: float) -> list[dict]:
    """Candidates eligible for trial: discovered, score >= threshold, never tried before."""
    tried_urls = {
        s.get("url", "").rstrip("/").lower()
        for s in get_sources(registry)
        if s.get("trial")
    }
    result = [
        s for s in get_sources(registry)
        if s.get("status") == "discovered"
        and s.get("url", "").rstrip("/").lower() not in tried_urls
        and (s.get("scores") or {}).get("final", 0.0) >= threshold
    ]
    return sorted(result, key=lambda x: -(x.get("scores") or {}).get("final", 0.0))


def upsert_source(registry: dict, entry: dict) -> bool:
    """Add entry if URL not already present. Returns True if added, False if duplicate."""
    if get_by_url(registry, entry["url"]):
        return False
    if "status" not in entry:
        entry = {**entry, "status": "discovered"}
    registry.setdefault("sources", []).append(entry)
    return True


def start_trial(registry: dict, source: dict, today: str) -> None:
    """Transition source from discovered → trialing and attach trial metadata."""
    for s in get_sources(registry):
        if s.get("url", "").rstrip("/").lower() == source["url"].rstrip("/").lower():
            s["status"] = "trialing"
            s["trial"] = {
                "start_date": today,
                "end_date": None,
                "report_sent": False,
                "daily_stats": [],
                "outcome": None,
                "auto_decided": False,
                "candidate_score": round((source.get("scores") or {}).get("final", 0.0), 3),
            }
            return
    raise KeyError(f"Source not found in registry: {source['url']}")


def update_trial_stats(registry: dict, name: str, date_stats: dict) -> None:
    """Append or update today's daily stats on the active trial source."""
    for s in get_sources(registry):
        if s.get("name") == name and s.get("status") == "trialing":
            stats = s.setdefault("trial", {}).setdefault("daily_stats", [])
            date = date_stats.get("date")
            for existing in stats:
                if existing.get("date") == date:
                    existing.update(date_stats)
                    return
            stats.append(date_stats)
            return
    raise KeyError(f"No active trial found for: {name}")


def end_trial(
    registry: dict,
    name: str,
    outcome: str,
    kept: bool,
    today: str,
    report_sent: bool = False,
    auto_decided: bool = True,
) -> None:
    """Close the active trial. kept=True → production, kept=False → rejected."""
    for s in get_sources(registry):
        if s.get("name") == name and s.get("status") == "trialing":
            trial = s.setdefault("trial", {})
            trial["end_date"] = today
            trial["outcome"] = outcome
            trial["auto_decided"] = auto_decided
            trial["report_sent"] = report_sent
            s["status"] = "production" if kept else "rejected"
            return
    raise KeyError(f"No active trial found for: {name}")


def set_production_config(registry: dict, name: str, keywords: list, limit: int) -> None:
    """Store production config (keywords, limit) on a graduated source."""
    for s in get_sources(registry):
        if s.get("name") == name:
            s["production"] = {"keywords": keywords, "limit": limit}
            return


def reject_source(registry: dict, name: str, reason: str) -> None:
    """Mark a discovered source as rejected with a reason."""
    for s in get_sources(registry):
        if s.get("name") == name:
            s["status"] = "rejected"
            s["reject_reason"] = reason
            return
