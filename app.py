"""Reddit Archiver -- desktop GUI.

Threading rule for this file: Tkinter is not thread-safe, so *nothing* touches a
widget from a worker thread. Workers call self.log() / self.set_status() /
self.set_progress(), each of which marshals back to the main thread via
self.after(). The previous version called widgets directly from workers, which
is what caused the random freezes.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

import tkinter
from tkinter import filedialog, messagebox

import customtkinter as ctk

import config
import health
import library
import dedupe
import share_server
import snapshots
import thumbnails
import storage
from storage import read_json, write_json
from reddit_client import (
    BROWSERS,
    AuthError,
    RedditClient,
    normalize_comment,
    normalize_post,
)

# Shared palette with Archive.html so the two halves look like one product.
BG = "#0f0f10"
CARD = "#1a1a1b"
BORDER = "#2a2a2b"
FG = "#d7dadc"
MUTED = "#8a8a8a"
ACCENT = "#ff4500"
ACCENT_HOVER = "#e03d00"
DANGER = "#c0392b"
DANGER_HOVER = "#a93226"
OK = "#46a758"

ctk.set_appearance_mode("Dark")


def apply_window_chrome(window):
    """Give a window the app icon and a dark title bar.

    Toplevel dialogs inherit neither from their parent: without this, Settings
    and Manage archive open with a white title bar and Tk's default feather
    icon, which looks like a different program entirely.

    Both are re-applied on a short delay because customtkinter rebuilds the
    native window just after construction, discarding whatever was set first.
    """
    icon = os.path.join(config.resource_dir(), "icon.ico")

    def apply():
        try:
            if os.path.exists(icon):
                # default= registers the icon for this window *and* every
                # Toplevel created from it; the plain call covers windows that
                # already exist.
                window.iconbitmap(default=icon)
                window.iconbitmap(icon)
        except tkinter.TclError:
            pass
        try:
            window._windows_set_titlebar_color("dark")
        except (AttributeError, tkinter.TclError):
            pass   # not Windows, or an unsupported build

    apply()
    try:
        window.after(200, apply)
        window.after(600, apply)
    except tkinter.TclError:
        pass


class RedditArchiverApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Reddit Archiver")
        # Tall enough that the whole sidebar fits without scrolling; the nav
        # scrolls anyway, so a smaller window still reaches everything.
        self.geometry("1020x780")
        self.minsize(840, 520)
        self.configure(fg_color=BG)

        self.settings = config.load()
        self.is_running = False
        self.client = None
        self.username = self.settings.get("username", "")
        self.share = None

        self._set_icon()
        self._build_ui()
        config.archive_paths(self.settings["archive_dir"])
        self.log(f"Reddit Archiver {config.VERSION} ready.")
        self._check_ffmpeg()
        self.after(200, self.refresh_stats)
        self.after(300, self.authenticate_async)

    def _check_ffmpeg(self):
        """Say something at startup if ffmpeg is missing.

        Without it, videos download without sound and every video card in the
        viewer is a black rectangle -- which looks like the app is broken rather
        than a missing dependency.
        """
        found = thumbnails.ffmpeg_path(self.settings.get("ffmpeg_path", ""))
        if found:
            return
        self.log("")
        self.log("--- ffmpeg was not found ---")
        for line in thumbnails.FFMPEG_HELP.split("\n"):
            self.log(line)
        self.log("Everything else works; videos will download without sound "
                 "and show no thumbnail until ffmpeg is available.")
        self.log("")

    def _set_icon(self):
        apply_window_chrome(self)

    # ---------------- UI construction ----------------

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color=CARD)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)

        ctk.CTkLabel(
            sidebar, text="Reddit Archiver",
            font=ctk.CTkFont(size=19, weight="bold"), text_color=FG,
        ).pack(pady=(24, 2), padx=20)

        self.account_label = ctk.CTkLabel(
            sidebar, text="Not signed in",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self.account_label.pack(pady=(0, 14), padx=20)

        # Stop and Settings are pinned to the bottom BEFORE the scrolling area
        # is packed, so no number of buttons above can ever push them out of
        # view. Stop especially must always be reachable mid-job.
        self.btn_stop = ctk.CTkButton(
            sidebar, text="Stop", command=self.stop_process, state="disabled",
            fg_color=DANGER, hover_color=DANGER_HOVER, height=34,
        )
        self.btn_stop.pack(side="bottom", pady=(6, 18), padx=20, fill="x")

        self.btn_settings = ctk.CTkButton(
            sidebar, text="Settings", command=self.open_settings, height=34,
            anchor="w", fg_color="transparent", hover_color=BORDER,
            border_width=1, border_color=BORDER, text_color=FG,
        )
        self.btn_settings.pack(side="bottom", pady=(10, 0), padx=20, fill="x")

        # Everything else scrolls, so the sidebar survives a short window.
        nav = ctk.CTkScrollableFrame(sidebar, fg_color=CARD, width=214)
        nav.pack(side="top", fill="both", expand=True, padx=0, pady=0)
        sidebar = nav

        self._section(sidebar, "SAVED CONTENT")
        self.btn_sync = self._button(
            sidebar, "Sync saved content", self.start_sync, primary=True
        )
        self.btn_backfill = self._button(
            sidebar, "Backfill comments + upgrade", self.start_backfill
        )

        self._section(sidebar, "PROFILE SNAPSHOTS")
        self.profile_entry = ctk.CTkEntry(
            sidebar, placeholder_text="username to snapshot",
            fg_color=BG, border_color=BORDER, text_color=FG,
        )
        self.profile_entry.pack(pady=(0, 8), padx=20, fill="x")
        self.profile_entry.bind("<Return>", lambda _e: self.start_snapshot())
        self.btn_snapshot = self._button(
            sidebar, "Snapshot profile", self.start_snapshot
        )
        self.btn_refresh = self._button(
            sidebar, "Refresh all profiles", self.start_refresh_all
        )
        self.btn_followed = self._button(
            sidebar, "Snapshot followed users", self.start_snapshot_followed
        )

        self._section(sidebar, "LIBRARY")
        self.btn_index = self._button(sidebar, "Rebuild index", self.start_reindex)
        self.btn_thumbs = self._button(
            sidebar, "Generate thumbnails", self.start_thumbnails
        )
        self.btn_health = self._button(
            sidebar, "Check archive health", self.start_health_check
        )
        self.btn_dedupe = self._button(
            sidebar, "Reclaim duplicate space", self.start_dedupe
        )
        self.btn_manage = self._button(
            sidebar, "Manage archive", self.open_manage
        )
        self.btn_viewer = self._button(sidebar, "Open viewer", self.open_viewer)
        self.btn_share = self._button(
            sidebar, "Share on this network", self.toggle_share
        )
        self.btn_folder = self._button(sidebar, "Open archive folder", self.open_folder)

        # --- Main panel ---
        main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew", padx=24, pady=24)
        main.grid_rowconfigure(3, weight=1)
        main.grid_columnconfigure(0, weight=1)

        # A row of stats, so opening the app tells you what you have rather
        # than presenting an empty log box.
        stats = ctk.CTkFrame(main, fg_color="transparent")
        stats.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self.stat_labels = {}
        for index, (key, caption) in enumerate(
            (("posts", "posts"), ("creators", "creators"),
             ("videos", "videos"), ("size", "on disk"), ("synced", "last sync"))
        ):
            stats.grid_columnconfigure(index, weight=1)
            tile = ctk.CTkFrame(stats, fg_color=CARD, corner_radius=8)
            tile.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 8, 0))
            value = ctk.CTkLabel(
                tile, text="—", font=ctk.CTkFont(size=19, weight="bold"),
                text_color=FG,
            )
            value.pack(pady=(12, 0), padx=14)
            ctk.CTkLabel(
                tile, text=caption, font=ctk.CTkFont(size=11),
                text_color=MUTED,
            ).pack(pady=(0, 11), padx=14)
            self.stat_labels[key] = value

        self.status_label = ctk.CTkLabel(
            main, text="Ready", font=ctk.CTkFont(size=15, weight="bold"),
            text_color=FG, anchor="w",
        )
        self.status_label.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        self.progress_bar = ctk.CTkProgressBar(
            main, height=6, progress_color=ACCENT, fg_color=CARD,
        )
        self.progress_bar.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.progress_bar.set(0)

        self.log_box = ctk.CTkTextbox(
            main, state="disabled", fg_color=CARD, border_color=BORDER,
            border_width=1, text_color=FG, font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        self.log_box.grid(row=3, column=0, sticky="nsew")

    def _section(self, parent, text):
        ctk.CTkLabel(
            parent, text=text, font=ctk.CTkFont(size=10, weight="bold"),
            text_color=MUTED, anchor="w",
        ).pack(pady=(18, 6), padx=20, fill="x")

    def _button(self, parent, text, command, primary=False):
        btn = ctk.CTkButton(
            parent, text=text, command=command, height=34, anchor="w",
            fg_color=ACCENT if primary else "transparent",
            hover_color=ACCENT_HOVER if primary else BORDER,
            border_width=0 if primary else 1,
            border_color=BORDER, text_color="#ffffff" if primary else FG,
        )
        btn.pack(pady=3, padx=20, fill="x")
        return btn

    # ---------------- Thread-safe UI helpers ----------------

    def log(self, message):
        self.after(0, self._log_main, str(message))

    def _log_main(self, message):
        stamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_status(self, text):
        self.after(0, lambda: self.status_label.configure(text=text))

    def set_progress(self, value):
        self.after(0, lambda: self.progress_bar.set(max(0.0, min(1.0, value))))

    def set_account(self, text, color=MUTED):
        self.after(0, lambda: self.account_label.configure(text=text, text_color=color))

    def toggle_ui(self, running):
        self.after(0, self._toggle_ui_main, running)

    def _toggle_ui_main(self, running):
        self.is_running = running
        state = "disabled" if running else "normal"
        for btn in (
            self.btn_sync, self.btn_backfill,
            self.btn_snapshot, self.btn_refresh, self.btn_followed,
            self.btn_index, self.btn_thumbs, self.btn_health,
            self.btn_dedupe, self.btn_manage,
            self.btn_settings,
        ):
            btn.configure(state=state)
        self.profile_entry.configure(state=state)
        self.btn_stop.configure(state="normal" if running else "disabled")

    def refresh_stats(self):
        """Recompute the stat row. Sizing walks the tree, so do it off-thread."""
        threading.Thread(target=self._stats_worker, daemon=True).start()

    def _stats_worker(self):
        archive_dir = self.settings["archive_dir"]
        values = {"posts": "0", "creators": "0", "videos": "0",
                  "size": "0 B", "synced": "never"}
        try:
            rows = []
            index = os.path.join(archive_dir, "library-data.js")
            if os.path.exists(index):
                text = open(index, encoding="utf-8").read()
                start, end = text.find("["), text.rfind("]")
                if start != -1 and end != -1:
                    rows = json.loads(text[start:end + 1])
            values["posts"] = f"{sum(1 for r in rows if r.get('type') != 'comment'):,}"
            values["videos"] = f"{sum(1 for r in rows if r.get('type') == 'video'):,}"
            values["creators"] = str(len({
                r["username"] for r in rows if r.get("username")
            }))
            values["size"] = storage.human_size(storage.dir_size(archive_dir))
            last = self.settings.get("last_sync") or 0
            if last:
                values["synced"] = time.strftime("%d %b", time.localtime(last))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        self.after(0, self._apply_stats, values)

    def _apply_stats(self, values):
        for key, text in values.items():
            label = self.stat_labels.get(key)
            if label is not None:
                label.configure(text=text)

    def stop_process(self):
        self.is_running = False
        self.log("Stopping after the current item finishes...")
        self.set_status("Stopping...")

    def _should_stop(self):
        return not self.is_running

    # ---------------- Authentication ----------------

    def authenticate_async(self):
        threading.Thread(target=self._authenticate, daemon=True).start()

    def _authenticate(self):
        self.set_account("Signing in...", MUTED)
        client = RedditClient(
            cookie=self.settings.get("reddit_session", ""),
            log=self.log,
            should_stop=lambda: False,
            request_delay=self.settings.get("request_delay", 1.5),
            ffmpeg_path=thumbnails.ffmpeg_path(
                self.settings.get("ffmpeg_path", "")) or "",
        )
        try:
            username, settings, changed = client.authenticate(self.settings)
        except AuthError as exc:
            self.client = None
            self.set_account("Not signed in", DANGER)
            for line in str(exc).split("\n"):
                self.log(line)
            self.set_status("Sign-in required -- open Settings")
            return

        self.settings = settings
        self.username = username
        self.client = client
        if changed:
            config.save(self.settings)
            self.log("Saved your session so you won't need to enter it again.")
        self.set_account(f"Signed in as u/{username}", OK)
        self.set_status("Ready")

    def _require_client(self):
        if self.client is None:
            self.log("Not signed in yet. Open Settings to authenticate.")
            return False
        # Re-point the stop check at the live flag for this run.
        self.client.should_stop = self._should_stop
        return True

    # ---------------- Job plumbing ----------------

    def _start_job(self, target, *args):
        if self.is_running:
            return
        if not self._require_client():
            return
        self._toggle_ui_main(True)
        self.set_progress(0)
        threading.Thread(target=self._run_job, args=(target, args), daemon=True).start()

    def _run_job(self, target, args):
        try:
            target(*args)
        except AuthError as exc:
            self.log(f"Authentication failed: {exc}")
            self.set_account("Session expired", DANGER)
            self.set_status("Sign-in required -- open Settings")
        except Exception as exc:  # keep the GUI alive on any worker crash
            self.log(f"Unexpected error: {exc!r}")
            self.set_status("Failed -- see log")
        finally:
            try:
                # Poster frames before indexing, so freshly downloaded videos
                # get a thumbnail in the same pass. Without this they show as
                # black cards until someone happens to click "Generate
                # thumbnails" by hand. Only missing ones are processed.
                thumbnails.generate(
                    self.settings["archive_dir"], log=self.log,
                    should_stop=lambda: False,
                    configured=self.settings.get("ffmpeg_path", ""),
                )
            except Exception as exc:
                self.log(f"Thumbnail generation failed: {exc!r}")
            try:
                library.build_index(self.settings["archive_dir"], log=self.log)
            except Exception as exc:
                self.log(f"Index rebuild failed: {exc!r}")
            self.settings["last_sync"] = int(time.time())
            config.save(self.settings)
            self.toggle_ui(False)
            self.set_progress(0)
            self.refresh_stats()

    def _comments_for(self, data, post_id, source_dir):
        """Comment thread for a post, reusing the archived copy when it is safe.

        Reddit locks threads after six months, and older posts rarely gain
        comments, so re-fetching them on every refresh spends the one API call
        per post that dominates a run. Returns (comments, reused).
        """
        if not self.settings.get("fetch_comments"):
            return None, False

        threshold = self.settings.get("comment_refresh_days", 30) or 0
        if threshold and source_dir:
            created = data.get("created_utc") or 0
            age_days = (time.time() - created) / 86400 if created else 0
            if age_days > threshold:
                previous = read_json(os.path.join(source_dir, "metadata.json"))
                if previous is not None:
                    existing = previous.get("top_comments")
                    # An empty list is a real answer (the post had none), so
                    # test for presence rather than truthiness.
                    if existing is not None:
                        return existing, True

        return self.client.fetch_comments(
            post_id, limit=self.settings.get("comment_limit", 50),
            known_count=data.get("num_comments"),
        ), False

    # ---------------- Saved sync ----------------

    def start_sync(self):
        self._start_job(self.sync_saved)

    def sync_saved(self):
        paths = config.archive_paths(self.settings["archive_dir"])
        username = self.username
        self.set_status(f"Collecting saved items for u/{username}...")
        self.log(f"--- Syncing saved content for u/{username} ---")

        # Enumerate everything first. It costs ~1 request per 100 items and buys
        # a progress bar that actually means something.
        items = []
        after = None
        while self.is_running:
            page = self.client.fetch_listing(
                f"/user/{username}/saved.json", after=after
            )
            children = (page or {}).get("data", {}).get("children") or []
            if not children:
                break
            items.extend(children)
            self.set_status(f"Collecting saved items... {len(items)} found")
            after = page["data"].get("after")
            if not after:
                break

        total = len(items)
        if not total:
            self.log("No saved items returned. Is the account correct?")
            return
        self.log(f"Found {total} saved items. Downloading what's missing...")

        known = dedupe.build_post_index(paths["root"])
        new = skipped = upgraded = comments_saved = unavailable = reused = 0
        comments_reused = 0
        saved_bytes = 0
        for index, child in enumerate(items, start=1):
            if not self.is_running:
                self.log("Stopped by user.")
                break
            self.set_progress(index / total)
            data = child.get("data", {})
            kind = child.get("kind")

            if kind == "t1":
                path = os.path.join(paths["saved_comments"], f"{data['id']}.json")
                if os.path.exists(path):
                    skipped += 1
                    continue
                write_json(path, normalize_comment(data))
                comments_saved += 1
                self.set_status(f"[{index}/{total}] saved comment {data['id']}")
                continue

            if kind != "t3":
                continue

            post_id = data.get("id")
            title = (data.get("title") or "untitled")[:50]
            post_dir = os.path.join(paths["saved_posts"], post_id)
            meta_path = os.path.join(post_dir, "metadata.json")

            existing = read_json(meta_path)
            if existing is not None and "media_files" in existing:
                skipped += 1
                continue

            self.set_status(f"[{index}/{total}] {title}")
            os.makedirs(post_dir, exist_ok=True)

            if existing is not None:
                # Raw dump from an older version: the media is already on disk,
                # so only the metadata needs upgrading. No re-download.
                media = sorted(
                    f for f in os.listdir(post_dir)
                    if f != "metadata.json" and not f.endswith(".part")
                )
                has_audio = any(f.endswith("_audio.mp4") for f in media)
                media_error = existing.get("media_error")
                self.log(f"Upgrading metadata: {title}")
                upgraded += 1
            else:
                media, freed = dedupe.reuse_existing(known.get(post_id), post_dir)
                media_error = None
                if media:
                    has_audio = any(m.endswith("_audio.mp4") for m in media)
                    reused += 1
                    saved_bytes += freed
                    self.log(f"Reusing: {title}")
                else:
                    self.log(f"Archiving: {title}")
                    media, has_audio = self.client.download_media(
                        data, post_dir, post_id
                    )
                    media_error = self.client.media_error
                    if media_error:
                        unavailable += 1
                new += 1

            comments, kept = self._comments_for(
                data, post_id, known.get(post_id))
            if kept:
                comments_reused += 1

            write_json(meta_path, normalize_post(
                data, source="saved", media_files=media,
                has_audio=has_audio, top_comments=comments,
                media_error=media_error,
            ))

        self.log(
            f"Done. {new} newly archived, {upgraded} upgraded, "
            f"{comments_saved} comments saved, {skipped} already current."
        )
        if reused:
            self.log(f"{reused} post(s) reused media already in the archive "
                     f"({storage.human_size(saved_bytes)} not re-downloaded).")
        if comments_reused:
            self.log(f"{comments_reused} post(s) kept their existing comment "
                     f"threads, saving that many requests.")
        if unavailable:
            self.log(
                f"{unavailable} post(s) had media that no longer exists at the "
                f"source -- the text and metadata were still saved."
            )
        self.set_status("Sync complete")

    # ---------------- Profile snapshot ----------------

    def start_snapshot(self):
        target = self.profile_entry.get().strip().lstrip("@").replace("u/", "")
        if not target:
            self.log("Enter a username to snapshot.")
            return
        self._start_job(self.snapshot_profile, target)

    def start_snapshot_followed(self):
        self._start_job(self.snapshot_followed)

    def snapshot_followed(self):
        """Snapshot everyone you follow on Reddit, without typing any names.

        Reddit has no 'following' endpoint: following someone subscribes you to
        a hidden u_<username> subreddit, so the subscription list is the source.
        """
        self.set_status("Reading who you follow...")
        self.log("--- Snapshotting followed users ---")

        users = self.client.fetch_followed_users()
        if not users:
            self.log("No followed users found in your subscriptions.")
            self.log("Follow someone on their profile page, then try again. "
                     "(Subreddits are ignored -- only people are archived.)")
            return

        archive_dir = self.settings["archive_dir"]
        already = set(snapshots.known_users(archive_dir))
        fresh = [u for u in users if u not in already]

        self.log(f"You follow {len(users)} user(s); "
                 f"{len(fresh)} not archived yet.")
        for name in users:
            self.log(f"  u/{name}" + ("" if name in already else "   (new)"))

        if not messagebox.askyesno(
            "Snapshot followed users",
            f"Archive all {len(users)} user(s) you follow?\n\n"
            f"{len(fresh)} are new; the rest will be refreshed, which is quick "
            f"because existing media and older comment threads are reused.\n\n"
            f"You can press Stop at any point -- finished profiles are kept.",
            parent=self,
        ):
            self.log("Cancelled.")
            return

        done = failed = 0
        for index, name in enumerate(users, start=1):
            if not self.is_running:
                self.log("Stopped by user.")
                break
            self.log(f"\n=== u/{name} ({index}/{len(users)}) ===")
            try:
                self.snapshot_profile(name)
                done += 1
            except AuthError:
                raise
            except Exception as exc:
                failed += 1
                self.log(f"u/{name} failed: {exc!r}")

        self.log(f"\nFinished {done} of {len(users)} followed user(s)."
                 + (f" {failed} failed." if failed else ""))
        self.set_status(f"Snapshotted {done} followed user(s)")

    def start_refresh_all(self):
        users = snapshots.known_users(self.settings["archive_dir"])
        if not users:
            self.log("No profiles have been snapshotted yet.")
            return
        self._start_job(self.refresh_all_profiles, users)

    def refresh_all_profiles(self, users):
        """Re-snapshot every profile previously captured, diffing each one."""
        self.log(f"--- Refreshing {len(users)} profile(s): {', '.join(users)} ---")
        done = 0
        for username in users:
            if not self.is_running:
                self.log("Stopped by user.")
                break
            self.log(f"\n=== u/{username} ({done + 1}/{len(users)}) ===")
            try:
                self.snapshot_profile(username)
            except AuthError:
                raise
            except Exception as exc:
                self.log(f"u/{username} failed: {exc!r}")
            done += 1
        self.set_status(f"Refreshed {done} profile(s)")

    def snapshot_profile(self, target):
        """Archive every submission on a user's profile.

        This is the code path that was entirely broken before -- it was defined
        outside the class, so calling it raised AttributeError immediately.
        """
        archive_dir = self.settings["archive_dir"]
        date_str = time.strftime("%Y-%m-%d")
        base_dir = os.path.join(archive_dir, "Profiles", target, date_str)
        os.makedirs(base_dir, exist_ok=True)

        self.set_status(f"Snapshotting u/{target}...")
        self.log(f"--- Profile snapshot: u/{target} ({date_str}) ---")

        about = self.client.fetch_json(
            f"https://www.reddit.com/user/{target}/about.json?raw_json=1"
        )
        if about is None:
            self.log(f"Could not read u/{target}'s profile -- suspended or renamed?")
            return
        write_json(os.path.join(base_dir, "profile_meta.json"), {
            "username": target,
            "archived_at": int(time.time() * 1000),
            "about": about.get("data"),
        })

        items = []
        after = None
        while self.is_running:
            page = self.client.fetch_listing(
                f"/user/{target}/submitted.json", after=after, extra="&sort=new"
            )
            children = (page or {}).get("data", {}).get("children") or []
            if not children:
                break
            items.extend(children)
            self.set_status(f"Collecting u/{target}'s posts... {len(items)} found")
            after = page["data"].get("after")
            if not after:
                break

        total = len(items)
        if not total:
            self.log(f"u/{target} has no visible submissions.")
            return
        self.log(f"Found {total} posts. Archiving...")

        # Snapshots live in dated folders, so a refresh would otherwise
        # re-download the creator's entire history every time. Anything already
        # held elsewhere in the archive is hard-linked in instead: no request,
        # no wait, no second copy on disk.
        known = dedupe.build_post_index(archive_dir)

        new = skipped = unavailable = reused = comments_reused = 0
        saved_bytes = 0
        for index, child in enumerate(items, start=1):
            if not self.is_running:
                self.log("Stopped by user.")
                break
            self.set_progress(index / total)
            data = child.get("data", {})
            post_id = data.get("id")
            title = (data.get("title") or "untitled")[:50]

            post_dir = os.path.join(base_dir, post_id)
            meta_path = os.path.join(post_dir, "metadata.json")
            if os.path.exists(meta_path):
                skipped += 1
                continue

            self.set_status(f"[{index}/{total}] {title}")

            media, freed = dedupe.reuse_existing(known.get(post_id), post_dir)
            if media:
                has_audio = any(m.endswith("_audio.mp4") for m in media)
                reused += 1
                saved_bytes += freed
                self.log(f"Reusing: {title}")
            else:
                self.log(f"Archiving: {title}")
                os.makedirs(post_dir, exist_ok=True)
                media, has_audio = self.client.download_media(
                    data, post_dir, post_id
                )
                if media:
                    known[post_id] = post_dir

            comments, kept = self._comments_for(
                data, post_id, known.get(post_id))
            if kept:
                comments_reused += 1

            write_json(meta_path, normalize_post(
                data, source="profile-snapshot", username=target,
                media_files=media, has_audio=has_audio, top_comments=comments,
                media_error=self.client.media_error,
            ))
            if self.client.media_error:
                unavailable += 1
            new += 1

        parts = [f"{new} archived", f"{skipped} already present"]
        if reused:
            parts.append(f"{reused} reused from an existing copy "
                         f"({storage.human_size(saved_bytes)} not re-downloaded)")
        if comments_reused:
            parts.append(f"{comments_reused} kept existing comments "
                         f"(saved {comments_reused} requests)")
        self.log("Snapshot done. " + ", ".join(parts) + ".")
        if unavailable:
            self.log(f"{unavailable} post(s) had media deleted at the source.")

        # Compare against the previous snapshot of this user, if there is one.
        result = snapshots.diff(archive_dir, target, date_str)
        if result:
            snapshots.record_diff(archive_dir, target, date_str, result, self.log)
            snapshots.mark_removed(
                archive_dir, target, result["removed"], date_str, self.log
            )
        self.set_status(f"Snapshot of u/{target} complete")

    # ---------------- Backfill ----------------

    def start_backfill(self):
        self._start_job(self.backfill)

    def backfill(self):
        """Upgrade legacy raw metadata and fetch any missing comment threads.

        Anything already archived -- including everything brought over by
        migrate.py -- gets brought up to the current schema in place. Media is
        never re-downloaded.
        """
        archive_dir = self.settings["archive_dir"]
        targets = []
        for base, source in (
            (os.path.join(archive_dir, "Saved", "Posts"), "saved"),
            (os.path.join(archive_dir, "Profiles"), "profile-snapshot"),
        ):
            if not os.path.isdir(base):
                continue
            for dirpath, _, filenames in os.walk(base):
                if "metadata.json" in filenames:
                    targets.append((dirpath, source))

        total = len(targets)
        if not total:
            self.log("Nothing archived yet -- run a sync first.")
            return

        self.log(f"--- Backfilling {total} archived posts ---")
        upgraded = fetched = failed = 0

        for index, (dirpath, source) in enumerate(targets, start=1):
            if not self.is_running:
                self.log("Stopped by user.")
                break
            self.set_progress(index / total)

            meta_path = os.path.join(dirpath, "metadata.json")
            record = read_json(meta_path)
            if record is None:
                failed += 1
                continue

            post_id = record.get("id") or os.path.basename(dirpath)
            is_raw = "media_files" not in record
            needs_comments = (
                self.settings.get("fetch_comments") and not record.get("top_comments")
            )
            if not is_raw and not needs_comments:
                continue

            self.set_status(f"[{index}/{total}] {(record.get('title') or post_id)[:50]}")

            comments = record.get("top_comments")
            if needs_comments:
                comments = self.client.fetch_comments(
                    post_id, limit=self.settings.get("comment_limit", 50),
                    known_count=record.get("num_comments"),
                )
                if comments:
                    fetched += 1

            if is_raw:
                media = sorted(
                    f for f in os.listdir(dirpath)
                    if f != "metadata.json" and not f.endswith(".part")
                    and f != "profile_meta.json"
                )
                username = None
                if source == "profile-snapshot":
                    # Layout is Profiles/<user>/<date>/<id>, so the snapshotted
                    # user is two levels up from the post folder.
                    username = record.get("username") or os.path.basename(
                        os.path.dirname(os.path.dirname(dirpath))
                    )
                record = normalize_post(
                    record, source=source, username=username,
                    media_files=media,
                    has_audio=any(f.endswith("_audio.mp4") for f in media),
                    top_comments=comments,
                    saved_at=int(os.path.getmtime(meta_path) * 1000),
                )
                upgraded += 1
            else:
                record["top_comments"] = comments or []

            write_json(meta_path, record)

        self.log(
            f"Backfill done. {upgraded} records upgraded, "
            f"{fetched} comment threads fetched, {failed} unreadable."
        )
        self.set_status("Backfill complete")

    # ---------------- Library actions ----------------

    def start_reindex(self):
        self._start_local_job("Rebuilding index...", "Index rebuilt", self._reindex)

    def _reindex(self):
        library.build_index(self.settings["archive_dir"], log=self.log)

    def start_thumbnails(self):
        self._start_local_job(
            "Generating video thumbnails...", "Thumbnails up to date",
            self._thumbnails,
        )

    def _thumbnails(self):
        thumbnails.generate(
            self.settings["archive_dir"], log=self.log,
            should_stop=self._should_stop,
            configured=self.settings.get("ffmpeg_path", ""),
        )
        library.build_index(self.settings["archive_dir"], log=self.log)

    def start_health_check(self):
        self._start_local_job(
            "Checking archive...", "Health check complete", self._health_check
        )

    def _health_check(self):
        report = health.check(
            self.settings["archive_dir"], log=self.log,
            should_stop=self._should_stop,
        )
        fixable = (len(report["partials"]) + len(report["empty_files"])
                   + len(report["missing_media"]) + len(report["orphan_media"]))
        if not fixable:
            return
        if messagebox.askyesno(
            "Repair archive",
            f"Found {fixable} issue(s) that can be repaired safely.\n\n"
            "This deletes only unusable files (interrupted and zero-byte "
            "downloads) and corrects metadata to match what is on disk.\n\n"
            "Repair now?",
            parent=self,
        ):
            health.repair(
                self.settings["archive_dir"], report, log=self.log,
                should_stop=self._should_stop,
            )
            library.build_index(self.settings["archive_dir"], log=self.log)

    def start_dedupe(self):
        self._start_local_job(
            "Looking for duplicate files...", "Duplicate scan complete",
            self._dedupe,
        )

    def _dedupe(self):
        """Collapse identical files onto shared storage, with confirmation."""
        archive_dir = self.settings["archive_dir"]
        groups, reclaimable, total = dedupe.scan(
            archive_dir, log=self.log, should_stop=self._should_stop
        )
        copies = sum(len(extra) for _k, extra, _s in groups)
        if not reclaimable:
            self.log("No duplicate media found -- nothing to reclaim.")
            return
        self.log(
            f"{copies} duplicate file(s) using "
            f"{storage.human_size(reclaimable)} of "
            f"{storage.human_size(total)}."
        )
        if not messagebox.askyesno(
            "Reclaim duplicate space",
            f"Found {copies} files whose contents are already stored "
            f"elsewhere in the archive, wasting "
            f"{storage.human_size(reclaimable)}.\n\n"
            f"They can share one copy on disk instead. Every file stays exactly "
            f"where it is and opens normally -- only the duplicated bytes go "
            f"away.\n\nReclaim the space now?",
            parent=self,
        ):
            self.log("Left the archive unchanged.")
            return
        dedupe.reclaim(archive_dir, groups, log=self.log,
                       should_stop=self._should_stop)

    def _start_local_job(self, busy_text, done_text, target):
        """Run a job that needs no network, so it works without signing in."""
        if self.is_running:
            return
        self._toggle_ui_main(True)

        def run():
            try:
                self.set_status(busy_text)
                target()
                self.set_status(done_text)
            except Exception as exc:
                self.log(f"Failed: {exc!r}")
                self.set_status("Failed -- see log")
            finally:
                self.toggle_ui(False)
                self.set_progress(0)
                self.refresh_stats()

        threading.Thread(target=run, daemon=True).start()

    def open_viewer(self, launch=True):
        archive_dir = self.settings["archive_dir"]
        viewer = os.path.join(archive_dir, "Archive.html")
        # resource_dir(), NOT APP_DIR: in a packaged build APP_DIR is the folder
        # holding the .exe, while bundled files are unpacked to _MEIPASS. Using
        # APP_DIR here meant the source was never found, the copy was skipped,
        # and the viewer simply never appeared.
        source = os.path.join(config.resource_dir(), "Archive.html")
        # The viewer has to sit beside library-data.js for relative paths to
        # resolve, so keep a current copy inside the archive.
        try:
            if os.path.exists(source):
                shutil.copyfile(source, viewer)
            elif not os.path.exists(viewer):
                self.log(f"Could not find the viewer template at {source}")
        except OSError as exc:
            self.log(f"Could not update viewer: {exc}")
        if not os.path.exists(viewer):
            self.log("Archive.html is missing -- this build is incomplete. "
                     "Please report it.")
            return
        if not os.path.exists(os.path.join(archive_dir, "library-data.js")):
            self.log("No index yet -- rebuilding first.")
            library.build_index(archive_dir, log=self.log)

        # Opened straight off disk. The viewer is a plain page with no runtime
        # dependency on this app -- it keeps working if you bookmark it, copy
        # the archive to another machine, or never open the app again.
        if not launch:
            return
        webbrowser.open(f"file:///{viewer.replace(os.sep, '/')}")
        self.log("Opened the viewer in your browser.")

    def toggle_share(self):
        """Serve the archive read-only to other devices on the same network."""
        if self.share and self.share.running:
            self.share.stop()
            self.share = None
            self.btn_share.configure(
                text="Share on this network", fg_color="transparent"
            )
            self.log("Stopped sharing. The archive is no longer reachable "
                     "from other devices.")
            return

        archive_dir = self.settings["archive_dir"]
        self.open_viewer(launch=False)   # ensure Archive.html and the index exist

        if not messagebox.askyesno(
            "Share on this network",
            "This makes your archive readable by ANY device on this WiFi.\n\n"
            "There is no access code -- the address alone is enough. It is "
            "read-only, so nothing can be deleted or changed through it, but "
            "anyone on this network who opens that address can browse it.\n\n"
            "Turn it off when you are done. Start sharing?",
            parent=self,
        ):
            return

        try:
            self.share = share_server.ShareServer(archive_dir, log=self.log)
            url = self.share.start()
        except OSError as exc:
            self.share = None
            self.log(f"Could not start sharing: {exc}")
            return

        self.btn_share.configure(text="Stop sharing", fg_color=OK)
        self.log("")
        self.log("--- Sharing on your local network ---")
        self.log(f"  {url}")
        self.log("Open that on your phone while on the same WiFi. Anyone on "
                 "this network can reach it, so turn it off when you are done.")
        self.log("If the phone cannot connect, Windows Firewall is almost "
                 "always the reason -- see the README.")
        self.log("Sharing stops when you press the button again or close "
                 "the app.")
        self.log("")
        try:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.log("(the link has been copied to your clipboard)")
        except tkinter.TclError:
            pass

    def open_folder(self):
        path = self.settings["archive_dir"]
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    # ---------------- Settings dialog ----------------

    def open_settings(self):
        SettingsDialog(self)

    def open_manage(self):
        ManageDialog(self)


class SettingsDialog(ctk.CTkToplevel):
    """Settings, laid out in a scrollable body so it fits on any screen."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.geometry("580x640")
        self.configure(fg_color=BG)
        self.transient(app)
        self.grab_set()
        apply_window_chrome(self)

        settings = app.settings
        self.pad = {"padx": 20, "fill": "x"}

        body = ctk.CTkScrollableFrame(self, fg_color=BG)
        body.pack(side="top", fill="both", expand=True, padx=4, pady=(14, 0))
        self.body = body

        ctk.CTkLabel(
            body, text="Settings", font=ctk.CTkFont(size=18, weight="bold"),
            text_color=FG, anchor="w",
        ).pack(pady=(0, 16), **self.pad)

        self.archive_entry = self._folder_field(
            "Archive folder", settings["archive_dir"]
        )
        self._hint(
            "Where downloads and the viewer live. Pointing this at an existing "
            "archive picks it up as-is; nothing is moved or deleted when you "
            "switch."
        )
        self.username_entry = self._field(
            "Reddit username (auto-detected)", settings.get("username", "")
        )
        self.cookie_entry = self._field(
            "reddit_session cookie", settings.get("reddit_session", ""), show="*"
        )
        self._hint(
            "Stored in plaintext in settings.json. This is a full account "
            "credential -- don't share that file or commit it anywhere."
        )

        # --- Which browser to read cookies from ---
        self._label("Read cookies from")
        self._browser_labels = ["Auto-detect"] + list(BROWSERS.values())
        self._browser_keys = ["auto"] + list(BROWSERS.keys())
        self.browser_menu = ctk.CTkOptionMenu(
            body, values=self._browser_labels, fg_color=CARD, button_color=BORDER,
            button_hover_color=ACCENT, text_color=FG,
        )
        current = settings.get("browser", "auto")
        if current in self._browser_keys:
            self.browser_menu.set(
                self._browser_labels[self._browser_keys.index(current)]
            )
        self.browser_menu.pack(pady=(0, 6), **self.pad)
        self._hint(
            "Edge and Chrome encrypt cookies per-app since v127, so reading them "
            "needs this app run as administrator. Firefox does not."
        )

        # --- Request pace ---
        self.delay_label = ctk.CTkLabel(
            body, text="", font=ctk.CTkFont(size=12, weight="bold"),
            text_color=FG, anchor="w",
        )
        self.delay_label.pack(pady=(0, 2), **self.pad)
        self.delay_slider = ctk.CTkSlider(
            body, from_=0.5, to=5.0, number_of_steps=45,
            progress_color=ACCENT, button_color=ACCENT, command=self._delay_changed,
        )
        self.delay_slider.set(settings.get("request_delay", 1.5))
        self.delay_slider.pack(pady=(0, 4), **self.pad)
        self._delay_changed(self.delay_slider.get())
        self._hint(
            "Minimum gap between Reddit requests. Raise it if you keep hitting "
            "rate limits -- the app also slows itself down automatically."
        )

        # --- ffmpeg ---
        found = thumbnails.ffmpeg_path(settings.get("ffmpeg_path", ""))
        self.ffmpeg_entry = self._folder_field(
            "ffmpeg location (optional)", settings.get("ffmpeg_path", "")
        )
        self._hint(
            (f"Found: {found}" if found else
             "NOT FOUND — videos will download without sound and show no "
             "thumbnail. Leave blank to search PATH, or point this at "
             "ffmpeg.exe. Get it from https://www.gyan.dev/ffmpeg/builds/")
        )

        # --- comment freshness ---
        self.age_label = ctk.CTkLabel(
            body, text="", font=ctk.CTkFont(size=12, weight="bold"),
            text_color=FG, anchor="w",
        )
        self.age_label.pack(pady=(0, 2), **self.pad)
        self.age_slider = ctk.CTkSlider(
            body, from_=0, to=180, number_of_steps=36,
            progress_color=ACCENT, button_color=ACCENT, command=self._age_changed,
        )
        self.age_slider.set(settings.get("comment_refresh_days", 30))
        self.age_slider.pack(pady=(0, 4), **self.pad)
        self._age_changed(self.age_slider.get())
        self._hint(
            "Refreshing re-reads comments for recent posts only; older ones "
            "keep what is already archived. Reddit locks threads after six "
            "months, so those can never change. Set to 0 to always re-fetch."
        )

        self.comments_switch = ctk.CTkSwitch(
            body, text="Capture comment threads when archiving",
            progress_color=ACCENT, text_color=FG,
        )
        self.comments_switch.pack(pady=(0, 10), anchor="w", padx=20)
        if settings.get("fetch_comments"):
            self.comments_switch.select()

        self.status = ctk.CTkLabel(
            body, text="", font=ctk.CTkFont(size=12), text_color=MUTED,
            anchor="w", justify="left", wraplength=470,
        )
        self.status.pack(pady=(4, 10), **self.pad)

        ctk.CTkLabel(
            body, text=f"Reddit Archiver {config.VERSION}  ·  settings stored in "
                       f"{config.SETTINGS_PATH}",
            font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
            justify="left", wraplength=470,
        ).pack(pady=(6, 4), **self.pad)

        # Buttons live outside the scroll area so they are always reachable.
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(side="bottom", fill="x", padx=24, pady=16)
        ctk.CTkButton(
            row, text="Detect from browser", command=self.detect,
            fg_color="transparent", border_width=1, border_color=BORDER,
            hover_color=BORDER, text_color=FG,
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(
            row, text="Save", command=self.save,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))

    # --- small builders ---

    def _label(self, text):
        ctk.CTkLabel(
            self.body, text=text, font=ctk.CTkFont(size=12, weight="bold"),
            text_color=FG, anchor="w",
        ).pack(pady=(0, 4), **self.pad)

    def _hint(self, text):
        ctk.CTkLabel(
            self.body, text=text, font=ctk.CTkFont(size=11), text_color=MUTED,
            anchor="w", justify="left", wraplength=470,
        ).pack(pady=(0, 14), **self.pad)

    def _field(self, label, value, show=None):
        self._label(label)
        entry = ctk.CTkEntry(
            self.body, fg_color=CARD, border_color=BORDER, text_color=FG, show=show,
        )
        entry.pack(pady=(0, 12), **self.pad)
        if value:
            entry.insert(0, value)
        return entry

    def _folder_field(self, label, value):
        """A path entry with a Browse button, for people who don't type paths."""
        self._label(label)
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(pady=(0, 6), **self.pad)
        entry = ctk.CTkEntry(
            row, fg_color=CARD, border_color=BORDER, text_color=FG,
        )
        entry.pack(side="left", fill="x", expand=True)
        if value:
            entry.insert(0, value)
        ctk.CTkButton(
            row, text="Browse...", width=86, command=lambda: self._browse(entry),
            fg_color="transparent", border_width=1, border_color=BORDER,
            hover_color=BORDER, text_color=FG,
        ).pack(side="left", padx=(8, 0))
        return entry

    def _browse(self, entry):
        current = entry.get().strip()
        start = current if os.path.isdir(current) else os.path.dirname(current)
        chosen = filedialog.askdirectory(
            parent=self, title="Choose a folder for the archive",
            initialdir=start if os.path.isdir(start) else os.path.expanduser("~"),
        )
        if not chosen:
            return   # cancelled
        entry.delete(0, "end")
        entry.insert(0, os.path.normpath(chosen))
        self._describe_target(os.path.normpath(chosen))

    def _describe_target(self, path):
        """Tell the user what is already in the folder they just picked."""
        if not hasattr(self, "status"):
            return   # field built before the status line exists
        posts = os.path.join(path, "Saved", "Posts")
        profiles = os.path.join(path, "Profiles")
        existing = 0
        for base in (posts, profiles):
            if os.path.isdir(base):
                existing += sum(
                    1 for _d, _s, files in os.walk(base) if "metadata.json" in files
                )
        if existing:
            self.status.configure(
                text=f"That folder already holds {existing} archived post(s) — "
                     f"they will be picked up on the next index rebuild.",
                text_color=OK,
            )
        elif os.path.isdir(path) and os.listdir(path):
            self.status.configure(
                text="That folder is not empty. The archive will be created "
                     "inside it alongside whatever is already there.",
                text_color=MUTED,
            )
        else:
            self.status.configure(
                text="New, empty archive location.", text_color=MUTED
            )

    def _age_changed(self, value):
        days = int(round(float(value)))
        self.age_label.configure(
            text=("Re-read comments for every post"
                  if days == 0 else
                  f"Only re-read comments for posts newer than {days} days")
        )

    def _delay_changed(self, value):
        self.delay_label.configure(
            text=f"Request pace: {float(value):.1f}s between calls"
        )

    def selected_browser(self):
        label = self.browser_menu.get()
        if label in self._browser_labels:
            return self._browser_keys[self._browser_labels.index(label)]
        return "auto"

    # --- actions ---

    def detect(self):
        target = self.selected_browser()
        where = "your browsers" if target == "auto" else BROWSERS.get(target, target)
        self.status.configure(text=f"Searching {where}...", text_color=MUTED)
        threading.Thread(
            target=self._detect_worker, args=(target,), daemon=True
        ).start()

    def _detect_worker(self, target):
        client = RedditClient(log=self.app.log, should_stop=lambda: False)
        cookie, username, source = client.try_browser_cookies(target)
        if cookie:
            self.after(0, self._apply_detected, cookie, username, source)
        else:
            self.after(0, lambda: self.status.configure(
                text="No working session found -- see the log for what each "
                     "browser reported. You can paste the cookie manually above.",
                text_color=DANGER,
            ))

    def _apply_detected(self, cookie, username, source):
        self.cookie_entry.delete(0, "end")
        self.cookie_entry.insert(0, cookie)
        self.username_entry.delete(0, "end")
        self.username_entry.insert(0, username)
        self.status.configure(
            text=f"Found a valid session for u/{username} in "
                 f"{BROWSERS.get(source, source)}.",
            text_color=OK,
        )

    def save(self):
        settings = self.app.settings
        previous = settings["archive_dir"]
        archive_dir = self.archive_entry.get().strip() or config.DEFAULTS["archive_dir"]
        archive_dir = os.path.abspath(os.path.expanduser(archive_dir))

        # A bad path here would break every later operation, so fail now, in
        # front of the user, rather than on the next download.
        try:
            os.makedirs(archive_dir, exist_ok=True)
            config.archive_paths(archive_dir)
        except OSError as exc:
            messagebox.showerror(
                "Cannot use that folder",
                f"{archive_dir}\n\n{exc}\n\nPick somewhere you can write to.",
                parent=self,
            )
            return

        settings["archive_dir"] = archive_dir
        settings["username"] = self.username_entry.get().strip().replace("u/", "")
        settings["reddit_session"] = self.cookie_entry.get().strip()
        settings["fetch_comments"] = bool(self.comments_switch.get())
        settings["browser"] = self.selected_browser()
        settings["request_delay"] = round(float(self.delay_slider.get()), 1)
        settings["ffmpeg_path"] = self.ffmpeg_entry.get().strip()
        settings["comment_refresh_days"] = int(round(float(self.age_slider.get())))
        self.app.settings = config.save(settings)
        self.app.log("Settings saved.")

        moved = os.path.normcase(archive_dir) != os.path.normcase(previous)
        self.destroy()
        if moved:
            # Switching location leaves the old archive untouched; index the new
            # one so the viewer reflects what is actually there now.
            self.app.log(f"Archive folder is now {archive_dir}")
            self.app.log(f"The previous archive at {previous} was left in place.")
            self.app.start_reindex()
        self.app.authenticate_async()



