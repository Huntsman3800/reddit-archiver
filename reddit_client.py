"""Reddit API access, authentication, and media downloading.

Two things worth knowing before editing this file:

1. Reddit now returns 403 on *every* anonymous .json API request. The site
   homepage and i.redd.it images still serve fine, which makes the failure look
   like a bug rather than an auth wall. A valid reddit_session cookie is
   mandatory for all metadata, including comment threads.

2. Because of (1), any request that comes back as HTML means the cookie is dead
   -- not that the URL was wrong. fetch_json() reports it that way.
"""

import concurrent.futures
import os
import re
import time

import requests

# A real, current Chrome UA. Reddit rejects obviously-scripted user agents on
# the .json endpoints, so this is not optional cosmetics.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
TIMEOUT = 20

# browser_cookie3 loader name -> display name. Order is the auto-detect order:
# Firefox first because Chromium browsers encrypt cookies in a way that usually
# blocks extraction on Windows.
BROWSERS = {
    "firefox": "Firefox",
    "librewolf": "LibreWolf",
    "edge": "Edge",
    "chrome": "Chrome",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "chromium": "Chromium",
}


def _explain_cookie_error(exc, label):
    """Turn browser_cookie3's errors into something actionable."""
    name = type(exc).__name__
    text = str(exc)
    if "Admin" in name or "requires admin" in text.lower():
        return (
            "needs administrator rights. Since Edge/Chrome 127 the cookie "
            "database is encrypted per-app; run this app as administrator, or "
            "paste the cookie manually in Settings."
        )
    if "Could not find" in text or "Failed to find" in text:
        return "not installed, or has no profile on this machine."
    if "locked" in text.lower() or "being used" in text.lower():
        return f"database is locked -- close {label} and try again."
    return f"{name}: {text[:120]}"


class AuthError(Exception):
    """Raised when we have no usable Reddit session."""


class Throttle:
    """Adaptive pacing for Reddit's sliding-window rate limit.

    A fixed delay does not work here. Reddit's limit is a sliding window, so a
    pace that is fine for a while will eventually trip -- and then retrying at
    that same pace trips it again immediately. That thrash is what produced
    repeating 30-second stalls.

    So: slow down hard on a 429, and only creep back toward the base delay
    after a sustained run of clean requests.
    """

    MAX_DELAY = 20.0
    # A penalty that outlives the burst that caused it is worse than no penalty
    # at all: one transient blip used to leave the client crawling at 20s per
    # request for the next ~70 requests. After this long without a 429, treat
    # the incident as over and go straight back to the configured pace.
    FORGET_AFTER = 90.0

    def __init__(self, base_delay=1.5):
        self.base = max(0.2, base_delay)
        self.current = self.base
        self.clean_streak = 0
        self.last_penalty = 0.0
        self.last_request = 0.0

    def mark_request(self):
        """Record when a request actually hit the network."""
        self.last_request = time.monotonic()

    def wait_needed(self):
        """Seconds still owed before the next request.

        Pacing is measured from the previous request, not slept blindly after
        each item. Work done in between -- downloading media, writing files --
        already counts toward the interval, so a post that took 2s of real work
        owes nothing more.
        """
        if not self.last_request:
            return 0.0
        return max(0.0, self.current - (time.monotonic() - self.last_request))

    def penalize(self):
        self.current = min(self.MAX_DELAY, max(self.current * 2, self.base * 2))
        self.clean_streak = 0
        self.last_penalty = time.monotonic()
        return self.current

    def reward(self, headers=None):
        """Called after a successful request; also honours Reddit's own hints."""
        remaining = _header_float(headers, "x-ratelimit-remaining")
        reset = _header_float(headers, "x-ratelimit-reset")

        # Reddit tells us how much budget is left in the current window. When
        # it gets thin, spread the remaining requests over the time left
        # instead of sprinting into a 429.
        if remaining is not None and reset is not None and remaining >= 0:
            if remaining < 1:
                self.current = max(self.current, min(self.MAX_DELAY, reset or 5))
                return
            if remaining < 15:
                self.current = max(self.base, min(self.MAX_DELAY, reset / remaining))
                return

        if self.current <= self.base:
            return

        # Long enough since the last 429 that the penalty has served its
        # purpose -- stop punishing an otherwise healthy run.
        if time.monotonic() - self.last_penalty > self.FORGET_AFTER:
            self.current = self.base
            self.clean_streak = 0
            return

        # Otherwise halve the *excess* over the base rather than scaling the
        # whole delay, which converges in a handful of requests instead of ~70.
        self.clean_streak += 1
        if self.clean_streak >= 4:
            self.current = max(self.base, self.base + (self.current - self.base) * 0.5)
            self.clean_streak = 0


