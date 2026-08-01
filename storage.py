"""Shared JSON and filesystem helpers.

These were copy-pasted into five modules; keeping one copy means a fix to the
atomic-write behaviour actually reaches everything.
"""

import json
import os
import shutil
import time

TRASH_DIRNAME = "Trash"


def read_json(path, default=None):
    """Read a JSON file, returning `default` if it is missing or corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, data, indent=2):
    """Write JSON atomically, so an interrupted write can't truncate the file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


def dir_size(path):
    """Total bytes under a directory. Missing files are skipped, not fatal."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def human_size(size):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def move_to_trash(archive_dir, relative_paths, log=print):
    """Move archive-relative paths into Archive/Trash/<timestamp>/.

    Deleting is never immediate: everything lands in a timestamped folder with
    its original structure intact, so a mistake stays recoverable until the user
    empties Trash themselves.

    Returns (moved, failed).
    """
    root = os.path.realpath(archive_dir)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    trash_root = os.path.join(archive_dir, TRASH_DIRNAME, stamp)
    moved, failed = [], []

    for relative in relative_paths:
        # Refuse anything that resolves outside the archive, or the trash itself.
        target = os.path.realpath(os.path.join(root, relative.replace("/", os.sep)))
        if (target != root and not target.startswith(root + os.sep)) \
                or relative.split("/")[0] == TRASH_DIRNAME \
                or not os.path.exists(target):
            failed.append(relative)
            continue
        destination = os.path.join(trash_root, relative.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.move(target, destination)
            moved.append(relative)
        except OSError as exc:
            log(f"  could not move {relative}: {exc}")
            failed.append(relative)

    if moved:
        log(f"Moved {len(moved)} item(s) to {TRASH_DIRNAME}/{stamp}")
    return moved, failed
