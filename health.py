"""Archive integrity checking.

Finds the ways an archive quietly rots: interrupted downloads, metadata that
points at files which are no longer there, media with no metadata beside it, and
posts whose media never downloaded at all.

check() only reports. repair() performs the fixes that cannot lose anything --
deleting zero-byte and .part leftovers, and dropping stale filenames out of
metadata. Orphaned media is reported but never deleted automatically, because an
orphan is usually a file whose metadata went missing, not junk.
"""

import os

import thumbnails
from storage import read_json, write_json

MEDIA_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4v",
              ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")
KNOWN_JSON = {"metadata.json", "profile_meta.json", "snapshot_diff.json"}


def _post_dirs(archive_dir):
    for base in ("Saved/Posts", "Profiles"):
        root = os.path.join(archive_dir, *base.split("/"))
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            if "metadata.json" in filenames:
                yield dirpath, filenames


def check(archive_dir, log=print, should_stop=lambda: False):
    """Scan the archive. Returns a report dict; changes nothing."""
    report = {
        "posts": 0,
        "partials": [],        # .part / .tmp leftovers
        "empty_files": [],     # zero-byte media
        "missing_media": [],   # metadata lists a file that isn't there
        "orphan_media": [],    # media with no metadata.json beside it
        "no_media": [],        # post has no media and no explanation
        "media_errors": [],    # media known to have failed, with the reason
        "unreadable": [],      # corrupt metadata.json
        "missing_posters": 0,
    }

    for dirpath, filenames in _post_dirs(archive_dir):
        if should_stop():
            break
        report["posts"] += 1
        meta_path = os.path.join(dirpath, "metadata.json")
        record = read_json(meta_path)
        if record is None:
            report["unreadable"].append(_rel(archive_dir, meta_path))
            continue

        on_disk = set()
        for name in filenames:
            full = os.path.join(dirpath, name)
            if name.endswith(".part") or name.endswith(".tmp"):
                report["partials"].append(_rel(archive_dir, full))
                continue
            if os.path.splitext(name)[1].lower() not in MEDIA_EXTS:
                continue
            if thumbnails.is_thumb(name):
                continue
            try:
                if os.path.getsize(full) == 0:
                    report["empty_files"].append(_rel(archive_dir, full))
                    continue
            except OSError:
                continue
            on_disk.add(name)

        listed = [f for f in (record.get("media_files") or [])
                  if not thumbnails.is_thumb(f)]
        for name in listed:
            if name not in on_disk:
                report["missing_media"].append(
                    {"post": _rel(archive_dir, dirpath), "file": name}
                )

        if listed:
            for name in on_disk:
                if name not in listed:
                    report["orphan_media"].append(
                        {"post": _rel(archive_dir, dirpath), "file": name}
                    )

        if record.get("media_error"):
            report["media_errors"].append({
                "post": _rel(archive_dir, dirpath),
                "reason": record["media_error"],
                "url": record.get("url", ""),
            })
        elif not on_disk and not record.get("selftext"):
            report["no_media"].append(_rel(archive_dir, dirpath))

        for name in on_disk:
            if name.lower().endswith((".mp4", ".webm", ".mkv", ".mov", ".m4v")) \
                    and not name.lower().endswith("_audio.mp4") \
                    and not os.path.exists(
                        os.path.join(dirpath, thumbnails.thumb_name(name))):
                report["missing_posters"] += 1

    # Stray files loose in the archive that belong to no post at all.
    for base in ("Saved/Posts", "Profiles"):
        root = os.path.join(archive_dir, *base.split("/"))
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            if "metadata.json" in filenames:
                continue
            for name in filenames:
                if name in KNOWN_JSON:
                    continue
                if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                    report["orphan_media"].append({
                        "post": _rel(archive_dir, dirpath),
                        "file": name,
                        "no_metadata": True,
                    })

    _log_report(report, log)
    return report


def _rel(archive_dir, path):
    return os.path.relpath(path, archive_dir).replace("\\", "/")


def _log_report(r, log):
    log(f"Checked {r['posts']} archived post(s).")
    rows = [
        ("interrupted downloads (.part/.tmp)", len(r["partials"])),
        ("zero-byte media files", len(r["empty_files"])),
        ("metadata pointing at missing files", len(r["missing_media"])),
        ("media files not listed in metadata", len(r["orphan_media"])),
        ("posts with no media and no text", len(r["no_media"])),
        ("posts whose media failed to download", len(r["media_errors"])),
        ("unreadable metadata.json", len(r["unreadable"])),
        ("videos still missing a poster frame", r["missing_posters"]),
    ]
    clean = True
    for label, count in rows:
        if count:
            clean = False
            log(f"  {count:>5}  {label}")
    if clean:
        log("  No problems found.")
    else:
        log("  'Repair' removes partial and zero-byte files and corrects "
            "metadata. Nothing else is deleted.")


def repair(archive_dir, report=None, log=print, should_stop=lambda: False):
    """Apply only the fixes that cannot lose data."""
    if report is None:
        report = check(archive_dir, log=lambda _m: None)

    removed = 0
    for rel in report["partials"] + report["empty_files"]:
        if should_stop():
            break
        path = os.path.join(archive_dir, rel.replace("/", os.sep))
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass

    # Reconcile metadata with what is actually on disk, both directions.
    touched = 0
    affected = {entry["post"] for entry in
                report["missing_media"] + report["orphan_media"]
                if not entry.get("no_metadata")}
    for rel in affected:
        if should_stop():
            break
        dirpath = os.path.join(archive_dir, rel.replace("/", os.sep))
        meta_path = os.path.join(dirpath, "metadata.json")
        record = read_json(meta_path)
        if record is None:
            continue
        try:
            names = os.listdir(dirpath)
        except OSError:
            continue
        actual = sorted(
            n for n in names
            if os.path.splitext(n)[1].lower() in MEDIA_EXTS
            and not thumbnails.is_thumb(n)
            and os.path.getsize(os.path.join(dirpath, n)) > 0
        )
        if actual != (record.get("media_files") or []):
            record["media_files"] = actual
            write_json(meta_path, record)
            touched += 1

    log(f"Repair: removed {removed} unusable file(s), "
        f"corrected {touched} metadata record(s).")
    return removed, touched


if __name__ == "__main__":
    import config

    check(config.load()["archive_dir"])
