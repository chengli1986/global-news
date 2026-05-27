#!/usr/bin/env python3
"""One-time backfill: add legacy RSS feeds from news-sources-config.json to rss-registry.json.

Why: rss-registry.json (introduced 2026-04-21 cc680c4) only tracks sources
that went through the AI-discovery → trial → production pipeline. Pre-existing
RSS feeds (Bloomberg/FT/CNBC/BBC/Economist/SCMP/…) were never migrated, so
Phase 0/0.5 telemetry (which reads registry production sources) only covers
~20/52 RSS sources — main media outlets are invisible to source-fitness
monitoring.

This script reads news-sources-config.json rss_feeds and adds any URL not
already in rss-registry.json as a production entry with `discovered_via="legacy"`.
Idempotent — re-running is a no-op once backfilled.

Usage:
    python3 scripts/backfill_legacy_to_registry.py [--dry-run]
"""
import argparse
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_FILE = os.path.join(REPO, "news-sources-config.json")
REGISTRY_FILE = os.path.join(REPO, "config", "rss-registry.json")


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


def backfill(
    sources_path: str = SOURCES_FILE,
    registry_path: str = REGISTRY_FILE,
    dry_run: bool = False,
) -> list[str]:
    """Add legacy rss_feeds to registry. Returns list of newly added source names."""
    with open(sources_path, encoding="utf-8") as f:
        cfg = json.load(f)
    with open(registry_path, encoding="utf-8") as f:
        registry = json.load(f)

    existing_urls = {
        s.get("url", "").rstrip("/").lower()
        for s in registry.get("sources", [])
    }
    # Also match by name to catch URL-drift cases where rss-health auto-swap
    # changed the live URL in news-sources-config but the registry kept the
    # original URL (e.g., 澎湃新闻: registry=rsshub, sources-config=plink).
    existing_names = {
        s.get("name") for s in registry.get("sources", [])
        if s.get("status") == "production"
    }

    added: list[str] = []
    for feed in cfg.get("news_sources", {}).get("rss_feeds", []):
        url_norm = feed["url"].rstrip("/").lower()
        if url_norm in existing_urls or feed["name"] in existing_names:
            continue
        entry = {
            "name": feed["name"],
            "url": feed["url"],
            "status": "production",
            "discovered_via": "legacy",
            "validation": None,
            "scores": None,
            "reject_reason": None,
            "trial": None,
            "production": {
                "keywords": feed.get("keywords", []),
                "limit": feed.get("limit", 3),
            },
        }
        registry.setdefault("sources", []).append(entry)
        added.append(feed["name"])
        existing_urls.add(url_norm)
        existing_names.add(feed["name"])

    if added and not dry_run:
        _atomic_write(registry_path, registry)
    return added


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill legacy RSS feeds into rss-registry.")
    p.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    p.add_argument("--sources-file", default=SOURCES_FILE)
    p.add_argument("--registry-file", default=REGISTRY_FILE)
    args = p.parse_args()

    added = backfill(
        sources_path=args.sources_file,
        registry_path=args.registry_file,
        dry_run=args.dry_run,
    )
    prefix = "[dry-run] would add" if args.dry_run else "Added"
    print(f"{prefix} {len(added)} legacy source(s) to registry:")
    for n in added:
        print(f"  - {n}")
    sys.exit(0)


if __name__ == "__main__":
    main()
