#!/usr/bin/env python3
"""One-time migration: discovered-rss.json + trial-state.json → rss-registry.json."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES_FILE = os.path.join(REPO, "config", "discovered-rss.json")
TRIAL_STATE_FILE = os.path.join(REPO, "config", "trial-state.json")
SOURCES_FILE = os.path.join(REPO, "news-sources-config.json")
REGISTRY_FILE = os.path.join(REPO, "config", "rss-registry.json")


def _atomic_write(path: str, data: dict) -> None:
    """Write JSON file atomically using temp file + rename."""
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


def migrate() -> None:
    """Migrate discovered-rss.json + trial-state.json → rss-registry.json."""
    with open(CANDIDATES_FILE, encoding="utf-8") as f:
        candidates_data = json.load(f)
    with open(TRIAL_STATE_FILE, encoding="utf-8") as f:
        trial_data = json.load(f)
    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources_data = json.load(f)

    candidates = candidates_data.get("candidates", [])
    active_trial = trial_data.get("active_trial")
    history = trial_data.get("history", [])
    rss_feeds = sources_data.get("news_sources", {}).get("rss_feeds", [])

    prod_by_url = {f["url"].rstrip("/").lower(): f for f in rss_feeds}
    history_by_name = {h["name"]: h for h in history}
    active_name = active_trial["name"] if active_trial else None

    sources = []
    for cand in candidates:
        name = cand["name"]
        url_norm = cand["url"].rstrip("/").lower()

        if cand.get("promoted"):
            status = "production"
        elif cand.get("rejected"):
            status = "rejected"
        elif name == active_name:
            status = "trialing"
        else:
            status = "discovered"

        entry: dict = {
            "name": name,
            "url": cand["url"],
            "language": cand.get("language", "en"),
            "category": cand.get("category", ""),
            "status": status,
            "discovered_via": cand.get("discovered_via", ""),
            "validation": cand.get("validation"),
            "scores": cand.get("scores"),
            "reject_reason": cand.get("reject_reason"),
            "trial": None,
            "production": None,
        }

        if status == "trialing" and active_trial:
            entry["trial"] = {
                "start_date": active_trial["start_date"],
                "end_date": active_trial.get("end_date"),
                "report_sent": active_trial.get("report_sent", False),
                "daily_stats": active_trial.get("daily_stats", []),
                "outcome": active_trial.get("outcome"),
                "auto_decided": active_trial.get("auto_decided", False),
                "candidate_score": active_trial.get("candidate_score", 0.0),
            }

        if name in history_by_name:
            h = history_by_name[name]
            entry["trial"] = {
                "start_date": h["start_date"],
                "end_date": h.get("end_date"),
                "report_sent": h.get("report_sent", False),
                "daily_stats": h.get("daily_stats", []),
                "outcome": h.get("outcome"),
                "auto_decided": h.get("auto_decided", False),
                "candidate_score": h.get("candidate_score", 0.0),
            }

        if status == "production" and url_norm in prod_by_url:
            feed = prod_by_url[url_norm]
            entry["production"] = {
                "keywords": feed.get("keywords", []),
                "limit": feed.get("limit", 3),
            }

        sources.append(entry)

    registry = {"version": 1, "sources": sources}
    _atomic_write(REGISTRY_FILE, registry)

    by_status: dict[str, int] = {}
    for s in sources:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1

    print(f"Migration complete: {len(sources)} sources → {REGISTRY_FILE}")
    for st, count in sorted(by_status.items()):
        print(f"  {st}: {count}")


def verify() -> None:
    """Verify rss-registry.json integrity."""
    with open(REGISTRY_FILE, encoding="utf-8") as f:
        registry = json.load(f)
    sources = registry.get("sources", [])
    trialing = [s for s in sources if s["status"] == "trialing"]
    assert len(trialing) <= 1, f"Multiple trialing sources: {[s['name'] for s in trialing]}"
    for s in sources:
        assert "name" in s and "url" in s and "status" in s, f"Missing fields: {s}"
        if s["status"] == "trialing":
            assert s.get("trial") and s["trial"].get("start_date"), \
                f"trialing source missing trial.start_date: {s['name']}"
    print(f"Verification passed: {len(sources)} sources, {len(trialing)} active trial")


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    else:
        migrate()
