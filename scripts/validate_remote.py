#!/usr/bin/env python3
"""
Remote profile health verification.

Tests every profile's version_check URL endpoint and attempts to extract
a version string. Generates a health report identifying broken profiles.

Usage:
    python3 scripts/validate_remote.py              # Check all profiles
    python3 scripts/validate_remote.py --sample 20  # Check random sample
    python3 scripts/validate_remote.py --slug firefox  # Check single profile
    python3 scripts/validate_remote.py --report     # Output markdown report
    python3 scripts/validate_remote.py --badge      # Output JSON for shields.io

Exit code: number of broken profiles (0 = all healthy)
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO / "manifest.json"
TIMEOUT = 15
WORKERS = 8
UA = "MacUpdater-HealthCheck/2.0"
GITHUB_TOKEN = (__import__("os").environ.get("GITHUB_TOKEN") or __import__("os").environ.get("GH_TOKEN", ""))

# Methods that can't be checked remotely (require local app, auth, etc.)
SKIP_METHODS = {
    "git_self_update", "none", "None", "",
    "microsoft_autoupdate", "broadcom_portal_only", "software_update_only",
    "download_and_parse_plist",
}

# Canonical method mapping (same as MacUpdater app)
METHOD_ALIASES = {
    "electron_updater_yaml": "yaml_api", "rest_api": "json_api",
    "sparkle_xml": "sparkle_appcast", "sparkle_rss": "sparkle_appcast",
    "custom_xml_api": "sparkle_appcast", "github_api": "github_api",
    "json_api": "json_api", "yaml_api": "yaml_api",
    "scrape_html": "scrape_html", "redirect_trace": "redirect_trace",
    "plain_text_api": "plain_text_api", "sourceforge_json": "sourceforge_json",
}


def normalize_method(m: str) -> str:
    return METHOD_ALIASES.get(m, m)


def fetch(url: str, timeout: int = TIMEOUT) -> tuple[int, str]:
    """Fetch a URL. Returns (status_code, body)."""
    if not url.startswith(("http://", "https://")):
        return -1, f"Invalid URL: {url[:80]}"
    headers = {"User-Agent": UA}
    if "api.github.com" in url and GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = resp.read()
        if len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B:
            import gzip
            data = gzip.decompress(data)
        return resp.status, data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return -1, str(e)



def _normalize_extraction(raw) -> dict:
    """Convert extraction string/None shorthand to a dict the extractors can use."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("regex:"):
            return {"pattern": s[len("regex:"):]}
        if s.startswith("sparkle:"):
            return {}  # Sparkle extractor handles version fields natively
        if s in ("tag_name", "product", "version"):
            return {"json_path": s}
        # Custom shorthand — pass as pattern for scrape/plain text
        return {"pattern": s}
    return {}


def extract_version(profile: dict) -> tuple[str | None, str | None]:
    """
    Try to extract a version string from the profile's endpoint.
    Returns (version, error).
    """
    vc = profile.get("version_check", {})
    url = vc.get("url", "")
    if not url:
        return None, "No version URL"

    method = normalize_method(vc.get("method", ""))
    extraction = _normalize_extraction(vc.get("extraction"))

    code, body = fetch(url)
    if code not in (200, 301, 302):
        # Try fallback_urls when primary mirror is down.
        for alt in (vc.get("fallback_urls") or []):
            code, body = fetch(alt)
            if code in (200, 301, 302):
                url = alt
                break
        else:
            return None, f"HTTP {code}"

    if code != 200:
        return None, f"HTTP {code} (redirect) — URL may still work"

    try:
        if method in ("sparkle_appcast", "sparkle_rss", "sparkle_xml"):
            return _extract_sparkle(body)
        elif method == "github_api":
            return _extract_github(body)
        elif method in ("json_api", "itunes_api"):
            return _extract_json(body, extraction)
        elif method == "yaml_api":
            return _extract_yaml(body, extraction)
        elif method == "scrape_html":
            return _extract_html(body, extraction)
        elif method == "sourceforge_json":
            return _extract_json(body, extraction)
        elif method == "redirect_trace":
            code2, body2 = fetch(url)
            return (f"HTTP {code2}" if code2 else None, None)
        elif method == "plain_text_api":
            pattern = extraction.get("pattern")
            if pattern:
                m = re.search(pattern, body, re.MULTILINE)
                return (m.group(1) if m else None, None)
            return (body.strip().splitlines()[0], None)
        else:
            return None, f"No extractor for method: {method}"
    except Exception as e:
        return None, str(e)[:100]


