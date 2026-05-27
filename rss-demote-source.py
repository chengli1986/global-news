#!/usr/bin/env python3
"""Demote a production RSS source to rejected and remove it from news-sources-config.

Mirror of rss-promote-candidate.py for the reverse direction. Use when a
production source is verified dead (persistent timeout, WAF block, 403, etc.)
and must be removed from the live feed list. Keeps news-sources-config.json
and config/rss-registry.json in sync — fixes the drift mode where a feed was
deleted from sources only and registry kept status=production (the
Endpoints News + Nikkei Asia case discovered 2026-05-27 via Phase 0 telemetry).

Usage:
    python3 rss-demote-source.py --name "Feed Name" --reason "persistent-timeout"
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


def demote_source(
    name: str,
    reason: str,
    registry_file: str = "",
    sources_file: str = DEFAULT_SOURCES,
) -> bool:
    """Demote *name* from production to rejected. Returns True on success.

    Refuses if the source is not in production status (use trial manager for
    trialing sources; rejected is already terminal).
    """
    reg_path = registry_file or _reg.REGISTRY_FILE
    registry = _reg.load_registry(reg_path)

    source = next(
        (s for s in _reg.get_sources(registry) if s.get("name") == name),
        None,
    )
    if source is None:
        print(f"ERROR: source '{name}' not found in registry.", file=sys.stderr)
        return False
    if source.get("status") != "production":
        print(
            f"ERROR: source '{name}' has status='{source.get('status')}' "
            f"(expected 'production'). Refusing to demote.",
            file=sys.stderr,
        )
        return False

    with open(sources_file, "r", encoding="utf-8") as f:
        sources_data = json.load(f)

    feeds = sources_data.get("news_sources", {}).get("rss_feeds", [])
    target_url_norm = source["url"].rstrip("/").lower()
    kept_feeds = [
        f for f in feeds
        if f.get("url", "").rstrip("/").lower() != target_url_norm
        and f.get("name") != name
    ]
    removed_count = len(feeds) - len(kept_feeds)
    if removed_count:
        sources_data["news_sources"]["rss_feeds"] = kept_feeds
        _atomic_write(sources_file, sources_data)

    _reg.reject_source(registry, name, reason)
    _reg.save_registry(registry, reg_path)

    drift_note = " (registry-only; not in sources-config)" if removed_count == 0 else ""
    print(f"Demoted '{name}' → rejected, reason={reason}{drift_note}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Demote a production RSS source to rejected."
    )
    parser.add_argument("--name", required=True, help="Exact source name to demote")
    parser.add_argument("--reason", required=True, help="reject_reason recorded in registry")
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

    success = demote_source(
        name=args.name,
        reason=args.reason,
        registry_file=args.registry_file,
        sources_file=args.sources_file,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
