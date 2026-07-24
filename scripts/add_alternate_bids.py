#!/usr/bin/env python3
"""Add known direct-vs-MAS / legacy alternate_bundle_ids."""
from __future__ import annotations

import json
import os
import plistlib
import glob
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Well-known alternate IDs (direct primary stays bundle_id).
ALTS: dict[str, list[str]] = {
    "messaging/whatsapp.json": [
        "net.whatsapp.WhatsApp",  # Homebrew / current desktop
        "desktop.WhatsApp",  # older desktop builds
    ],
    "browsers/duckduckgo.json": [
        "com.duckduckgo.mobile.ios",  # iOS-on-Mac wrapper
    ],
    "messaging/microsoft-teams.json": [
        "com.microsoft.teams",  # classic Teams (pre-teams2)
    ],
    "password-managers/bitwarden.json": [
        "com.8bit.bitwarden",  # Mac App Store
    ],
    "password-managers/1password.json": [
        "com.agilebits.onepassword7",
        "com.agilebits.onepassword4",
    ],
    "vpn-security/tailscale.json": [
        "io.tailscale.ipn.macsys",  # direct PKG (already may be set)
    ],
    "notes-writing/notion.json": [
        # brew quit id; keep if profile uses different primary
    ],
}


def installed_bids() -> dict[str, str]:
    out = {}
    for app in glob.glob("/Applications/*.app"):
        info = os.path.join(app, "Contents", "Info.plist")
        if not os.path.isfile(info):
            continue
        with open(info, "rb") as fh:
            pl = plistlib.load(fh)
        bid = pl.get("CFBundleIdentifier")
        if bid:
            out[os.path.basename(app)[:-4]] = bid
    return out


def main() -> None:
    installed = installed_bids()
    print("installed sample:", {k: installed[k] for k in installed if any(x in k.lower() for x in ("whats", "duck", "team", "bitward", "1pass", "tail", "notion", "discord"))})

    for rel, alts in ALTS.items():
        path = REPO / rel
        if not path.exists() or not alts:
            continue
        data = json.loads(path.read_text())
        primary = data.get("bundle_id")
        merged = []
        for a in list(data.get("alternate_bundle_ids") or []) + alts:
            if a and a != primary and a not in merged:
                merged.append(a)
        data["alternate_bundle_ids"] = merged
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"{rel}: primary={primary} alts={merged}")


if __name__ == "__main__":
    main()
