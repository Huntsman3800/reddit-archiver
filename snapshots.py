"""Profile snapshot history and diffing.

Each snapshot of a user is a dated folder, so the archive naturally holds a
timeline. Comparing a fresh snapshot against the previous one answers the
question this archive exists to answer: *what has this creator deleted?*

A post present in an earlier snapshot but absent from a newer one is gone from
Reddit. The archived copy is now the only copy, so it gets flagged rather than
quietly sitting there looking like every other post.
"""

import os
import time

from storage import read_json, write_json

DIFF_FILENAME = "snapshot_diff.json"


def profiles_root(archive_dir):
    return os.path.join(archive_dir, "Profiles")


def known_users(archive_dir):
    """Every user who has ever been snapshotted, most recently first."""
    root = profiles_root(archive_dir)
    if not os.path.isdir(root):
        return []
    users = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        dates = snapshot_dates(archive_dir, name)
        if dates:
            users.append((name, dates[-1]))
    users.sort(key=lambda pair: pair[1], reverse=True)
    return [name for name, _ in users]


def snapshot_dates(archive_dir, username):
    """Dated snapshot folders for a user, oldest first."""
    user_dir = os.path.join(profiles_root(archive_dir), username)
    if not os.path.isdir(user_dir):
        return []
    dates = [
        d for d in os.listdir(user_dir)
        if os.path.isdir(os.path.join(user_dir, d))
    ]
    return sorted(dates)


def post_ids(archive_dir, username, date):
    """Post ids captured in one snapshot."""
    snap = os.path.join(profiles_root(archive_dir), username, date)
    if not os.path.isdir(snap):
        return set()
    return {
        name for name in os.listdir(snap)
        if os.path.isfile(os.path.join(snap, name, "metadata.json"))
    }


def diff(archive_dir, username, new_date, previous_date=None):
    """Compare a snapshot against the one before it.

    Returns a dict with 'added' and 'removed' id lists, or None when there is
    nothing to compare against.
    """
    dates = snapshot_dates(archive_dir, username)
    if new_date not in dates:
        return None
    if previous_date is None:
        earlier = [d for d in dates if d < new_date]
        if not earlier:
            return None
        previous_date = earlier[-1]

    before = post_ids(archive_dir, username, previous_date)
    after = post_ids(archive_dir, username, new_date)
    if not before:
        return None

    return {
        "username": username,
        "compared_at": int(time.time() * 1000),
        "previous": previous_date,
        "current": new_date,
        "added": sorted(after - before),
        "removed": sorted(before - after),
        "unchanged": len(before & after),
    }


def mark_removed(archive_dir, username, removed_ids, seen_date, log=print):
    """Flag archived copies of posts that have disappeared from Reddit.

    The flag goes on every earlier snapshot that still holds the post, since
    that is where the surviving copy actually lives.
    """
    if not removed_ids:
        return 0
    marked = 0
    stamp = int(time.time() * 1000)
    for date in snapshot_dates(archive_dir, username):
        if date >= seen_date:
            continue
        for post_id in removed_ids:
            meta = os.path.join(
                profiles_root(archive_dir), username, date, post_id, "metadata.json"
            )
            record = read_json(meta)
            if record is None or record.get("gone_from_reddit"):
                continue
            record["gone_from_reddit"] = True
            record["gone_noticed_at"] = stamp
            write_json(meta, record)
            marked += 1
    if marked:
        log(f"  flagged {marked} archived post(s) as deleted from Reddit")
    return marked


def record_diff(archive_dir, username, new_date, result, log=print):
    """Persist a diff next to the snapshot it describes."""
    if not result:
        return
    snap = os.path.join(profiles_root(archive_dir), username, new_date)
    os.makedirs(snap, exist_ok=True)
    write_json(os.path.join(snap, DIFF_FILENAME), result)

    added, removed = len(result["added"]), len(result["removed"])
    if added or removed:
        log(
            f"  vs {result['previous']}: {added} new, {removed} no longer on Reddit"
        )
    else:
        log(f"  vs {result['previous']}: no changes")
