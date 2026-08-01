"""Poster frames for archived videos.

Video cards in the viewer use preload="none" -- without it, a page with a
hundred videos on it makes the browser open a hundred connections and stall. The
cost is that the browser paints nothing at all until you click, which is why
video cards showed up as solid black.

So each video gets a real JPEG poster extracted with ffmpeg, stored next to it
as <video-stem>.thumb.jpg.
"""

import os
import shutil
import subprocess

THUMB_SUFFIX = ".thumb.jpg"
VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4v")

# Seek a little way in first: the opening frame of a video is very often black
# or a fade-in, which is exactly the useless thumbnail we're trying to avoid.
SEEK_ATTEMPTS = (2.0, 0.5, 0.0)


FFMPEG_HELP = (
    "ffmpeg is required for video thumbnails and for merging video with audio.\n"
    "Download the Windows build from https://www.gyan.dev/ffmpeg/builds/ "
    "(the 'essentials' release is enough), unzip it, and either add its bin "
    "folder to PATH or point Settings at ffmpeg.exe."
)


def ffmpeg_path(configured=""):
    """Locate ffmpeg: an explicit setting first, then PATH, then common spots.

    Returns None when it genuinely cannot be found. The app is expected to keep
    working without it -- only thumbnails and audio muxing are affected -- but
    it must say so rather than silently producing black video cards.
    """
    if configured:
        if os.path.isfile(configured):
            return configured
        # Tolerate someone pointing at the folder rather than the binary.
        candidate = os.path.join(configured, "ffmpeg.exe")
        if os.path.isfile(candidate):
            return candidate

    found = shutil.which("ffmpeg")
    if found:
        return found

    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
        r"C:\ffmpeg",
    ):
        if not base:
            continue
        for candidate in (
            os.path.join(base, "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(base, "ffmpeg.exe"),
        ):
            if os.path.isfile(candidate):
                return candidate
    return None


def thumb_name(video_filename):
    return os.path.splitext(video_filename)[0] + THUMB_SUFFIX


def is_thumb(filename):
    return filename.lower().endswith(THUMB_SUFFIX)


def _extract(ffmpeg, video_path, out_path, width=480):
    for seek in SEEK_ATTEMPTS:
        try:
            result = subprocess.run(
                [
                    ffmpeg, "-v", "error", "-ss", str(seek), "-i", video_path,
                    "-frames:v", "1", "-vf", f"scale={width}:-2",
                    "-q:v", "4", "-y", out_path,
                ],
                capture_output=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode == 0 and os.path.exists(out_path) \
                and os.path.getsize(out_path) > 0:
            return True
        # A seek past the end of a very short clip fails; the next, earlier
        # attempt will succeed.
        if os.path.exists(out_path) and os.path.getsize(out_path) == 0:
            try:
                os.remove(out_path)
            except OSError:
                pass
    return False


def generate(archive_dir, log=print, should_stop=lambda: False, force=False,
             configured=""):
    """Create any missing poster frames. Returns (created, failed, skipped)."""
    ffmpeg = ffmpeg_path(configured)
    if not ffmpeg:
        for line in FFMPEG_HELP.split("\n"):
            log(line)
        return 0, 0, 0

    targets = []
    for dirpath, dirnames, filenames in os.walk(archive_dir):
        # Deleted items live in Trash; don't spend time on them.
        dirnames[:] = [d for d in dirnames if d != "Trash"]
        for name in filenames:
            if is_thumb(name) or not name.lower().endswith(VIDEO_EXTS):
                continue
            if name.lower().endswith("_audio.mp4"):
                continue   # the separate audio track from old-format videos
            poster = os.path.join(dirpath, thumb_name(name))
            if force or not os.path.exists(poster):
                targets.append((os.path.join(dirpath, name), poster))

    if not targets:
        return 0, 0, 0

    log(f"Generating {len(targets)} video thumbnail(s)...")
    created = failed = 0
    for index, (video, poster) in enumerate(targets, start=1):
        if should_stop():
            log("Thumbnail generation stopped.")
            break
        if _extract(ffmpeg, video, poster):
            created += 1
        else:
            failed += 1
            log(f"  could not read a frame from {os.path.basename(video)}")
        if index % 25 == 0:
            log(f"  ...{index}/{len(targets)}")

    log(f"Thumbnails: {created} created" + (f", {failed} failed" if failed else "."))
    return created, failed, 0


if __name__ == "__main__":
    import config

    generate(config.load()["archive_dir"])
