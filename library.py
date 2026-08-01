"""Builds the index the offline viewer reads.

Two files are written next to Archive.html:

    library-data.js      window.LIBRARY_DATA     -- one slim row per item
    library-comments.js  window.LIBRARY_COMMENTS -- {post_id: [comment tree]}

They are split because comment trees dwarf everything else, and the viewer only
needs them once you actually open a post. They are .js rather than .json on
purpose: a page opened via file:// cannot fetch() a local JSON file (CORS), but
it can always load a script tag.

This module reads *both* schemas found on disk -- the slim records written by
the current app and the old browser extension, and the raw 107-key Reddit API
dumps written by earlier versions -- so nothing needs rewriting to be indexed.
"""

import json
import os

import thumbnails
from storage import read_json

VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")


def _is_video(filename):
    return filename.lower().endswith(VIDEO_EXTS)


def _is_media(filename):
    if thumbnails.is_thumb(filename):
        return False   # generated poster frames are not archive content
    lowered = filename.lower()
    return lowered.endswith(VIDEO_EXTS) or lowered.endswith(IMAGE_EXTS)


def _full_permalink(value):
    """Old records store a full URL, raw API dumps store a path. Accept both."""
    if not value:
        return ""
    if value.startswith("http"):
        return value
    return "https://www.reddit.com" + value


def _archived_ms(record, fallback_path=None):
    """When this item entered the archive, in epoch milliseconds."""
    for key in ("saved_at", "archived_at", "addedAt"):
        value = record.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    # Raw API dumps predate saved_at -- fall back to the file's mtime, which is
    # when we wrote it, i.e. genuinely when it was archived.
    if fallback_path:
        try:
            return int(os.path.getmtime(fallback_path) * 1000)
        except OSError:
            pass
    created = record.get("created_utc")
    return int(created * 1000) if created else 0


def _media_entries(record, dirpath, filenames):
    """Resolve a post's media, preferring what is actually on disk.

    A recorded media_files list can go stale (files moved, renamed, or a
    download interrupted), so the directory listing wins and the record is only
    used for ordering.
    """
    on_disk = sorted(f for f in filenames if _is_media(f))
    if not on_disk:
        return [], False

    recorded = [f for f in (record.get("media_files") or []) if f in on_disk]
    ordered = recorded + [f for f in on_disk if f not in recorded]

    # The old extension saved Reddit video and audio as two separate files.
    # The audio track is not something to display, but its presence tells us
    # the video is not silent.
    visual = [f for f in ordered if not f.lower().endswith("_audio.mp4")]
    has_separate_audio = any(f.lower().endswith("_audio.mp4") for f in ordered)

    entries = []
    for name in visual:
        entry = {"f": name, "v": _is_video(name)}
        if entry["v"]:
            poster = thumbnails.thumb_name(name)
            if os.path.exists(os.path.join(dirpath, poster)):
                entry["p"] = poster
        entries.append(entry)

    # Three states, not two. Raw API dumps from older versions record nothing
    # about audio, and claiming those videos "may be silent" would be a guess
    # dressed up as information -- so leave it unknown (None) and let the
    # viewer stay quiet.
    if has_separate_audio:
        has_audio = True
    elif "has_audio" in record:
        has_audio = bool(record["has_audio"])
    elif "media_files" in record:
        has_audio = False   # slim record, written by a version that tracked it
    else:
        has_audio = None    # legacy raw dump: genuinely unknown
    return entries, has_audio


def _index_post(record, dirpath, filenames, archive_dir, source, username=None):
    media, has_audio = _media_entries(record, dirpath, filenames)
    rel_dir = os.path.relpath(dirpath, archive_dir).replace("\\", "/")

    is_video = bool(record.get("is_video")) or any(m["v"] for m in media)
    comments = record.get("top_comments") or []

    row = {
        "id": record.get("id"),
        "type": "video" if is_video else "post",
        "source": source,
        "title": record.get("title") or "(untitled)",
        "author": record.get("author"),
        "subreddit": record.get("subreddit"),
        "selftext": record.get("selftext") or "",
        "permalink": _full_permalink(record.get("permalink")),
        "created_utc": record.get("created_utc"),
        "archived_ms": _archived_ms(record, os.path.join(dirpath, "metadata.json")),
        "score": record.get("score", 0),
        "num_comments": record.get("num_comments", 0),
        "dir": rel_dir,
        "media": media,
        "has_audio": has_audio,
        "nsfw": bool(record.get("over_18")),
        "ncomments": len(comments),
    }
    if username:
        row["username"] = username
    if record.get("media_error") and not media:
        row["media_error"] = record["media_error"]
    return row, comments