def _header_float(headers, name):
    if not headers:
        return None
    try:
        return float(headers.get(name))
    except (TypeError, ValueError):
        return None


class RedditClient:
    def __init__(self, cookie="", log=print, should_stop=lambda: False,
                 request_delay=1.5, ffmpeg_path=""):
        self.log = log
        self.should_stop = should_stop
        # Passed to yt-dlp so it can mux video+audio even when ffmpeg is not on
        # PATH. Without it, Reddit videos download silent.
        self.ffmpeg_path = ffmpeg_path
        self.throttle = Throttle(request_delay)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.cookie = cookie or ""
        # Set by download_media() when a media host refused or lost the file, so
        # the caller can record it and stop retrying content that is truly gone.
        self.media_error = None
        if self.cookie:
            self._set_cookie(self.cookie)

    def sleep(self, seconds):
        """Sleep in slices so Stop takes effect immediately.

        A single long time.sleep() made the Stop button useless during a
        rate-limit wait -- the app looked frozen for the full duration.
        """
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return True
            if self.should_stop():
                return False
            time.sleep(min(0.2, remaining))

    def pace(self):
        """Wait out whatever is still owed since the last request.

        Time spent downloading media and writing files counts toward the
        interval, so this usually sleeps for little or nothing at the default
        pace. Returns False if the user stopped.
        """
        owed = self.throttle.wait_needed()
        if owed <= 0:
            return not self.should_stop()
        return self.sleep(owed)

    def _set_cookie(self, value):
        self.session.cookies.set("reddit_session", value, domain=".reddit.com")
        self.cookie = value

    # ---------------- Authentication ----------------

    def whoami(self):
        """Return the logged-in username, or None if the session is invalid.

        This doubles as the cookie validity check -- there is no cheaper way to
        confirm a session works than asking Reddit who it belongs to.
        """
        try:
            res = self.session.get(
                "https://www.reddit.com/api/me.json?raw_json=1", timeout=TIMEOUT
            )
            if res.status_code == 200 and "application/json" in res.headers.get(
                "Content-Type", ""
            ):
                data = res.json().get("data") or {}
                name = data.get("name")
                if name:
                    return name
        except (requests.RequestException, ValueError):
            pass

        # Fallback: old.reddit.com renders the username into the page chrome
        # even when the JSON endpoints are being difficult.
        try:
            res = self.session.get("https://old.reddit.com/", timeout=TIMEOUT)
            if res.status_code == 200:
                match = re.search(
                    r'<span class="user"><a href="[^"]*?/user/([^/"]+)', res.text
                )
                if match:
                    return match.group(1)
        except requests.RequestException:
            pass

        return None

    def try_browser_cookies(self, preferred="auto"):
        """Probe browsers for a working reddit_session.

        `preferred` is 'auto' or a key from BROWSERS. When a specific browser is
        named, only that one is tried and its failure is reported in full --
        silently falling back to a different browser is exactly the behaviour
        that made it look like Edge "wasn't being detected".

        Returns (cookie, username, browser_name) or (None, None, None).
        """
        try:
            import browser_cookie3
        except ImportError:
            self.log("browser_cookie3 is not installed -- skipping auto-detect.")
            return None, None, None

        if preferred and preferred != "auto":
            order = [preferred]
        else:
            order = list(BROWSERS.keys())

        saved_cookie = self.cookie
        for key in order:
            label = BROWSERS.get(key, key)
            loader = getattr(browser_cookie3, key, None)
            if loader is None:
                self.log(f"{label}: not supported by browser_cookie3.")
                continue
            if self.should_stop():
                break

            try:
                # No leading dot: browser_cookie3 matches this as a substring,
                # and '.reddit.com' misses cookies stored as 'reddit.com'.
                jar = loader(domain_name="reddit.com")
            except Exception as exc:
                self.log(f"{label}: {_explain_cookie_error(exc, label)}")
                continue

            value = None
            for cookie in jar:
                if cookie.name == "reddit_session":
                    value = cookie.value
                    break
            if not value:
                self.log(f"{label}: no Reddit session found (not logged in?).")
                continue

            self.log(f"Found a Reddit session in {label}, verifying...")
            self._set_cookie(value)
            username = self.whoami()
            if username:
                self.log(f"Verified: logged in as u/{username} (via {label}).")
                return value, username, key
            self.log(f"{label}: that session was expired or invalid.")

        # Nothing worked -- restore whatever we started with.
        self.cookie = saved_cookie
        self.session.cookies.clear()
        if saved_cookie:
            self._set_cookie(saved_cookie)
        return None, None, None

    def authenticate(self, settings):
        """Establish a working session, reusing saved credentials when possible.

        Order: saved cookie -> browser auto-detect. Returns
        (username, updated_settings_dict, changed) so the caller can persist.
        """
        changed = False

        if settings.get("reddit_session"):
            self._set_cookie(settings["reddit_session"])
            self.log("Checking saved session...")
            username = self.whoami()
            if username:
                self.log(f"Signed in as u/{username}.")
                if settings.get("username") != username:
                    settings["username"] = username
                    changed = True
                return username, settings, changed
            self.log("Saved session has expired.")

        preferred = settings.get("browser", "auto")
        if preferred and preferred != "auto":
            self.log(f"Looking for a Reddit session in {BROWSERS.get(preferred, preferred)}...")
        else:
            self.log("Looking for a Reddit session in your browsers...")
        cookie, username, source = self.try_browser_cookies(preferred)
        if cookie:
            settings["reddit_session"] = cookie
            settings["username"] = username
            settings["cookie_source"] = source
            return username, settings, True

        raise AuthError(
            "No working Reddit session found.\n"
            "Log into reddit.com in Firefox, or paste your reddit_session "
            "cookie in Settings."
        )

    # ---------------- Core fetching ----------------

    def fetch_json(self, url, retries=3, rate_limit_retries=4):
        """GET a Reddit .json endpoint, handling rate limits and dead sessions.

        Gives up on an individual URL rather than blocking forever. A single
        skipped item is recoverable -- just re-run; an indefinite hang is not.
        """
        attempt = 0
        throttled = 0
        while not self.should_stop():
            # Honour whatever interval is still owed from the previous call,
            # so pacing is enforced per API request rather than per item.
            if not self.pace():
                return None
            self.throttle.mark_request()
            try:
                res = self.session.get(url, timeout=TIMEOUT)
            except requests.RequestException as exc:
                attempt += 1
                if attempt > retries:
                    self.log(f"Network error, giving up on {url}: {exc}")
                    return None
                wait = 2 ** attempt
                self.log(f"Network error, retrying in {wait}s...")
                if not self.sleep(wait):
                    return None
                continue

            if res.status_code == 429:
                throttled += 1
                slowed = self.throttle.penalize()
                if throttled > rate_limit_retries:
                    self.log(
                        f"Still rate limited after {rate_limit_retries} tries -- "
                        f"skipping this item and moving on."
                    )
                    return None
                # Reddit's retry-after is occasionally minutes long; cap it so
                # the run stays responsive, and slow the overall pace instead.
                wait = min(_header_float(res.headers, "retry-after") or slowed, 60.0)
                self.log(
                    f"Rate limited ({throttled}/{rate_limit_retries}) -- waiting "
                    f"{wait:.0f}s, pace now {slowed:.1f}s between requests."
                )
                if not self.sleep(wait):
                    return None
                continue

            self.throttle.reward(res.headers)

            if res.status_code in (401, 403):
                raise AuthError(
                    "Reddit rejected the request (HTTP %d). Your session cookie "
                    "is expired or invalid -- re-authenticate in Settings."
                    % res.status_code
                )

            if res.status_code == 404:
                return None

            if "application/json" not in res.headers.get("Content-Type", ""):
                # Reddit served HTML. With modern Reddit this always means the
                # session is dead, never that the path was wrong.
                raise AuthError(
                    "Reddit returned a web page instead of data. Your session "
                    "cookie is no longer valid."
                )

            if res.status_code != 200:
                self.log(f"API error {res.status_code}: {url}")
                return None

            try:
                return res.json()
            except ValueError:
                self.log(f"Could not parse response from {url}")
                return None
        return None

    def fetch_listing(self, path, after=None, limit=100, extra=""):
        url = f"https://www.reddit.com{path}?limit={limit}&raw_json=1{extra}"
        if after:
            url += f"&after={after}"
        return self.fetch_json(url)

    def fetch_followed_users(self):
        """Usernames you follow, from your subscription list.

        Following someone on Reddit subscribes you to a hidden subreddit named
        u_<username>, which is why followed people appear alongside subreddits
        on old.reddit.com/subreddits. There is no separate "following" API, so
        the subscription listing is the source, filtered down to user profiles.

        Returns a sorted list of usernames.
        """
        users = []
        after = None
        pages = 0
        while not self.should_stop() and pages < 30:
            page = self.fetch_listing(
                "/subreddits/mine/subscriber.json", after=after
            )
            children = (page or {}).get("data", {}).get("children") or []
            if not children:
                break
            for child in children:
                data = child.get("data") or {}
                name = _username_from_subreddit(data)
                if name:
                    users.append(name)
            after = page["data"].get("after")
            pages += 1
            if not after:
                break

        # Reddit occasionally lists the same profile twice across pages.
        return sorted(set(users), key=str.lower)

    # ---------------- Comments ----------------

    def fetch_comments(self, post_id, limit=50, depth=5, known_count=None):
        """Return a normalized, nested comment tree for a post.

        Shape matches what the old browser extension wrote, so migrated and
        newly-synced posts render identically in the viewer.

        `known_count` is the post's num_comments. When Reddit already told us
        the post has no comments, the request is skipped -- comment fetching is
        one request per post, and this is the cheapest way to spend fewer of
        them against the rate limit.
        """
        if known_count == 0:
            return []

        data = self.fetch_json(
            f"https://www.reddit.com/comments/{post_id}.json"
            f"?limit={limit}&depth={depth}&raw_json=1"
        )
        if not isinstance(data, list) or len(data) < 2:
            return []

        def walk(listing):
            out = []
            children = (listing or {}).get("data", {}).get("children", []) or []
            for child in children:
                if child.get("kind") != "t1":
                    continue  # skip 'more' stubs and anything non-comment
                c = child.get("data", {})
                if c.get("body") is None:
                    continue
                replies = c.get("replies")
                out.append(
                    {
                        "id": c.get("id"),
                        "author": c.get("author"),
                        "body": c.get("body", ""),
                        "score": c.get("score", 0),
                        "created_utc": c.get("created_utc"),
                        "replies": walk(replies) if isinstance(replies, dict) else [],
                    }
                )
            return out

        return walk(data[1])

    # ---------------- Media ----------------

    def _download_file(self, url, filepath):
        try:
            with self.session.get(url, stream=True, timeout=TIMEOUT) as res:
                if res.status_code != 200:
                    return False
                tmp = filepath + ".part"
                with open(tmp, "wb") as f:
                    for chunk in res.iter_content(64 * 1024):
                        if self.should_stop():
                            f.close()
                            os.remove(tmp)
                            return False
                        f.write(chunk)
                os.replace(tmp, filepath)
                return True
        except (requests.RequestException, OSError) as exc:
            self.log(f"  download failed: {exc}")
            return False

    def _download_parallel(self, jobs, out_dir, workers=6):
        """Fetch several CDN files at once. Returns filenames in gallery order.

        Order matters -- the viewer shows a gallery in the order Reddit lists
        it -- so results are re-sorted by index rather than by completion.
        """
        if len(jobs) == 1:
            idx, url, name = jobs[0]
            return [name] if self._download_file(url, os.path.join(out_dir, name)) else []

        done = {}

        def run(job):
            idx, url, name = job
            if self.should_stop():
                return
            if self._download_file(url, os.path.join(out_dir, name)):
                done[idx] = name

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(workers, len(jobs))
        ) as pool:
            list(pool.map(run, jobs))

        return [done[i] for i in sorted(done)]

    def download_media(self, item, out_dir, post_id):
        """Fetch a post's media into out_dir.

        Returns (media_files, has_audio). Filenames follow the convention the
        viewer relies on:
            single image   <id>.<ext>
            gallery        <id>_1.jpg, <id>_2.jpg, ...
            video          <id>_video.mp4   (audio already muxed in)
        """
        url = (item.get("url_overridden_by_dest") or item.get("url") or "").strip()
        media_files = []
        has_audio = False
        self.media_error = None

        # --- Galleries first: a gallery's `url` is a /gallery/ page, so this
        # must be checked before the plain-image branch. ---
        metadata = item.get("media_metadata")
        if metadata:
            order = []
            gallery_data = item.get("gallery_data") or {}
            for entry in gallery_data.get("items", []) or []:
                media_id = entry.get("media_id")
                if media_id in metadata:
                    order.append(media_id)
            if not order:
                order = list(metadata.keys())

            jobs = []
            for idx, media_id in enumerate(order, start=1):
                info = metadata.get(media_id) or {}
                if info.get("status") != "valid":
                    continue
                source = info.get("s") or {}
                img_url = source.get("u") or source.get("gif") or ""
                img_url = img_url.replace("&amp;", "&")
                if not img_url:
                    continue
                ext = _clean_ext(img_url, default="jpg")
                jobs.append((idx, img_url, f"{post_id}_{idx}.{ext}"))

            # Gallery images come from Reddit's CDN, which is not the rate-limited
            # API, so fetching a few at once is both safe and much faster than
            # walking a 20-image gallery one file at a time.
            if jobs:
                media_files = self._download_parallel(jobs, out_dir)
            if media_files:
                return media_files, False

        # --- Direct images ---
        lowered = url.lower().split("?")[0]
        if lowered.endswith(IMAGE_EXTS) or "i.redd.it" in url:
            ext = _clean_ext(url, default="jpg")
            name = f"{post_id}.{ext}"
            if self._download_file(url, os.path.join(out_dir, name)):
                return [name], False

        # --- Video / everything else, via yt-dlp ---
        if url and not item.get("is_self"):
            name, has_audio, error = self._download_video(url, out_dir, post_id)
            if name:
                return [name], has_audio
            if error:
                self.media_error = error
                # Reddit often keeps its own copy of an externally-hosted video.
                # When the original host has removed it, this is the last chance
                # to save anything.
                fallback = (
                    ((item.get("preview") or {}).get("reddit_video_preview") or {})
                    .get("fallback_url")
                )
                if fallback:
                    self.log("  trying Reddit's mirrored copy instead...")
                    name = f"{post_id}_video.mp4"
                    if self._download_file(
                        fallback.replace("&amp;", "&"), os.path.join(out_dir, name)
                    ):
                        self.media_error = None
                        return [name], True

        return media_files, has_audio

    def _download_video(self, url, out_dir, post_id):
        """Download via yt-dlp, muxing audio into a single <id>_video.mp4.

        Returns (filename, has_audio, error). `error` is a short reason string
        when a media-hosting URL failed, and None for ordinary links yt-dlp is
        right to refuse (news articles, text posts and so on).
        """
        try:
            import yt_dlp
        except ImportError:
            return None, False, "yt-dlp is not installed"

        target = os.path.join(out_dir, f"{post_id}_video")
        opts = {
            "outtmpl": target + ".%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "merge_output_format": "mp4",
            "ignoreerrors": True,
            "retries": 2,
            "socket_timeout": TIMEOUT,
        }
        if self.ffmpeg_path:
            opts["ffmpeg_location"] = self.ffmpeg_path
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:
            return None, False, self._classify_media_error(url, str(exc))

        if not info:
            return None, False, self._classify_media_error(url, "no media found")

        has_audio = _info_has_audio(info)

        # yt-dlp may land on any extension; find whatever it actually wrote.
        for candidate in sorted(os.listdir(out_dir)):
            if candidate.startswith(f"{post_id}_video."):
                if not candidate.endswith(".mp4"):
                    final = os.path.join(out_dir, f"{post_id}_video.mp4")
                    try:
                        os.replace(os.path.join(out_dir, candidate), final)
                        return f"{post_id}_video.mp4", has_audio, None
                    except OSError:
                        return candidate, has_audio, None
                return candidate, has_audio, None
        return None, False, self._classify_media_error(url, "nothing was written")

    def _classify_media_error(self, url, message):
        """Explain a media failure, and stay quiet about non-media links.

        Silence here was the whole problem: redgifs videos that had been deleted
        upstream looked identical to ones that were never attempted.
        """
        host_is_media = any(
            h in url.lower() for h in
            ("redgifs", "v.redd.it", "gfycat", "imgur", "streamable",
             "youtube.com", "youtu.be", "gifs.com", "vimeo")
        )
        if not host_is_media:
            return None   # an article or text link; nothing was expected

        lowered = message.lower()
        if "410" in message or "gone" in lowered:
            reason = "deleted by the uploader (HTTP 410)"
        elif "404" in message or "not found" in lowered:
            reason = "no longer exists (HTTP 404)"
        elif "403" in message or "forbidden" in lowered:
            reason = "access denied (HTTP 403)"
        elif "private" in lowered:
            reason = "private"
        elif "timed out" in lowered or "timeout" in lowered:
            reason = "timed out"
        else:
            reason = message.split("\n")[0][:110]
        self.log(f"  media unavailable: {reason}")
        return reason