class ManageDialog(ctk.CTkToplevel):
    """Browse what the archive holds and delete parts of it.

    Deleting is a filesystem operation, so it lives here rather than in the
    viewer -- and it shows sizes and counts first, so you can see what you are
    about to remove. Everything goes to Archive/Trash, never straight out.
    """

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Manage archive")
        self.geometry("640x600")
        self.configure(fg_color=BG)
        self.transient(app)
        self.grab_set()
        apply_window_chrome(self)

        ctk.CTkLabel(
            self, text="Manage archive", font=ctk.CTkFont(size=18, weight="bold"),
            text_color=FG, anchor="w",
        ).pack(pady=(18, 4), padx=22, fill="x")
        self.summary = ctk.CTkLabel(
            self, text="Measuring...", font=ctk.CTkFont(size=12),
            text_color=MUTED, anchor="w",
        )
        self.summary.pack(pady=(0, 12), padx=22, fill="x")

        self.body = ctk.CTkScrollableFrame(self, fg_color=BG)
        self.body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.trash_note = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=11), text_color=MUTED,
            anchor="w", justify="left", wraplength=580,
        )
        self.trash_note.pack(side="bottom", fill="x", padx=22, pady=(0, 2))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(side="bottom", fill="x", padx=22, pady=14)
        self.trash_btn = ctk.CTkButton(
            row, text="Nothing deleted yet", command=self.empty_trash,
            fg_color="transparent", border_width=1, border_color=BORDER,
            hover_color=DANGER, text_color=FG,
        )
        self.trash_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(
            row, text="Close", command=self.destroy,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))

        self.reload()

    # --- contents ---

    def reload(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.summary.configure(text="Measuring...")
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        """Sizing walks the whole tree, so keep it off the UI thread."""
        archive_dir = self.app.settings["archive_dir"]
        entries = []

        saved = os.path.join(archive_dir, "Saved")
        if os.path.isdir(saved):
            posts = os.path.join(saved, "Posts")
            count = len(os.listdir(posts)) if os.path.isdir(posts) else 0
            entries.append({
                "label": "Saved content", "detail": f"{count} posts",
                "path": "Saved", "size": storage.dir_size(saved), "kind": "saved",
            })

        for username in snapshots.known_users(archive_dir):
            user_dir = os.path.join(archive_dir, "Profiles", username)
            dates = snapshots.snapshot_dates(archive_dir, username)
            total = sum(
                len(snapshots.post_ids(archive_dir, username, d)) for d in dates
            )
            entries.append({
                "label": f"u/{username}",
                "detail": f"{total} posts across {len(dates)} snapshot"
                          f"{'s' if len(dates) != 1 else ''} ({', '.join(dates)})",
                "path": f"Profiles/{username}",
                "size": storage.dir_size(user_dir), "kind": "profile",
            })

        trash = os.path.join(archive_dir, storage.TRASH_DIRNAME)
        trash_size = storage.dir_size(trash) if os.path.isdir(trash) else 0
        try:
            self.after(0, self._render, entries, trash_size)
        except (RuntimeError, tkinter.TclError):
            # Dialog was closed while we were still measuring -- sizing a large
            # archive takes a moment, and there is nothing left to draw into.
            pass

    def _render(self, entries, trash_size):
        if not self.winfo_exists():
            return
        total = sum(e["size"] for e in entries)
        self.summary.configure(
            text=f"{len(entries)} section(s), {storage.human_size(total)} total"
        )
        # Say "in this archive", not "Trash" -- the previous wording read as the
        # Windows Recycle Bin, which is not where any of this goes.
        self.trash_btn.configure(
            text=f"Erase deleted items permanently ({storage.human_size(trash_size)})"
            if trash_size else "Nothing deleted yet",
            state="normal" if trash_size else "disabled",
        )
        self.trash_note.configure(
            text=(f"Deleted items are kept in {os.path.join(self.archive_dir(), storage.TRASH_DIRNAME)} "
                  f"— not the Windows Recycle Bin. Drag a folder back out to restore it.")
            if trash_size else ""
        )

        if not entries:
            ctk.CTkLabel(
                self.body, text="Nothing archived yet.", text_color=MUTED,
            ).pack(pady=20)
            return

        for entry in entries:
            card = ctk.CTkFrame(self.body, fg_color=CARD, corner_radius=8)
            card.pack(fill="x", padx=10, pady=5)
            text = ctk.CTkFrame(card, fg_color="transparent")
            text.pack(side="left", fill="both", expand=True, padx=14, pady=11)
            ctk.CTkLabel(
                text, text=entry["label"], font=ctk.CTkFont(size=13, weight="bold"),
                text_color=FG, anchor="w",
            ).pack(fill="x")
            ctk.CTkLabel(
                text, text=f"{entry['detail']}  ·  {storage.human_size(entry['size'])}",
                font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
                wraplength=420, justify="left",
            ).pack(fill="x")
            ctk.CTkButton(
                card, text="Delete", width=78,
                fg_color="transparent", border_width=1, border_color=BORDER,
                hover_color=DANGER, text_color=FG,
                command=lambda e=entry: self.delete(e),
            ).pack(side="right", padx=14)

    # --- actions ---

    def delete(self, entry):
        if not messagebox.askyesno(
            "Delete",
            f"Are you sure you want to delete {entry['label']}?\n\n"
            f"{entry['detail']}\n{storage.human_size(entry['size'])}\n\n"
            f"It will be moved to:\n"
            f"{os.path.join(self.archive_dir(), storage.TRASH_DIRNAME)}\n\n"
            f"That is a folder inside your archive, not the Windows Recycle "
            f"Bin. You can drag it back out to restore it.",
            parent=self,
        ):
            return
        archive_dir = self.app.settings["archive_dir"]
        moved, failed = storage.move_to_trash(
            archive_dir, [entry["path"]], log=self.app.log
        )
        if failed:
            messagebox.showerror(
                "Delete failed",
                f"Could not delete {entry['label']}. It may be open in another "
                f"program.", parent=self,
            )
        if moved:
            library.build_index(archive_dir, log=self.app.log)
        self.reload()

    def archive_dir(self):
        return self.app.settings["archive_dir"]

    def empty_trash(self):
        archive_dir = self.archive_dir()
        trash = os.path.join(archive_dir, storage.TRASH_DIRNAME)
        if not os.path.isdir(trash):
            return
        size = storage.dir_size(trash)
        posts = sum(
            1 for _d, _s, files in os.walk(trash) if "metadata.json" in files
        )
        if not messagebox.askyesno(
            "Permanently delete",
            f"Permanently delete {posts} archived post(s), "
            f"{storage.human_size(size)}?\n\n"
            f"{trash}\n\n"
            f"This does NOT go to the Recycle Bin — the files are erased and "
            f"cannot be recovered. Some of this content may no longer exist "
            f"anywhere else.",
            parent=self,
        ):
            return
        try:
            shutil.rmtree(trash)
            self.app.log(f"Emptied Trash ({storage.human_size(size)} freed).")
        except OSError as exc:
            messagebox.showerror("Could not empty Trash", str(exc), parent=self)
        self.reload()


if __name__ == "__main__":
    RedditArchiverApp().mainloop()
