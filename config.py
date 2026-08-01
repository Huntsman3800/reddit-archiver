"""Persistent settings for Reddit Archiver.

Everything the user would otherwise retype each launch lives in settings.json
next to this file: the archive location, their Reddit username, and the
reddit_session cookie.

Note on the cookie: it is stored in plaintext by explicit user choice. An
active reddit_session is a full account credential -- keep settings.json out of
any backup or repo you share. SETTINGS_PATH is git-ignored for that reason.
"""

import json
import os
import sys

def _app_dir():
    """Where settings and the default archive live.

    Critical for the packaged build: under PyInstaller one-file, __file__ points
    into a temp folder that is deleted on exit, so anything written relative to
    it disappears between runs. Frozen builds anchor to the .exe instead, and
    fall back to LOCALAPPDATA when that location is read-only (Program Files, a
    read-only drive, and so on).
    """
    if getattr(sys, "frozen", False):
        beside_exe = os.path.dirname(os.path.abspath(sys.executable))
        probe = os.path.join(beside_exe, ".write-test")
        try:
            with open(probe, "w"):
                pass
            os.remove(probe)
            return beside_exe
        except OSError:
            fallback = os.path.join(
                os.environ.get("LOCALAPPDATA")
                or os.path.expanduser("~/.local/share"),
                "RedditArchiver",
            )
            os.makedirs(fallback, exist_ok=True)
            return fallback
    return os.path.dirname(os.path.abspath(__file__))


def resource_dir():
    """Where bundled read-only files (Archive.html, the icon) actually live.

    PyInstaller unpacks those into _MEIPASS, which is NOT where settings go.
    """
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


APP_DIR = _app_dir()
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

# Anything faster than this trips Reddit's rate limiter within a few hundred
# requests once comment fetching is enabled.
MIN_SAFE_DELAY = 1.0

DEFAULTS = {
    "archive_dir": os.path.join(APP_DIR, "Archive"),
    "username": "",
    "reddit_session": "",
    "cookie_source": "",       # which browser the cookie was auto-loaded from
    "browser": "auto",         # 'auto' or a browser_cookie3 loader name
    "fetch_comments": True,    # capture comment threads on saved posts
    "comment_limit": 50,       # top-level comments to keep per post
    # Starting gap between API calls. This is a floor, not a fixed pace: the
    # client slows down on its own when Reddit pushes back. Values below ~1.0
    # reliably trip the rate limiter once comment fetching is on.
    "request_delay": 1.5,
    "ffmpeg_path": "",         # blank = find it on PATH
    "last_sync": 0,
}

VERSION = "1.0.1"


def load():
    """Read settings.json, filling in any missing keys with defaults."""
    data = dict(DEFAULTS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable settings should never stop the app from
            # starting -- fall back to defaults and let it rewrite on save.
            pass

    # Older versions shipped a 0.6s default that reliably trips Reddit's rate
    # limiter once comment fetching is on, and a settings.json written back then
    # would keep that value forever. Pull it up to something survivable.
    try:
        if float(data["request_delay"]) < MIN_SAFE_DELAY:
            data["request_delay"] = DEFAULTS["request_delay"]
    except (TypeError, ValueError):
        data["request_delay"] = DEFAULTS["request_delay"]

    return data


def save(data):
    """Write settings.json atomically so a crash mid-write can't corrupt it."""
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)
    return merged


def archive_paths(archive_dir):
    """The canonical folder layout, created on demand."""
    paths = {
        "root": archive_dir,
        "saved_posts": os.path.join(archive_dir, "Saved", "Posts"),
        "saved_comments": os.path.join(archive_dir, "Saved", "Comments"),
        "profiles": os.path.join(archive_dir, "Profiles"),
    }
    for key, path in paths.items():
        if key != "root":
            os.makedirs(path, exist_ok=True)
    return paths