def _extract_sparkle(body: str) -> tuple[str | None, str | None]:
    ns_uris = (
        "http://www.andymatuschak.org/xml-namespaces/sparkle",
        "https://sparkle-project.org/xml-namespaces/sparkle",
    )
    text = body.lstrip("\ufeff \t\r\n")
    while text.startswith("<!--"):
        endc = text.find("-->")
        if endc < 0:
            break
        text = text[endc + 3:].lstrip()
    if not (text.startswith("<?xml") or text.startswith("<rss") or text.startswith("<feed")):
        return None, "Response is not XML"
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return None, f"XML parse error: {str(e)[:80]}"

    items = root.findall("channel/item")
    if not items:
        items = [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "item"]
    if not items:
        return None, "No items in feed"

    best = None
    best_ver = None
    for item in items:
        short_ver = None
        sparkle_ver = None
        encl = item.find("enclosure")
        for uri in ns_uris:
            ns = {"sparkle": uri}
            short_ver = short_ver or item.findtext("sparkle:shortVersionString", namespaces=ns)
            sparkle_ver = sparkle_ver or item.findtext("sparkle:version", namespaces=ns)
            if encl is not None:
                short_ver = short_ver or encl.get(f"{{{uri}}}shortVersionString")
                sparkle_ver = sparkle_ver or encl.get(f"{{{uri}}}version")
        if not short_ver or not sparkle_ver:
            for el in item.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "shortVersionString" and (el.text or "").strip():
                    short_ver = short_ver or el.text.strip()
                elif tag == "version" and "}" in el.tag and (el.text or "").strip():
                    sparkle_ver = sparkle_ver or el.text.strip()
        if not short_ver and not sparkle_ver and encl is not None:
            filename = (encl.get("url") or "").rsplit("/", 1)[-1]
            m = re.search(r"(\d+(?:\.\d+)+)", filename)
            if m:
                short_ver = m.group(1)
        if not short_ver and not sparkle_ver:
            title = item.findtext("title") or ""
            m = re.search(r"(\d+(?:\.\d+)+)", title)
            if m:
                short_ver = m.group(1)
        cand = short_ver or sparkle_ver
        try:
            vt = tuple(int(x) for x in (cand or "").split("."))
        except (ValueError, TypeError):
            vt = (0,)
        if best_ver is None or vt > best_ver:
            best_ver, best = vt, cand

    return (best, None) if best else (None, "No version in Sparkle feed")



def _extract_github(body: str) -> tuple[str | None, str | None]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON from GitHub API: {str(e)[:60]}"
    # Handle array response (list of releases)
    if isinstance(data, list):
        if not data:
            return None, "Empty release list"
        data = data[0]
    if not isinstance(data, dict):
        return None, "Unexpected GitHub response type"
    tag = data.get("tag_name", "")
    ver = tag.lstrip("v").lstrip("release/")
    ver = re.sub(r"^(desktop-|Audacity-|mac-|XQuartz-)", "", ver)
    return (ver, None)


def _extract_json(body: str, extraction: dict) -> tuple[str | None, str | None]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON response: {str(e)[:60]}"
    path = extraction.get("json_path") or extraction.get("path", "version")
    # Simple JSON path traversal
    for key in path.lstrip("$").lstrip(".").split("."):
        key = key.split("[")[0]
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list):
            if key.isdigit():
                data = data[int(key)]
            elif data and isinstance(data[0], dict):
                data = data[0].get(key)
            else:
                data = data[0] if data else None
        else:
            return None, f"Path {path} not found (got {type(data).__name__})"
    return (str(data) if data is not None else None, None)


def _extract_yaml(body: str, extraction: dict) -> tuple[str | None, str | None]:
    key = extraction.get("key", "version")
    for line in body.splitlines():
        if line.strip().startswith(f"{key}:"):
            ver = line.split(":", 1)[1].strip().strip("'\"")
            return (ver, None)
    return None, f"YAML key '{key}' not found"


def _extract_html(body: str, extraction: dict) -> tuple[str | None, str | None]:
    pattern = extraction.get("pattern", r"(\d+\.\d+(?:\.\d+)?)")
    m = re.search(pattern, body)
    return (m.group(1) if m else None, None)


