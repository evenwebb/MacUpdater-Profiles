#!/usr/bin/env python3
"""Validate all profiles against schema.json. Exit 1 on any failure."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".github", "scripts", "__pycache__"}

REQUIRED = ["name", "slug", "category", "license", "version_check", "download"]
VALID_LICENSES = {"free", "paid"}
VALID_VERSION_METHODS = {
    "sparkle_appcast", "github_api", "json_api", "yaml_api",
    "scrape_html", "itunes_api", "redirect_trace",
    "download_and_parse_plist", "plain_text_api", "sourceforge_json",
    "git_self_update", "microsoft_autoupdate", "none", "None", "url_checksum_only",
}
VALID_DOWNLOAD_METHODS = {
    "direct_url", "redirect_url", "construct_url", "pattern",
    "app_store_only", "source_build_only", "sparkle_enclosure",
    "form_post_with_csrf", "extracted_from_json", "extracted_from_version_check",
    "yaml_field", "json_field", "web_redirect", "composed_url",
    "constructed_url", "versioned_url", "fixed_url", "direct",
    "scrape", "git_pull", "sparkle_enclosure", "mac_app_store",
    "macappstore", "dynamic_cdn_url", "construct_per_macos",
    "construct_url_or_suite", "construct_from_github_tag",
    "scrape_website", "extract_from_sparkle", "custom_api",
    "broadcom_portal_only", "url_checksum_only", "construct", "dynamic_portal_only",
}

# Download methods where MacUpdater never installs from download.url
NON_INSTALL_DOWNLOAD_METHODS = {
    "app_store_only", "source_build_only", "broadcom_portal_only",
    "dynamic_portal_only", "git_pull", "url_checksum_only",
}

# Checked against URL path/query only (not hostname — e.g. developer.android.com is fine)
_NON_MAC_PATH_MARKERS = (
    "windows", "win32", "win64", "_win_", "linux_", "_linux", "freebsd",
)

_VERSION_PLACEHOLDERS = ("{version}", "{VERSION}", "{VER}", "{TAG}")

_STALE_VERSION_RE = re.compile(
    r"(?:/v\d+\.\d+(?:\.\d+)?(?:\.\d+)?/)|(?:_\d+\.\d+(?:\.\d+)?(?:_\d+)?_(?:darwin|mac|osx|windows|linux))",
    re.IGNORECASE,
)

errors = 0
warnings = 0
count = 0


def _fail(rel: Path, msg: str) -> None:
    global errors
    errors += 1
    print(f"FAIL {rel}: {msg}")


def _warn(rel: Path, msg: str) -> None:
    global warnings
    warnings += 1
    print(f"WARN {rel}: {msg}")


def _download_url(data: dict) -> str:
    dl = data.get("download", {}) or {}
    return (dl.get("url") or "").strip()


def _url_target(url: str) -> str:
    p = urlparse(url.lower())
    return f"{p.path}?{p.query}"


def _url_is_non_mac(url: str) -> bool:
    if not url.lower().startswith("http"):
        return False
    target = _url_target(url)
    if any(m in target for m in _NON_MAC_PATH_MARKERS):
        return True
    if ".apk" in target or "_android_" in target:
        return True
    return False


def _has_version_placeholder(url: str) -> bool:
    return any(p in url for p in _VERSION_PLACEHOLDERS)


def validate_download(data: dict, rel: Path) -> None:
    if data.get("skip"):
        return

    dl = data.get("download", {}) or {}
    method = (dl.get("method") or "").lower()
    url = _download_url(data)
    file_type = (dl.get("file_type") or "").lower()

    if method in NON_INSTALL_DOWNLOAD_METHODS:
        return

    if method == "redirect_url":
        if url and _url_is_non_mac(url):
            _fail(rel, f"redirect_url points at non-macOS URL: {url[:80]}")
        return

    if not url or "{" in url:
        if data.get("auto_updates") and method in ("construct_url", "constructed_url", "direct_url"):
            _warn(rel, "auto_updates=true but download URL has no {VERSION} placeholder — version may go stale")
        return

    if _url_is_non_mac(url):
        _fail(rel, f"download URL is not for macOS: {url[:100]}")

    if (
        file_type == "dmg"
        and method in ("direct_url", "construct_url", "constructed_url", "fixed_url")
        and not url.lower().endswith((".dmg", ".pkg"))
    ):
        _warn(rel, f"file_type is dmg but URL does not end in .dmg/.pkg: {url[:80]}")

    if method in ("construct_url", "constructed_url", "versioned_url"):
        if not _has_version_placeholder(url) and _STALE_VERSION_RE.search(url):
            _warn(rel, "construct_url has hardcoded version in URL — use {VERSION} placeholder")


for d in sorted(REPO.iterdir()):
    if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
        continue
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue
        count += 1
        rel = f.relative_to(REPO)
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            _fail(rel, f"invalid JSON: {e}")
            continue

        for field in REQUIRED:
            if not data.get(field):
                _fail(rel, f"missing required field '{field}'")

        lic = data.get("license", "")
        if lic and lic not in VALID_LICENSES:
            _fail(rel, f"invalid license '{lic}'")

        vc = data.get("version_check", {})
        method = vc.get("method", "")
        if method and method not in VALID_VERSION_METHODS:
            _fail(rel, f"unknown version_check method '{method}'")

        dl = data.get("download", {})
        dl_method = dl.get("method", "")
        if dl_method and dl_method not in VALID_DOWNLOAD_METHODS:
            _fail(rel, f"unknown download method '{dl_method}'")

        slug = data.get("slug", "")
        if not slug:
            _fail(rel, "missing required field 'slug'")

        if not data.get("skip") and not data.get("bundle_id"):
            _warn(rel, "missing bundle_id — bundle-ID matching will fail")
        elif data.get("bundle_id") and not data.get("skip"):
            bid = data["bundle_id"]
            if not all(part for part in bid.split(".")):
                _warn(rel, f"bundle_id '{bid}' looks malformed (should be reverse-DNS)")

        validate_download(data, rel)

manifest_path = REPO / "manifest.json"
if manifest_path.exists():
    try:
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema_version", 0) < 3:
            _warn(Path("manifest.json"), f"schema_version {manifest.get('schema_version', 0)} is outdated")
        if manifest.get("total_profiles", 0) != count:
            _fail(Path("manifest.json"), f"total_profiles={manifest['total_profiles']} but found {count}")
    except json.JSONDecodeError as e:
        _fail(Path("manifest.json"), f"invalid JSON: {e}")
else:
    _fail(Path("manifest.json"), "manifest.json missing")

if errors:
    print(f"\n{errors} error(s), {warnings} warning(s) in {count} profile(s)")
    sys.exit(1)

print(f"OK: {count} profile(s) validated" + (f" ({warnings} warning(s))" if warnings else ""))
