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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CANDIDATES = os.path.join(SCRIPT_DIR, "config", "discovered-rss.json")
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
    candidates_file: str = DEFAULT_CANDIDATES,
    sources_file: str = DEFAULT_SOURCES,
) -> bool:
    """Promote *name* from candidates to production sources.

    Returns True on success, False if the candidate cannot be promoted.
    """
    # Load candidates
    with open(candidates_file, "r", encoding="utf-8") as f:
        candidates_data = json.load(f)

    entries: list = candidates_data.get("discovered", [])

    # Find the candidate
    target = None
    target_idx = -1
    for idx, entry in enumerate(entries):
        if entry.get("name") == name and not entry.get("promoted", False):
            target = entry
            target_idx = idx
            break

    if target is None:
        print(f"ERROR: candidate '{name}' not found or already promoted.", file=sys.stderr)
        return False

    # Load sources
    with open(sources_file, "r", encoding="utf-8") as f:
        sources_data = json.load(f)

    new_feed = {
        "name": target["name"],
        "url": target["url"],
        "keywords": [],
        "limit": limit,
    }
    sources_data["news_sources"]["rss_feeds"].append(new_feed)

    # Atomic write sources first, then mark candidate as promoted
    _atomic_write(sources_file, sources_data)

    entries[target_idx]["promoted"] = True
    _atomic_write(candidates_file, candidates_data)

    print(f"Promoted '{name}' → {target['url']} (limit={limit})")
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
        "--candidates-file",
        default=DEFAULT_CANDIDATES,
        help=f"Path to discovered-rss.json (default: {DEFAULT_CANDIDATES})",
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
        candidates_file=args.candidates_file,
        sources_file=args.sources_file,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
