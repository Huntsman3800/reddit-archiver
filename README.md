<div align="center">

<img src="docs/icon.png" width="120" alt="Reddit Archiver">

# Reddit Archiver

**Keep the Reddit posts you care about — after they're deleted.**

Archives your saved posts and comments, snapshots entire user profiles, and gives
you a fast offline viewer for everything it has collected.

[![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows&logoColor=white)](#install)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](#run-from-source)
[![License](https://img.shields.io/badge/License-MIT-ff4500)](LICENSE)
[![Download](https://img.shields.io/badge/Download-.exe-46a758)](../../releases/latest)

</div>

---

## Why

Creators delete their accounts. Subreddits go private. Media hosts purge files.
Your saved list quietly fills with `[removed]`.

This keeps a local copy — text, images, video, and full comment threads — and
tells you when something you archived has since vanished from Reddit.

## Features

|  | |
|---|---|
| **Saved sync** | Every saved post and comment, incrementally. Re-running skips what's current instead of re-downloading. |
| **Profile snapshots** | Archive a user's entire submission history into a dated folder. Snapshot again later and keep both. |
| **Deletion detection** | Each snapshot is diffed against the last. Posts that disappeared from Reddit are flagged — your copy is now the only one. |
| **Comment threads** | Full nested threads, collapsible in the viewer. |
| **Offline viewer** | A single HTML file. No server, no build step, no internet. |
| **Media handling** | Galleries, Reddit video with audio muxed in, and external hosts via yt-dlp. Failures are reported, not swallowed. |
| **Health checks** | Finds interrupted downloads, orphaned files, and metadata that drifted from disk. Repairs only what can't lose data. |
| **Safe deletes** | Everything goes to a recoverable folder first. Nothing is erased without a second, explicit step. |

## Install

### Download the app

Grab `RedditArchiver.exe` from the [latest release](../../releases/latest) and
run it. No Python required, no console window, no installer.

> [!NOTE]
> Windows SmartScreen may warn on first run because the executable isn't code
> signed — signing certificates cost money. Choose **More info → Run anyway**,
> or build it yourself from source below.

### Install ffmpeg

Needed for video sound and for the thumbnails the viewer shows on video cards.
**The app runs fine without it** and says so at startup, but videos download
silent and their cards stay blank.

1. Download a build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) — the
   *essentials* release is enough
2. Unzip it anywhere
3. Either add its `bin` folder to `PATH`, or point **Settings → ffmpeg location**
   at `ffmpeg.exe`

The app also checks `Program Files\ffmpeg\bin`, `%LOCALAPPDATA%\ffmpeg` and
`C:\ffmpeg` automatically.

### Run from source

```bash
pip install -r requirements.txt
python app.py
```

## Signing in

**You shouldn't have to type anything.** On launch the app finds a
`reddit_session` cookie in your browser, verifies it against Reddit, and
remembers both the cookie and your username.

<details>
<summary><b>If auto-detect fails</b></summary>

<br>

Since v127, Chrome and Edge encrypt their cookie database per-application.
Reading it raises *"This operation requires admin"*. Options:

- **Use Firefox** — no such restriction, works out of the box
- **Run as administrator** — lets it read Chrome/Edge
- **Paste it manually** — Settings → `reddit_session` cookie, once

Settings also has a **browser selector**. Naming a browser explicitly makes the
app use only that one and report exactly why it failed, instead of quietly
falling back to another.

</details>

> [!IMPORTANT]
> **Reddit returns HTTP 403 for every anonymous `.json` request.** A valid
> session is mandatory for all metadata, including comments. If you see
> *"Reddit returned a web page instead of data"*, your cookie has expired —
> that's almost never a bug in the app.

## Usage

| Button | What it does |
|---|---|
| **Sync saved content** | Downloads anything missing from your saved list |
| **Backfill comments + upgrade** | Fetches comment threads for what's already archived. Never re-downloads media |
| **Snapshot profile** | Archives one user's full submission history |
| **Refresh all profiles** | Re-snapshots every creator you've captured, diffing each |
| **Rebuild index** | Regenerates the viewer's data files |
| **Generate thumbnails** | Extracts poster frames for videos missing one |
| **Check archive health** | Scans for problems, repairs safely on your confirmation |
| **Manage archive** | Shows what's stored, with sizes — and deletes parts of it |
| **Open viewer** | Opens the offline viewer in your browser |

### Viewer

Everything is ordered by **when the content was posted**, never by when you
archived it.

- Group by month, year, author, subreddit, or type
- Filter by source, type, or creator; search titles, text, subreddits, authors
- Star favourites and apply your own tags
- **Blur NSFW** toggle and **Shuffle** for random browsing
- `←` `→` between media in a post · `↑` `↓` between posts · `Esc` closes ·
  `/` focuses search

Posts whose media has since been deleted show the reason rather than an empty
card. The text and metadata are still there.

## Rate limiting

Reddit enforces a *sliding window* limit, so a fixed delay can't work: a pace
that runs fine for a few hundred requests eventually trips, and retrying at that
same pace trips it again — which looks like a 30-second stall repeating forever.

<details>
<summary><b>How the client paces itself</b></summary>

<br>

- **Request pace** in Settings is a floor, not a fixed rate (default 1.5s)
- On a 429 it **doubles** the gap, up to 20s, and only eases back after a
  sustained clean run
- It reads Reddit's `x-ratelimit-remaining` / `x-ratelimit-reset` headers and
  spreads the remaining budget over the window
- After 4 failed attempts it **skips that item and continues** rather than
  blocking — re-run later to pick up what was skipped
- All waits are interruptible, so **Stop takes effect immediately**

Hitting limits often? Raise the pace slider. Turning off comment capture roughly
halves the request count, since it costs one request per post.

</details>

## Storage

```
Archive/
├── Saved/
│   ├── Posts/<post_id>/metadata.json + media
│   └── Comments/<comment_id>.json
├── Profiles/<username>/<YYYY-MM-DD>/
│   ├── profile_meta.json
│   ├── snapshot_diff.json
│   └── <post_id>/metadata.json + media
├── Trash/<timestamp>/          ← deleted items, recoverable
├── library-data.js
├── library-comments.js
└── Archive.html
```

**Settings → Archive folder** moves this anywhere. Switching is
non-destructive — the old location is left untouched, and pointing back at it
later picks it up unchanged.

> [!WARNING]
> `Trash/` is **not** the Windows Recycle Bin. Deleted items stay inside your
> archive and keep using disk space. Drag a folder back out to restore it, or use
> **Erase deleted items permanently** to reclaim the space — that step really is
> irreversible.

<details>
<summary><b>Media filename convention</b></summary>

<br>

| Kind | Filename |
|---|---|
| Single image | `<id>.jpg` |
| Gallery | `<id>_1.jpg`, `<id>_2.jpg`, … |
| Video | `<id>_video.mp4` (audio muxed in) |
| Poster frame | `<id>_video.thumb.jpg` (generated) |

Video is detected by extension rather than filename, so oddly-named files from
older versions still work. Cards show the generated poster instead of a `<video>`
element — a grid of video tags either stalls the browser or paints solid black.

</details>

## Migrating from the browser extension

The old extension used a flat layout; this uses a folder per post. Migration only
brings over what's **missing** from your current archive.

```bash
python migrate.py            # dry run — reports, changes nothing
python migrate.py --apply    # do it
python migrate.py --apply --move   # move instead of copy, no duplicate GBs
```

Then run **Backfill comments + upgrade** to pull comment threads for everything
that came over.

## Building the .exe yourself

```bash
pip install pyinstaller
pyinstaller RedditArchiver.spec
```

Produces `dist/RedditArchiver.exe`. Pushing a `v*` tag builds and publishes it
automatically via [GitHub Actions](.github/workflows/build.yml).

Settings and the default archive live beside the `.exe`, not in PyInstaller's
temp unpack folder, so they persist between runs. If the `.exe` sits somewhere
read-only, both fall back to `%LOCALAPPDATA%\RedditArchiver`.

ffmpeg isn't bundled: it's ~80 MB, carries its own licence terms, and the app
degrades gracefully without it.

## Project layout

| File | Role |
|---|---|
| `app.py` | The GUI — every button lives here |
| `reddit_client.py` | Auth, API calls, rate limiting, downloads |
| `library.py` | Builds the viewer's index |
| `config.py` | Settings and path resolution |
| `storage.py` | Shared JSON, sizing, and trash helpers |
| `thumbnails.py` | ffmpeg poster-frame extraction |
| `snapshots.py` | Snapshot history and diffing |
| `health.py` | Integrity checks and safe repair |
| `migrate.py` | One-off import from the old extension |
| `Archive.html` | The offline viewer |
| `tools/make_icon.py` | Regenerates `icon.ico` — build-time only |

## Licence

[MIT](LICENSE) © Huntsman3800

<div align="center">
<sub>Not affiliated with Reddit. Archive responsibly — respect creators' wishes
about their work.</sub>
</div>
