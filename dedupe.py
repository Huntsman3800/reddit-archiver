"""Store each file's bytes once, however many places reference it.

Two problems this solves:

1. Profile snapshots are per-date folders, so refreshing a creator used to
   re-download their entire history into a new dated folder. Same bytes, again,
   every refresh.
2. A post saved by you *and* captured in a profile snapshot was downloaded and
   stored twice.

Both are fixed with hard links: the new location points at the same bytes on
disk rather than a second copy. Archived media is never modified after it is
written, which is what makes this safe -- and deleting one link leaves the
others intact, so the Trash flow is unaffected.

Hard links need NTFS and a single volume. Anything else falls back to copying,
which is correct, just not free.
"""

import collections
import hashlib
import os
import shutil

MEDIA_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4v",
              ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")
SKIP_DIRS = {"Trash"}


def is_media(name):
    return os.path.splitext(name)[1].lower() in MEDIA_EXTS


def link_or_copy(src, dst):
    """Hard-link src to dst, falling back to a copy. True if it was a link."""
    if os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
        return True
    except (OSError, AttributeError, NotImplementedError):
        try:
            shutil.copy2(src, dst)
        except OSError:
            pass
        return False


def build_post_index(archive_dir, log=print):
    """Map post id -> a directory already holding that post's media.

    Built once per job so a snapshot can ask "do I already have this post?"
    without walking the archive for every single item.
    """
    index = {}
    for base in ("Saved/Posts", "Profiles"):
        root = os.path.join(archive_dir, *base.split("/"))
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            if "metadata.json" not in filenames:
                continue
            if not any(is_media(f) for f in filenames):
                continue
            post_id = os.path.basename(dirpath)
            # Prefer whichever copy has the most media, so a partial download
            # never becomes the source everything else links to.
            existing = index.get(post_id)
            if existing is None:
                index[post_id] = dirpath
            else:
                have = sum(1 for f in os.listdir(existing) if is_media(f))
                new = sum(1 for f in filenames if is_media(f))
                if new > have:
                    index[post_id] = dirpath
    return index


def reuse_existing(source_dir, target_dir, log=print):
    """Link a known post's media into a new location instead of downloading.

    Returns (filenames, bytes_saved) or ([], 0) when there is nothing to reuse.
    """
    if not source_dir or not os.path.isdir(source_dir):
        return [], 0
    try:
        names = sorted(f for f in os.listdir(source_dir) if is_media(f))
    except OSError:
        return [], 0
    if not names:
        return [], 0

    os.makedirs(target_dir, exist_ok=True)
    linked, saved = [], 0
    for name in names:
        src = os.path.join(source_dir, name)
        dst = os.path.join(target_dir, name)
        try:
            size = os.path.getsize(src)
        except OSError:
            continue
        if os.path.exists(dst):
            linked.append(name)
            continue
        if link_or_copy(src, dst):
            saved += size
        linked.append(name)
    return linked, saved


def _digest(path):
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_file(a, b):
    """Already the same bytes on disk (i.e. already hard-linked)?"""
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return (sa.st_ino != 0 and sa.st_ino == sb.st_ino
            and sa.st_dev == sb.st_dev)


def scan(archive_dir, log=print, should_stop=lambda: False):
    """Find duplicate media. Returns (groups, reclaimable_bytes, total_bytes).

    Only files of identical size can have identical content, so hashing is
    limited to those groups -- on a large archive that is the difference
    between seconds and many minutes.
    """
    by_size = collections.defaultdict(list)
    total = 0
    for dirpath, dirnames, names in os.walk(archive_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in names:
            if not is_media(name):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            total += size
            by_size[size].append(full)

    groups, reclaimable = [], 0
    for size, paths in by_size.items():
        if len(paths) < 2 or should_stop():
            continue
        by_hash = collections.defaultdict(list)
        for path in paths:
            try:
                by_hash[_digest(path)].append(path)
            except OSError:
                continue
        for same in by_hash.values():
            if len(same) < 2:
                continue
            # Count only copies that are not already sharing storage.
            keeper = same[0]
            extra = [p for p in same[1:] if not _same_file(keeper, p)]
            if extra:
                groups.append((keeper, extra, size))
                reclaimable += size * len(extra)
    return groups, reclaimable, total


def reclaim(archive_dir, groups=None, log=print, should_stop=lambda: False):
    """Replace duplicate files with hard links to a single copy.

    Every path keeps working -- the file is still there from the filesystem's
    point of view, it just no longer occupies its own blocks.
    """
    if groups is None:
        groups, _, _ = scan(archive_dir, log=log, should_stop=should_stop)

    freed = relinked = failed = 0
    for keeper, extra, size in groups:
        if should_stop():
            break
        for path in extra:
            tmp = path + ".relink"
            try:
                os.replace(path, tmp)       # keep the original until the link lands
                os.link(keeper, path)
                os.remove(tmp)
                freed += size
                relinked += 1
            except OSError:
                # Put it back exactly as it was; never lose the file.
                try:
                    if os.path.exists(tmp) and not os.path.exists(path):
                        os.replace(tmp, path)
                    elif os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                failed += 1

    log(f"Reclaimed {_human(freed)} by de-duplicating {relinked} file(s)."
        + (f" {failed} could not be linked." if failed else ""))
    return freed, relinked, failed


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


if __name__ == "__main__":
    import config

    archive = config.load()["archive_dir"]
    found, reclaimable, total = scan(archive)
    print(f"{len(found)} duplicate group(s), {_human(reclaimable)} reclaimable "
          f"of {_human(total)}")