def _index_comment(record, path, archive_dir):
    return {
        "id": record.get("id"),
        "type": "comment",
        "source": "saved",
        "body": record.get("body", ""),
        "author": record.get("author"),
        "subreddit": record.get("subreddit"),
        "link_title": record.get("link_title", ""),
        "permalink": _full_permalink(record.get("permalink")),
        "created_utc": record.get("created_utc"),
        "archived_ms": _archived_ms(record, path),
        "score": record.get("score", 0),
        "dir": os.path.relpath(os.path.dirname(path), archive_dir).replace("\\", "/"),
        "media": [],
        "ncomments": 0,
    }


def _dedupe(rows):
    """Collapse rows that describe the same Reddit post.

    The same post legitimately lands in the archive more than once: saved by
    you *and* captured in a profile snapshot, or captured in two snapshots of
    the same user taken on different dates. Those are all one piece of content
    and should be one card.

    The copy with the most media wins (a later snapshot may have caught a
    gallery the earlier one missed), tie-broken by the most comments and then
    the most recent archive. Whichever copy wins keeps the union of the
    identifying fields, so filtering by creator still works on a row whose
    winning copy came from your saved list.
    """
    def rank(row):
        return (len(row.get("media") or []),
                row.get("ncomments") or 0,
                row.get("archived_ms") or 0)

    order = []
    position = {}   # post id -> index into `order`
    merged = 0

    for row in rows:
        key = row.get("id")
        if not key:
            order.append(row)
            continue
        if key not in position:
            position[key] = len(order)
            order.append(row)
            continue

        merged += 1
        current = order[position[key]]
        winner, loser = ((row, current) if rank(row) > rank(current)
                         else (current, row))

        # Keep creator attribution even when the saved copy wins, otherwise the
        # post disappears from that creator's sidebar filter.
        if not winner.get("username") and loser.get("username"):
            winner["username"] = loser["username"]
        if not winner.get("ncomments") and loser.get("ncomments"):
            winner["ncomments"] = loser["ncomments"]

        order[position[key]] = winner

    return order, merged


def build_index(archive_dir, log=print):
    """Walk the archive and write both index files. Returns the row count."""
    rows = []
    comment_trees = {}

    # --- Saved posts -------------------------------------------------
    saved_posts = os.path.join(archive_dir, "Saved", "Posts")
    if os.path.isdir(saved_posts):
        for dirpath, _, filenames in os.walk(saved_posts):
            if "metadata.json" not in filenames:
                continue
            record = read_json(os.path.join(dirpath, "metadata.json"))
            if not record:
                log(f"Skipping unreadable metadata in {dirpath}")
                continue
            row, comments = _index_post(
                record, dirpath, filenames, archive_dir, "saved"
            )
            rows.append(row)
            if comments:
                comment_trees[row["id"]] = comments

    # --- Saved comments ----------------------------------------------
    saved_comments = os.path.join(archive_dir, "Saved", "Comments")
    if os.path.isdir(saved_comments):
        for filename in sorted(os.listdir(saved_comments)):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(saved_comments, filename)
            record = read_json(path)
            if record:
                rows.append(_index_comment(record, path, archive_dir))

    # --- Profile snapshots -------------------------------------------
    # Layout: Profiles/<username>/<date>/<post_id>/metadata.json
    profiles = os.path.join(archive_dir, "Profiles")
    if os.path.isdir(profiles):
        for dirpath, _, filenames in os.walk(profiles):
            if "metadata.json" not in filenames:
                continue
            record = read_json(os.path.join(dirpath, "metadata.json"))
            if not record:
                continue
            # Recover the snapshotted user from the record, else from the path.
            username = record.get("username")
            if not username:
                rel = os.path.relpath(dirpath, profiles).replace("\\", "/").split("/")
                username = rel[0] if rel else None
            row, comments = _index_post(
                record, dirpath, filenames, archive_dir,
                "profile-snapshot", username=username,
            )
            rows.append(row)
            if comments:
                comment_trees[row["id"]] = comments

    rows, merged = _dedupe(rows)
    if merged:
        log(f"Merged {merged} duplicate row(s) archived in more than one place.")

    # Ordered by when the content was posted, which is what the viewer sorts and
    # groups by. Archive date is kept on each row but is no longer a sort key.
    rows.sort(key=lambda r: r.get("created_utc") or 0, reverse=True)

    data_path = os.path.join(archive_dir, "library-data.js")
    with open(data_path, "w", encoding="utf-8") as f:
        f.write("window.LIBRARY_DATA=")
        json.dump(rows, f, separators=(",", ":"), ensure_ascii=False)
        f.write(";\n")

    comments_path = os.path.join(archive_dir, "library-comments.js")
    with open(comments_path, "w", encoding="utf-8") as f:
        f.write("window.LIBRARY_COMMENTS=")
        json.dump(comment_trees, f, separators=(",", ":"), ensure_ascii=False)
        f.write(";\n")

    log(
        f"Indexed {len(rows)} items "
        f"({sum(1 for r in rows if r['type'] == 'comment')} saved comments, "
        f"{len(comment_trees)} posts with comment threads)."
    )
    return len(rows)


if __name__ == "__main__":
    import config

    settings = config.load()
    build_index(settings["archive_dir"])
