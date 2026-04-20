#!/usr/bin/env python3
"""Promote a discovered RSS candidate into the production sources list.

Usage:
    python3 rss-promote-candidate.py --name "Feed Name" [--limit 3]
"""
import argparse
import json
import os
import sys
import tempfile

import rss_registry as _reg

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOURCES = os.path.join(SCRIPT_DIR, "news-sources-config.json")


def _atomic_write(path: str, data: dict) -> None:
    """Write JSON to *path* atomically via a temp file + os.replace."""
    dir_ = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def promote_candidate(
    name: str,
    limit: int = 3,
    registry_file: str = "",
    sources_file: str = DEFAULT_SOURCES,
) -> bool:
    """Promote *name* from candidates to production sources.

    Returns True on success, False if the candidate cannot be promoted.
    """
    reg_path = registry_file or _reg.REGISTRY_FILE
    registry = _reg.load_registry(reg_path)

    # Find the candidate
    source = next(
        (s for s in _reg.get_sources(registry)
         if s.get("name") == name and s.get("status") == "discovered"),
        None,
    )
    if source is None:
        print(f"ERROR: candidate '{name}' not found or not in discovered status.", file=sys.stderr)
        return False

    # Load production sources config
    with open(sources_file, "r", encoding="utf-8") as f:
        sources_data = json.load(f)

    # Idempotency: skip if URL already in sources
    existing_urls = {
        s.get("url", "").rstrip("/").lower()
        for s in sources_data.get("news_sources", {}).get("rss_feeds", [])
    }
    target_url_norm = source["url"].rstrip("/").lower()
    if target_url_norm not in existing_urls:
        new_feed = {
            "name": source["name"],
            "url": source["url"],
            "keywords": [],
            "limit": limit,
        }
        sources_data["news_sources"]["rss_feeds"].append(new_feed)
        _atomic_write(sources_file, sources_data)

    # Update registry: mark as production
    source["status"] = "production"
    _reg.set_production_config(registry, name, keywords=[], limit=limit)
    _reg.save_registry(registry, reg_path)

    print(f"Promoted '{name}' → {source['url']} (limit={limit})")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote a discovered RSS candidate to production sources."
    )
    parser.add_argument("--name", required=True, help="Exact candidate name to promote")
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Article limit for the new feed entry (default: 3)",
    )
    parser.add_argument(
        "--registry-file",
        default="",
        help=f"Path to rss-registry.json (default: {_reg.REGISTRY_FILE})",
    )
    parser.add_argument(
        "--sources-file",
        default=DEFAULT_SOURCES,
        help=f"Path to news-sources-config.json (default: {DEFAULT_SOURCES})",
    )
    args = parser.parse_args()

    success = promote_candidate(
        name=args.name,
        limit=args.limit,
        registry_file=args.registry_file,
        sources_file=args.sources_file,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