def check_profile(slug: str) -> dict:
    """Check a single profile. Returns health result dict."""
    path = None
    for d in REPO.iterdir():
        if d.is_dir() and d.name not in (".git", ".github", "scripts", "__pycache__"):
            pf = d / f"{slug}.json"
            if pf.exists():
                path = pf
                break
    if not path:
        return {"slug": slug, "status": "missing", "error": "Profile file not found"}

    try:
        profile = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {"slug": slug, "status": "broken", "error": f"Failed to read profile: {e}"}

    if profile.get("skip") is True:
        return {"slug": slug, "status": "skipped", "method": "skip"}

    vc = profile.get("version_check", {})
    method = normalize_method(vc.get("method", ""))

    if method in SKIP_METHODS:
        return {"slug": slug, "status": "skipped", "method": method}

    version, error = extract_version(profile)
    if version:
        return {"slug": slug, "status": "ok", "version": version, "method": method}

    err = error or "Unknown error"
    # Unauthenticated GitHub API runs hit secondary rate limits; don't count as profile breakage.
    url = (profile.get("version_check") or {}).get("url", "")
    if (
        method == "github_api"
        and "api.github.com" in url
        and (err.startswith("HTTP 403") or err.startswith("HTTP 429"))
        and not GITHUB_TOKEN
    ):
        return {
            "slug": slug,
            "status": "rate_limited",
            "error": err + " (set GITHUB_TOKEN / GH_TOKEN for accurate checks)",
            "method": method,
        }
    return {"slug": slug, "status": "broken", "error": err, "method": method}


def main():
    import argparse
    p = argparse.ArgumentParser(description="Remote profile health check")
    p.add_argument("--slug", default="", help="Check single profile by slug")
    p.add_argument("--sample", type=int, default=0, help="Check random sample of N profiles")
    p.add_argument("--offset", type=int, default=0, help="Skip first N profiles (for batch rotation)")
    p.add_argument("--submit", action="store_true", help="Submit results as anonymous gist")
    p.add_argument("--report", action="store_true", help="Output markdown report")
    p.add_argument("--badge", action="store_true", help="Output JSON for shields.io badge")
    args = p.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text())
    all_slugs = sorted(manifest["apps"].keys())

    if args.slug:
        slugs = [args.slug]
    elif args.sample:
        start = args.offset % len(all_slugs)
        batch = all_slugs[start:start + args.sample]
        if len(batch) < args.sample:
            batch += all_slugs[:args.sample - len(batch)]
        slugs = batch
    else:
        slugs = all_slugs

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(check_profile, s): s for s in slugs}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: (r.get("status", ""), r.get("slug", "")))

    # Counts
    ok = sum(1 for r in results if r["status"] == "ok")
    broken = sum(1 for r in results if r["status"] == "broken")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    rate_limited = sum(1 for r in results if r["status"] == "rate_limited")
    pct = (ok * 100 // (ok + broken)) if (ok + broken) else 0

    if args.badge:
        color = "green" if pct >= 90 else "yellow" if pct >= 70 else "red"
        badge = {
            "schemaVersion": 1,
            "label": "profile health",
            "message": f"{pct}%",
            "color": color,
        }
        print(json.dumps(badge))
        badge_path = REPO / "badges" / "health.json"
        badge_path.parent.mkdir(parents=True, exist_ok=True)
        badge_path.write_text(json.dumps(badge, indent=2) + "\n")
        return 0

    if args.report:
        print(f"# Profile Health Report\n")
        extra = f", {rate_limited} rate-limited" if rate_limited else ""
        print(f"**{ok}/{ok+broken} profiles healthy ({pct}%)** — {skipped} skipped{extra}\n")
        if broken:
            print("## Broken profiles\n")
            for r in results:
                if r["status"] == "broken":
                    print(f"- **{r['slug']}** — `{r.get('method','?')}` — {r.get('error','?')}")
        if ok > 0:
            print(f"\n## Healthy profiles ({ok})\n")
            for r in results:
                if r["status"] == "ok":
                    print(f"- {r['slug']} → `{r.get('version','?')}` ({r.get('method','?')})")
        return broken  # exit code = broken count

    # Default: print summary
    for r in results:
        if r["status"] == "ok":
            print(f"  OK     {r['slug']:30s} → {r.get('version','?'):15s} ({r.get('method','?')})")
        elif r["status"] == "broken":
            print(f"  BROKEN {r['slug']:30s} — {r.get('error','?'):40s} ({r.get('method','?')})")
        elif r["status"] == "rate_limited":
            print(f"  LIMIT  {r['slug']:30s} — {r.get('error','?'):40s} ({r.get('method','?')})")
        else:
            print(f"  SKIP   {r['slug']:30s} ({r.get('method','?')})")

    # Update manifest with health stats
    if not args.sample and not args.slug:
        manifest["health"] = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total": ok + broken,
            "ok": ok,
            "broken": broken,
            "skipped": skipped,
            "pct": pct,
        }
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"\nUpdated manifest.json with health stats")

    print(f"\n{ok} ok, {broken} broken, {skipped} skipped, {rate_limited} rate-limited — {pct}% healthy")
    return broken


if __name__ == "__main__":
    sys.exit(min(main(), 127))
