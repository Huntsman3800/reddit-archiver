"""Bring content over from the old browser-extension archive.

The old extension wrote a flat layout with a slim JSON record per post:

    saved/posts/<id>.json      saved/posts/<id>_1.jpg
    saved/videos/<id>.json     saved/videos/<id>_video.mp4  <id>_audio.mp4
    saved/comments/<id>.json
    profiles/<user>/<date>/{profile_meta.json,posts/,videos/}

The desktop app uses a folder per post:

    Archive/Saved/Posts/<id>/metadata.json + media
    Archive/Saved/Comments/<id>.json
    Archive/Profiles/<user>/<date>/<id>/metadata.json + media

Only items missing from the new archive are brought over -- anything already
downloaded by the desktop app is left alone.

Usage:
    python migrate.py                 # dry run, shows what would happen
    python migrate.py --apply         # copy the missing items
    python migrate.py --apply --move  # move instead of copy (no duplicate GBs)
    python migrate.py --apply --repair-only   # just fix existing filenames
    python migrate.py --source "D:\\path\\to\\old"
"""

import argparse
import json
import os
import re
import shutil
import sys

import config
import library

# Reddit titles routinely contain characters the Windows console's default
# cp1252 codec cannot encode, and printing one would otherwise abort the whole
# migration. Degrade those characters instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

DEFAULT_SOURCES = [
    os.path.join(os.path.expanduser("~"), "Desktop", "Reddit"),
    os.path.join(os.getcwd(), "saved"),
]

VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4v")
MEDIA_EXTS = VIDEO_EXTS + (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")


class Stats:
    def __init__(self):
        self.migrated = 0
        self.skipped = 0
        self.bytes = 0
        self.repaired = 0
        self.failed = 0


def human(size):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def find_source(explicit=None):
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    for candidate in DEFAULT_SOURCES:
        if os.path.isdir(candidate):
            return candidate
    return None


def media_for(directory, post_id, names):
    """Media files belonging to post_id.

    Matches '<id>.' and '<id>_' only, so post 'abc12' never swallows 'abc123's
    files -- a plain prefix glob would.
    """
    out = []
    for name in names:
        if not name.startswith(post_id):
            continue
        rest = name[len(post_id):]
        if rest[:1] not in (".", "_"):
            continue
        if name.lower().endswith(".json"):
            continue
        if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
            out.append(name)
    return sorted(out)


def transfer(src, dst, move, dry):
    if dry:
        return os.path.getsize(src) if os.path.exists(src) else 0
    size = os.path.getsize(src)
    if move:
        shutil.move(src, dst)
    else:
        shutil.copy2(src, dst)
    return size


def migrate_flat_dir(old_dir, target_base, source_kind, username, stats,
                     move=False, dry=True):
    """Convert one flat old-style directory into folder-per-post."""
    if not os.path.isdir(old_dir):
        return

    names = os.listdir(old_dir)
    json_files = [n for n in names if n.lower().endswith(".json")
                  and n != "profile_meta.json"]
    if not json_files:
        return

    print(f"\n  {old_dir}  ({len(json_files)} records)")

    for json_name in sorted(json_files):
        post_id = json_name[:-5]
        target_dir = os.path.join(target_base, post_id)

        if os.path.exists(os.path.join(target_dir, "metadata.json")):
            stats.skipped += 1
            continue

        try:
            with open(os.path.join(old_dir, json_name), "r", encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"    ! unreadable {json_name}: {exc}")
            stats.failed += 1
            continue

        media = media_for(old_dir, post_id, names)

        # The old record is already in the slim schema; normalise the fields
        # the new indexer cares about without touching permalink, which is
        # ALREADY a full URL here. Re-prefixing it was the bug that produced
        # dead links in the previous migration.
        record["media_files"] = media
        record["source"] = source_kind
        record.setdefault("has_audio", any(m.endswith("_audio.mp4") for m in media))
        if username:
            record["username"] = username
        if not record.get("saved_at"):
            record["saved_at"] = int(
                os.path.getmtime(os.path.join(old_dir, json_name)) * 1000
            )

        if not dry:
            os.makedirs(target_dir, exist_ok=True)
            with open(os.path.join(target_dir, "metadata.json"), "w",
                      encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)

        for name in media:
            stats.bytes += transfer(
                os.path.join(old_dir, name),
                os.path.join(target_dir, name),
                move, dry,
            )

        stats.migrated += 1
        if stats.migrated % 250 == 0:
            print(f"    ... {stats.migrated} items ({human(stats.bytes)})")


def migrate_comments(old_dir, target_dir, stats, move=False, dry=True):
    if not os.path.isdir(old_dir):
        return
    files = [n for n in os.listdir(old_dir) if n.lower().endswith(".json")]
    if not files:
        return
    print(f"\n  {old_dir}  ({len(files)} comments)")
    if not dry:
        os.makedirs(target_dir, exist_ok=True)
    for name in sorted(files):
        target = os.path.join(target_dir, name)
        if os.path.exists(target):
            stats.skipped += 1
            continue
        stats.bytes += transfer(
            os.path.join(old_dir, name), target, move, dry
        )
        stats.migrated += 1


def repair_video_names(archive_dir, stats, dry=True):
    """Rename yt-dlp's '<id>_<title>.mp4' output to '<id>_video.mp4'.

    Earlier versions used a title-based output template, which produced very
    long filenames the viewer could not recognise as video. Detection is now by
    extension so this is cosmetic -- but it keeps filenames short and stops
    Windows path-length problems.
    """
    print("\nRepairing media filenames in the existing archive...")
    for base in ("Saved/Posts", "Profiles"):
        root = os.path.join(archive_dir, *base.split("/"))
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            if "metadata.json" not in filenames:
                continue
            post_id = os.path.basename(dirpath)
            for name in filenames:
                stem, ext = os.path.splitext(name)
                if ext.lower() not in VIDEO_EXTS:
                    continue
                if stem in (f"{post_id}_video", f"{post_id}_audio", post_id):
                    continue
                if not stem.startswith(post_id):
                    continue
                target = os.path.join(dirpath, f"{post_id}_video{ext.lower()}")
                if os.path.exists(target):
                    continue
                print(f"  {name}  ->  {post_id}_video{ext.lower()}")
                if not dry:
                    try:
                        os.rename(os.path.join(dirpath, name), target)
                    except OSError as exc:
                        print(f"    ! rename failed: {exc}")
                        stats.failed += 1
                        continue
                stats.repaired += 1

    # Clean up interrupted downloads while we're here.
    for dirpath, _, filenames in os.walk(archive_dir):
        for name in filenames:
            if name.endswith(".part") or name.endswith(".tmp"):
                print(f"  removing partial file {name}")
                if not dry:
                    try:
                        os.remove(os.path.join(dirpath, name))
                    except OSError:
                        pass


def main():
    parser = argparse.ArgumentParser(
        description="Migrate the old browser-extension archive into the desktop app."
    )
    parser.add_argument("--source", help="old archive root (contains saved/ and profiles/)")
    parser.add_argument("--apply", action="store_true",
                        help="actually perform the migration (default is a dry run)")
    parser.add_argument("--move", action="store_true",
                        help="move files instead of copying, avoiding a duplicate copy")
    parser.add_argument("--repair-only", action="store_true",
                        help="skip migration, only fix filenames in the archive")
    args = parser.parse_args()

    dry = not args.apply
    settings = config.load()
    archive_dir = settings["archive_dir"]
    paths = config.archive_paths(archive_dir)
    stats = Stats()

    print("=" * 68)
    print("Reddit Archiver -- migration" + ("  [DRY RUN]" if dry else ""))
    print("=" * 68)
    print(f"Destination: {archive_dir}")

    if not args.repair_only:
        source = find_source(args.source)
        if not source:
            print("\nNo old archive found. Pass --source with the folder that "
                  "contains 'saved' and 'profiles'.")
            return 1
        print(f"Source:      {source}")
        print(f"Mode:        {'MOVE' if args.move else 'COPY'}")

        print("\n--- Saved posts and videos ---")
        for sub in ("posts", "videos"):
            migrate_flat_dir(
                os.path.join(source, "saved", sub), paths["saved_posts"],
                "saved", None, stats, args.move, dry,
            )

        print("\n--- Saved comments ---")
        migrate_comments(
            os.path.join(source, "saved", "comments"),
            paths["saved_comments"], stats, args.move, dry,
        )

        print("\n--- Profile snapshots ---")
        profiles_root = os.path.join(source, "profiles")
        if os.path.isdir(profiles_root):
            for user in sorted(os.listdir(profiles_root)):
                user_dir = os.path.join(profiles_root, user)
                if not os.path.isdir(user_dir):
                    continue
                for date_folder in sorted(os.listdir(user_dir)):
                    date_dir = os.path.join(user_dir, date_folder)
                    if not os.path.isdir(date_dir):
                        continue
                    target_base = os.path.join(
                        paths["profiles"], user, date_folder
                    )
                    meta_src = os.path.join(date_dir, "profile_meta.json")
                    meta_dst = os.path.join(target_base, "profile_meta.json")
                    if os.path.exists(meta_src) and not os.path.exists(meta_dst):
                        if not dry:
                            os.makedirs(target_base, exist_ok=True)
                            shutil.copy2(meta_src, meta_dst)
                    for sub in ("posts", "videos"):
                        migrate_flat_dir(
                            os.path.join(date_dir, sub), target_base,
                            "profile-snapshot", user, stats, args.move, dry,
                        )

    repair_video_names(archive_dir, stats, dry)

    print("\n" + "=" * 68)
    print(f"Migrated:  {stats.migrated} items ({human(stats.bytes)})")
    print(f"Skipped:   {stats.skipped} already present")
    print(f"Repaired:  {stats.repaired} filenames")
    if stats.failed:
        print(f"Failed:    {stats.failed}")

    if dry:
        print("\nThis was a DRY RUN -- nothing was changed.")
        print("Re-run with --apply to perform it (add --move to avoid duplicating "
              "the data).")
    else:
        print("\nRebuilding the library index...")
        library.build_index(archive_dir)
        print("Done. Open the app and click 'Backfill comments + upgrade' to "
              "fetch comment threads for the migrated posts.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
