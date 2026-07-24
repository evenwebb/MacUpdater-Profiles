#!/usr/bin/env python3
"""Bulk-clean validate_profiles.py soft warnings.

1. Landing-page download URLs → method redirect_url
2. Hardcoded GitHub construct URLs → {VERSION} placeholders
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".github", "scripts", "__pycache__", "badges"}

_ARCHIVE_EXT = (".dmg", ".pkg", ".zip", ".tar.gz", ".tgz")
_VERSIONED_GITHUB = re.compile(
    r"(https://github\.com/[^/]+/[^/]+/releases/download)/v?[\d.]+/(.+)$",
    re.I,
)
# Capture version-like segments in filenames for placeholder rewrite
_VER_IN_NAME = re.compile(r"(?<![A-Za-z])(\d+\.\d+(?:\.\d+)*)(?![A-Za-z])")


def _is_archive_url(url: str) -> bool:
    low = url.lower().split("?", 1)[0]
    return any(low.endswith(ext) for ext in _ARCHIVE_EXT)


def fix_landing_page(data: dict) -> bool:
    dl = data.get("download") or {}
    method = (dl.get("method") or "").lower()
    url = (dl.get("url") or "").strip()
    if not url or method not in {"direct_url", "construct_url", "constructed_url", "fixed_url"}:
        return False
    if _is_archive_url(url):
        return False
    # Marketing / download-page URLs resolve via redirect
    dl["method"] = "redirect_url"
    if not dl.get("note"):
        dl["note"] = "Landing/download page URL; MacUpdater follows redirects to the archive."
    data["download"] = dl
    return True


def fix_hardcoded_github(data: dict) -> bool:
    dl = data.get("download") or {}
    method = (dl.get("method") or "").lower()
    if method not in {"construct_url", "constructed_url", "versioned_url", "github_release"}:
        return False
    url = (dl.get("url") or dl.get("pattern") or "").strip()
    if not url or "{VERSION}" in url or "{version}" in url or "{TAG}" in url:
        return False
    # Only rewrite obvious GitHub release asset URLs with embedded versions
    m = _VERSIONED_GITHUB.match(url)
    if not m:
        return False
    base, filename = m.group(1), m.group(2)
    # Replace version tokens in filename with {VERSION}
    new_name = _VER_IN_NAME.sub("{VERSION}", filename, count=2)
    # Prefer v{VERSION} in the path segment
    new_url = f"{base}/v{{VERSION}}/{new_name}"
    if dl.get("url"):
        dl["url"] = new_url
    else:
        dl["pattern"] = new_url
    dl["method"] = "construct_url"
    data["download"] = dl
    return True


def main() -> None:
    changed = 0
    for d in sorted(REPO.iterdir()):
        if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
            continue
        for f in sorted(d.glob("*.json")):
            if f.name.startswith("_"):
                continue
            data = json.loads(f.read_text())
            touched = False
            if fix_landing_page(data):
                touched = True
            if fix_hardcoded_github(data):
                touched = True
            if touched:
                f.write_text(json.dumps(data, indent=2) + "\n")
                changed += 1
                print(f"updated {f.relative_to(REPO)}")
    print(f"changed {changed} profiles")


if __name__ == "__main__":
    main()
