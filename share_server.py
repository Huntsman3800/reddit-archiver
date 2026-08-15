"""Read-only LAN sharing, so the archive can be browsed from a phone.

Deliberately narrow:

* GET and HEAD only. There is no write path at all -- nothing here can delete,
  tag, or modify anything, unlike the app's own controls.
* No access code: the URL is just the address, so it can be typed by hand or
  bookmarked on a phone. That means ANY device on the network can read the
  archive while sharing is on -- switch it off when you are not using it.
* Byte-range support, which SimpleHTTPRequestHandler does not implement.
  Without HTTP 206 a phone cannot seek within a video and Safari refuses to
  play one at all.

This is plain HTTP on a local network: fine for a home WiFi, not something to
expose to the internet or trust on a network you do not control.
"""

import mimetypes
import os
import posixpath
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 64 * 1024


def lan_ip():
    """This machine's address on the local network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no traffic is sent; just routing
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class ShareHandler(BaseHTTPRequestHandler):
    archive_dir = None
    log_fn = None

    protocol_version = "HTTP/1.1"           # keep-alive; phones open many requests
    server_version = "RedditArchiver"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    # ---- helpers ----

    def _resolve(self):
        """Map the URL to a file inside the archive, or None."""
        path = unquote(urlparse(self.path).path)
        parts = [p for p in posixpath.normpath(path).split("/")
                 if p not in ("", ".", "..")]
        if not parts:
            parts = ["Archive.html"]
        target = os.path.realpath(os.path.join(self.archive_dir, *parts))
        root = os.path.realpath(self.archive_dir)
        if target != root and not target.startswith(root + os.sep):
            return None                      # traversal attempt
        if os.path.isdir(target):
            target = os.path.join(target, "Archive.html")
        return target if os.path.isfile(target) else None

    # ---- routes ----

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _serve(self, head_only):
        path = self._resolve()
        if not path:
            self.send_error(404, "Not found")
            return

        try:
            size = os.path.getsize(path)
            handle = open(path, "rb")
        except OSError:
            self.send_error(404, "Not found")
            return

        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = self._range_for(size)

        with handle:
            if start is None:
                self.send_response(200)
                length = size
            else:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                length = end - start + 1
                handle.seek(start)

            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            # Advertising range support is what lets a phone scrub a video.
            self.send_header("Accept-Ranges", "bytes")
            if path.endswith((".js", ".html")):
                self.send_header("Cache-Control", "no-store")
            else:
                self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()

            if head_only:
                return
            remaining = length
            try:
                while remaining > 0:
                    chunk = handle.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass                          # phone navigated away mid-stream

    def _range_for(self, size):
        """Parse a Range header into (start, end), or (None, None)."""
        header = self.headers.get("Range")
        if not header or size == 0:
            return None, None
        match = RANGE_RE.search(header)
        if not match:
            return None, None
        first, last = match.group(1), match.group(2)
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        elif last:                            # suffix form: last N bytes
            start = max(0, size - int(last))
            end = size - 1
        else:
            return None, None
        if start >= size:
            return None, None
        return start, min(end, size - 1)


class ShareServer:
    """Owns the background LAN server. Off unless explicitly started."""

    def __init__(self, archive_dir, log=print, port=8778):
        self.archive_dir = archive_dir
        self.log = log
        self.port = port
        self.httpd = None
        self.thread = None

    @property
    def running(self):
        return self.httpd is not None

    def start(self):
        if self.httpd:
            return self.url()

        handler = type("BoundShareHandler", (ShareHandler,), {
            "archive_dir": self.archive_dir,
            "log_fn": staticmethod(self.log),
        })
        # 0.0.0.0 so other devices on the WiFi can reach it, not just this PC.
        self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self.url()

    def url(self):
        return f"http://{lan_ip()}:{self.port}/"

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