def _username_from_subreddit(data):
    """Pull a username out of a subreddit listing entry, or None.

    A followed person appears as a subreddit named u_<username>. Reddit is not
    consistent about which fields it fills in, so check several: subreddit_type
    is the most reliable signal, the u_ prefix is the fallback, and the url
    (/user/<name>/) preserves the real capitalisation.
    """
    if not isinstance(data, dict):
        return None

    display = data.get("display_name") or ""
    is_user_profile = (
        data.get("subreddit_type") == "user" or display.startswith("u_")
    )
    if not is_user_profile:
        return None

    url = data.get("url") or ""
    match = re.search(r"/user/([^/]+)/?", url)
    if match:
        return match.group(1)
    if display.startswith("u_"):
        return display[2:]
    return None


def _clean_ext(url, default="jpg"):
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    if not ext.isalnum() or len(ext) > 4:
        return default
    return ext


def _info_has_audio(info):
    """Best-effort check for an audio stream in a yt-dlp info dict."""
    if info.get("acodec") not in (None, "none"):
        return True
    for fmt in info.get("requested_formats") or []:
        if fmt.get("acodec") not in (None, "none"):
            return True
    return False


def normalize_post(item, source, username=None, saved_at=None,
                   media_files=None, has_audio=False, top_comments=None,
                   media_error=None):
    """Reduce a raw Reddit API post to the slim record we store on disk.

    The raw API blob is ~107 keys and 7.5 KB per post; this keeps the ~15 that
    the viewer actually uses, and matches the schema the old browser extension
    wrote so both generations of data index identically.
    """
    permalink = item.get("permalink", "")
    if permalink and not permalink.startswith("http"):
        permalink = "https://www.reddit.com" + permalink

    record = {
        "id": item.get("id"),
        "title": item.get("title"),
        "author": item.get("author"),
        "subreddit": item.get("subreddit"),
        "selftext": item.get("selftext") or None,
        "url": item.get("url"),
        "permalink": permalink,
        "created_utc": item.get("created_utc"),
        "score": item.get("score", 0),
        "num_comments": item.get("num_comments", 0),
        "over_18": item.get("over_18", False),
        "is_video": bool(item.get("is_video")) or item.get("post_hint") == "hosted:video",
        "media_files": media_files or [],
        "has_audio": has_audio,
        "saved_at": saved_at or int(time.time() * 1000),
        "source": source,
    }
    if username:
        record["username"] = username
    if top_comments is not None:
        record["top_comments"] = top_comments
    if media_error:
        record["media_error"] = media_error
    return record


def normalize_comment(item, saved_at=None):
    permalink = item.get("permalink", "")
    if permalink and not permalink.startswith("http"):
        permalink = "https://www.reddit.com" + permalink
    return {
        "id": item.get("id"),
        "body": item.get("body", ""),
        "author": item.get("author"),
        "subreddit": item.get("subreddit"),
        "link_title": item.get("link_title", ""),
        "permalink": permalink,
        "created_utc": item.get("created_utc"),
        "score": item.get("score", 0),
        "saved_at": saved_at or int(time.time() * 1000),
        "source": "saved",
    }
