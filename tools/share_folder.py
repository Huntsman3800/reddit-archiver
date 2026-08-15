"""Share any folder read-only over your local network.

Standalone on purpose -- it imports nothing from Reddit Archiver, so you can
copy this one file anywhere and run it against any directory.

    python share_folder.py "D:\\some folder"
    python share_folder.py "D:\\some folder" --port 9000
    python share_folder.py "D:\\some folder" --no-token     # no access code

It prints a URL. Open that on your phone while on the same WiFi.

Why not `python -m http.server`? That has no byte-range support, so phones
cannot seek within a video and Safari often refuses to play one at all. This
also adds an access code, a browsable gallery view, and refuses anything but
GET/HEAD.

Plain HTTP on a local network: fine at home, not something to expose to the
internet or use on a network you do not control. Ctrl+C stops it.
"""

import argparse
import html
import json
import mimetypes
import os
import posixpath
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

COOKIE = "folder_share"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 64 * 1024
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".flv", ".avi"}

PAGE_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0f0f10;color:#d7dadc;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;background:rgba(15,15,16,.95);
 backdrop-filter:blur(10px);border-bottom:1px solid #2a2a2b;padding:14px 16px;z-index:5}
h1{margin:0;font-size:15px;font-weight:600;word-break:break-all}
.up{display:inline-block;margin-top:8px;color:#ff4500;text-decoration:none;font-size:13px}
main{padding:14px 16px 60px}
.folders{display:flex;flex-direction:column;gap:6px;margin-bottom:20px}
.folder{display:block;padding:12px 14px;background:#1a1a1b;border:1px solid #2a2a2b;
 border-radius:8px;color:#d7dadc;text-decoration:none;font-size:14px}
.folder:active{background:#232324}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.tile{background:#1a1a1b;border:1px solid #2a2a2b;border-radius:8px;overflow:hidden;
 text-decoration:none;color:#d7dadc;display:flex;flex-direction:column}
.tile img{width:100%;aspect-ratio:1;object-fit:cover;background:#000;display:block}
.ph{width:100%;aspect-ratio:1;display:flex;align-items:center;justify-content:center;
 background:#000;font-size:30px}
.name{padding:8px 9px;font-size:11.5px;line-height:1.35;word-break:break-all}
.size{color:#8a8a8a;font-size:10.5px;padding:0 9px 9px}
.empty{color:#8a8a8a;padding:40px 0;text-align:center}

/* ---- swipe viewer ---- */
#viewer{position:fixed;inset:0;background:#000;z-index:50;display:none;
 touch-action:none;overscroll-behavior:contain}
#viewer.on{display:block}
#track{position:absolute;inset:0;display:flex;will-change:transform}
.slide{flex:0 0 100%;height:100%;display:flex;align-items:center;
 justify-content:center;position:relative}
.slide img,.slide video{max-width:100%;max-height:100%;object-fit:contain;
 display:block}
#vclose{position:absolute;top:calc(8px + env(safe-area-inset-top));right:10px;
 z-index:2;width:42px;height:42px;border-radius:50%;border:none;
 background:rgba(0,0,0,.55);color:#fff;font-size:20px;cursor:pointer}
#vcount{position:absolute;bottom:calc(10px + env(safe-area-inset-bottom));
 left:0;right:0;text-align:center;color:#bbb;font-size:12px;z-index:2;
 pointer-events:none;text-shadow:0 1px 4px #000}
#vname{position:absolute;top:calc(14px + env(safe-area-inset-top));left:12px;
 right:64px;color:#ddd;font-size:12px;z-index:2;word-break:break-all;
 text-shadow:0 1px 4px #000}
.vnav{position:absolute;top:50%;transform:translateY(-50%);z-index:2;
 width:44px;height:44px;border-radius:50%;border:none;cursor:pointer;
 background:rgba(255,255,255,.10);color:#fff;font-size:24px}
#vprev{left:8px} #vnext{right:8px}
@media (hover:none){.vnav{display:none}}
"""

# Kept out of the f-strings below: the braces in JS would be interpreted as
# format fields and the page would fail to build.
VIEWER_JS = """
(function(){
  var M = window.__MEDIA__ || [];
  if (!M.length) return;
  var viewer=document.getElementById('viewer'), track=document.getElementById('track');
  var vcount=document.getElementById('vcount'), vname=document.getElementById('vname');
  var i=0;

  var dragging=false, startX=0, startY=0, dx=0, dy=0, locked='';
  var scale=1, tx=0, ty=0;
  var pinching=false, pinchDist=0, baseScale=1, baseTx=0, baseTy=0, pinchX=0, pinchY=0;
  var panning=false, panX=0, panY=0;
  var lastTap=0, MAX=5;

  function curImg(){
    var slide=track.children[1];
    return slide ? slide.querySelector('img') : null;
  }

  function applyZoom(animate){
    var img=curImg();
    if(!img) return;
    img.style.transition = animate ? 'transform .18s' : 'none';
    img.style.transform = 'translate('+tx+'px,'+ty+'px) scale('+scale+')';
  }

  function resetZoom(animate){ scale=1; tx=0; ty=0; applyZoom(animate); }

  // Stop the picture being dragged completely off screen.
  function clampPan(){
    var img=curImg();
    if(!img) return;
    var natural=img.getBoundingClientRect();
    var w=natural.width/scale, h=natural.height/scale;
    var maxX=Math.max(0,(w*scale-window.innerWidth)/2);
    var maxY=Math.max(0,(h*scale-window.innerHeight)/2);
    tx=Math.max(-maxX,Math.min(maxX,tx));
    ty=Math.max(-maxY,Math.min(maxY,ty));
  }

  function slideFor(n){
    var m=M[n], el=document.createElement('div'); el.className='slide';
    if(!m) return el;
    if(m.v){
      var v=document.createElement('video');
      v.src=m.h; v.controls=true; v.playsInline=true; v.preload='metadata';
      el.appendChild(v);
    } else {
      var img=document.createElement('img');
      img.src=m.h; img.alt=m.n; img.draggable=false;
      el.appendChild(img);
    }
    return el;
  }

  // Only previous, current and next exist at once. Building every slide would
  // mean thousands of elements in a large folder.
  function build(){
    track.innerHTML='';
    track.appendChild(slideFor(i-1));
    track.appendChild(slideFor(i));
    track.appendChild(slideFor(i+1));
    track.style.transition='none';
    track.style.transform='translateX(-100%)';
    vcount.textContent=(i+1)+' / '+M.length;
    vname.textContent=M[i]?M[i].n:'';
    resetZoom(false);
  }

  function open(n){
    i=n; build(); viewer.classList.add('on');
    document.body.style.overflow='hidden';
  }
  function close(){
    viewer.classList.remove('on'); track.innerHTML='';
    document.body.style.overflow='';
  }
  function go(step){
    var n=i+step;
    if(n<0||n>=M.length){
      track.style.transition='transform .2s';
      track.style.transform='translateX(-100%)';
      return;
    }
    track.style.transition='transform .2s';
    track.style.transform='translateX('+(step>0?-200:0)+'%)';
    setTimeout(function(){ i=n; build(); },200);
  }

  document.addEventListener('click',function(e){
    var t=e.target.closest('[data-i]');
    if(t){ e.preventDefault(); open(parseInt(t.dataset.i,10)); }
  });
  document.getElementById('vclose').addEventListener('click',close);
  document.getElementById('vprev').addEventListener('click',function(){go(-1);});
  document.getElementById('vnext').addEventListener('click',function(){go(1);});
  document.addEventListener('keydown',function(e){
    if(!viewer.classList.contains('on')) return;
    if(e.key==='Escape'){ if(scale>1){ resetZoom(true); } else { close(); } }
    else if(e.key==='ArrowRight'&&scale===1) go(1);
    else if(e.key==='ArrowLeft'&&scale===1) go(-1);
  });

  function dist(t){
    return Math.hypot(t[1].clientX-t[0].clientX, t[1].clientY-t[0].clientY);
  }

  viewer.addEventListener('touchstart',function(e){
    if(e.touches.length===2 && curImg()){
      pinching=true; dragging=false; panning=false;
      pinchDist=dist(e.touches);
      baseScale=scale; baseTx=tx; baseTy=ty;
      pinchX=(e.touches[0].clientX+e.touches[1].clientX)/2;
      pinchY=(e.touches[0].clientY+e.touches[1].clientY)/2;
      return;
    }
    if(e.touches.length!==1) return;
    if(scale>1){
      panning=true;
      panX=e.touches[0].clientX-tx; panY=e.touches[0].clientY-ty;
      return;
    }
    dragging=true; locked=''; dx=dy=0;
    startX=e.touches[0].clientX; startY=e.touches[0].clientY;
    track.style.transition='none';
  },{passive:false});

  viewer.addEventListener('touchmove',function(e){
    if(pinching && e.touches.length===2){
      e.preventDefault();
      var f=dist(e.touches)/(pinchDist||1);
      scale=Math.max(1,Math.min(MAX, baseScale*f));
      // Zoom about the point between the fingers, not the image centre.
      var k=scale/baseScale;
      tx=pinchX-(pinchX-baseTx)*k;
      ty=pinchY-(pinchY-baseTy)*k;
      clampPan(); applyZoom(false);
      return;
    }
    if(panning && e.touches.length===1){
      e.preventDefault();
      tx=e.touches[0].clientX-panX; ty=e.touches[0].clientY-panY;
      clampPan(); applyZoom(false);
      return;
    }
    if(!dragging) return;
    dx=e.touches[0].clientX-startX; dy=e.touches[0].clientY-startY;
    if(!locked) locked = Math.abs(dx)>Math.abs(dy) ? 'x' : 'y';
    if(locked==='x'){
      e.preventDefault();
      track.style.transform='translateX(calc(-100% + '+dx+'px))';
    } else {
      viewer.style.opacity = String(Math.max(0.3, 1-Math.abs(dy)/500));
    }
  },{passive:false});

  viewer.addEventListener('touchend',function(e){
    // Double tap toggles between fit and 2.5x, centred where you tapped.
    if(!pinching && !panning && e.changedTouches && e.changedTouches.length===1
       && Math.abs(dx)<10 && Math.abs(dy)<10){
      var now=Date.now();
      if(now-lastTap<300 && curImg()){
        var t=e.changedTouches[0];
        if(scale>1){ resetZoom(true); }
        else {
          scale=2.5;
          tx=(window.innerWidth/2-t.clientX)*(scale-1);
          ty=(window.innerHeight/2-t.clientY)*(scale-1);
          clampPan(); applyZoom(true);
        }
        lastTap=0;
        dragging=false;
        return;
      }
      lastTap=now;
    }

    if(pinching){
      pinching=false;
      if(scale<=1.02) resetZoom(true);
      return;
    }
    if(panning){ panning=false; return; }
    if(!dragging) return;
    dragging=false;
    viewer.style.opacity='';
    if(locked==='y'){
      if(Math.abs(dy)>110) close();
      return;
    }
    var threshold=Math.min(90, window.innerWidth*0.18);
    if(dx<-threshold) go(1);
    else if(dx>threshold) go(-1);
    else {
      track.style.transition='transform .18s';
      track.style.transform='translateX(-100%)';
    }
  },{passive:false});
})();
"""


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class Handler(BaseHTTPRequestHandler):
    root = None
    token = None

    protocol_version = "HTTP/1.1"
    server_version = "FolderShare"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    # ---- auth ----

    def _auth(self):
        if not self.token:
            return "ok"
        if f"k={self.token}" in urlparse(self.path).query:
            return "grant"
        cookie = self.headers.get("Cookie") or ""
        return "ok" if f"{COOKIE}={self.token}" in cookie else False

    # ---- path handling ----

    def _resolve(self):
        path = unquote(urlparse(self.path).path)
        parts = [p for p in posixpath.normpath(path).split("/")
                 if p not in ("", ".", "..")]
        target = os.path.realpath(os.path.join(self.root, *parts))
        root = os.path.realpath(self.root)
        if target != root and not target.startswith(root + os.sep):
            return None
        return target

    # ---- responses ----

    def _send(self, status, body, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self._handle(True)

    def do_GET(self):
        self._handle(False)

    def _handle(self, head_only):
        auth = self._auth()
        if not auth:
            self._send(403, b"<h1>Access code required</h1>"
                            b"<p>Open the full link printed by the script.</p>")
            return
        if auth == "grant":
            self.send_response(302)
            self.send_header("Set-Cookie",
                             f"{COOKIE}={self.token}; Path=/; SameSite=Lax; Max-Age=86400")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        target = self._resolve()
        if target is None or not os.path.exists(target):
            self._send(404, b"<h1>Not found</h1>")
            return
        if os.path.isdir(target):
            self._send(200, self._listing(target).encode("utf-8"))
            return
        self._send_file(target, head_only)

    def _listing(self, directory):
        rel = os.path.relpath(directory, self.root).replace("\\", "/")
        if rel == ".":
            rel = ""
        try:
            entries = sorted(os.scandir(directory),
                             key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            entries = []

        folders, tiles, media = [], [], []
        for entry in entries:
            link = quote(("/" + rel + "/" + entry.name).replace("//", "/"))
            label = html.escape(entry.name)
            if entry.is_dir():
                folders.append(f'<a class="folder" href="{link}">{label}/</a>')
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            try:
                size = human(entry.stat().st_size)
            except OSError:
                size = ""

            # Photos and videos join the swipe viewer; everything else stays a
            # plain download link.
            index = ""
            if ext in IMAGE_EXT or ext in VIDEO_EXT:
                index = f' data-i="{len(media)}"'
                media.append({"h": link, "n": entry.name,
                              "v": ext in VIDEO_EXT})

            if ext in IMAGE_EXT:
                thumb = f'<img loading="lazy" src="{link}" alt="">'
            elif ext in VIDEO_EXT:
                thumb = '<div class="ph">&#9654;</div>'
            else:
                thumb = '<div class="ph">&#128196;</div>'
            tiles.append(
                f'<a class="tile" href="{link}"{index}>{thumb}'
                f'<div class="name">{label}</div>'
                f'<div class="size">{size}</div></a>'
            )

        parent = ""
        if rel:
            up = quote("/" + posixpath.dirname(rel))
            parent = f'<a class="up" href="{up or "/"}">&#8592; up a level</a>'

        body = ""
        if folders:
            body += '<div class="folders">' + "".join(folders) + "</div>"
        if tiles:
            body += '<div class="grid">' + "".join(tiles) + "</div>"
        if not body:
            body = '<div class="empty">This folder is empty.</div>'

        viewer = (
            '<div id="viewer">'
            '<div id="track"></div>'
            '<div id="vname"></div>'
            '<button id="vclose" aria-label="Close">&#10005;</button>'
            '<button class="vnav" id="vprev" aria-label="Previous">&#8249;</button>'
            '<button class="vnav" id="vnext" aria-label="Next">&#8250;</button>'
            '<div id="vcount"></div>'
            "</div>"
        )
        payload = json.dumps(media, ensure_ascii=False)

        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1,"
            "viewport-fit=cover'>"
            f"<title>{html.escape(rel or os.path.basename(self.root) or 'Shared')}</title>"
            f"<style>{PAGE_CSS}</style></head><body>"
            f"<header><h1>{html.escape('/' + rel if rel else 'Shared folder')}</h1>"
            f"{parent}</header><main>{body}</main>"
            + viewer
            + "<script>window.__MEDIA__=" + payload + ";</script>"
            + "<script>" + VIEWER_JS + "</script>"
            + "</body></html>"
        )

    def _send_file(self, path, head_only):
        try:
            size = os.path.getsize(path)
            handle = open(path, "rb")
        except OSError:
            self._send(404, b"<h1>Not found</h1>")
            return

        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start = end = None
        header = self.headers.get("Range")
        if header and size:
            m = RANGE_RE.search(header)
            if m:
                first, last = m.group(1), m.group(2)
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    start = max(0, size - int(last))
                    end = size - 1
                if start is not None and start >= size:
                    start = end = None
                elif end is not None:
                    end = min(end, size - 1)

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
            self.send_header("Accept-Ranges", "bytes")   # lets phones seek
            self.end_headers()
            if head_only:
                return
            left = length
            try:
                while left > 0:
                    chunk = handle.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass



# --------------------------------------------------------------- system tray
# Uses pywin32 directly rather than pystray, so there is no extra dependency
# to install. If pywin32 is missing the script still runs and Ctrl+C stops it.

class Tray:
    """A notification-area icon whose menu can stop the server."""

    WM_TRAY = 0x0400 + 20          # WM_USER + 20
    ID_OPEN, ID_COPY, ID_STOP = 1, 2, 3

    def __init__(self, url, folder, on_stop):
        import win32api
        import win32con
        import win32gui

        self.win32api, self.win32con, self.win32gui = win32api, win32con, win32gui
        self.url, self.folder, self.on_stop = url, folder, on_stop
        self.running = True

        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = "FolderShareTray"
        wc.lpfnWndProc = self._proc
        try:
            handle = win32gui.RegisterClass(wc)
        except Exception:
            # Already registered by an earlier Tray in this process -- Windows
            # refuses to register the same class twice, so reuse it by name.
            handle = wc.lpszClassName

        self.hwnd = win32gui.CreateWindow(
            handle, "Folder sharing", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None
        )
        win32gui.UpdateWindow(self.hwnd)

        icon = self._icon()
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (
            self.hwnd, 0,
            win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
            self.WM_TRAY, icon, f"Sharing {os.path.basename(folder) or folder}",
        ))

    def _icon(self):
        """The app icon if it is sitting next to the script, else a stock one."""
        here = os.path.dirname(os.path.abspath(__file__))
        for candidate in (os.path.join(here, "icon.ico"),
                          os.path.join(os.path.dirname(here), "icon.ico")):
            if os.path.isfile(candidate):
                try:
                    return self.win32gui.LoadImage(
                        0, candidate, self.win32con.IMAGE_ICON, 0, 0,
                        self.win32con.LR_LOADFROMFILE | self.win32con.LR_DEFAULTSIZE,
                    )
                except Exception:
                    pass
        return self.win32gui.LoadIcon(0, self.win32con.IDI_APPLICATION)

    def _proc(self, hwnd, msg, wparam, lparam):
        g, c = self.win32gui, self.win32con
        if msg == self.WM_TRAY:
            if lparam == c.WM_RBUTTONUP:
                self._menu()
            elif lparam == c.WM_LBUTTONDBLCLK:
                webbrowser.open(self.url)
        elif msg == c.WM_DESTROY:
            self._remove()
            g.PostQuitMessage(0)
        return g.DefWindowProc(hwnd, msg, wparam, lparam)

    def _menu(self):
        g, c = self.win32gui, self.win32con
        menu = g.CreatePopupMenu()
        g.AppendMenu(menu, c.MF_STRING, self.ID_OPEN, "Open in browser")
        g.AppendMenu(menu, c.MF_STRING, self.ID_COPY, "Copy link")
        g.AppendMenu(menu, c.MF_SEPARATOR, 0, "")
        g.AppendMenu(menu, c.MF_STRING, self.ID_STOP, "Stop sharing")
        pos = g.GetCursorPos()
        # Required, or the menu refuses to close when you click elsewhere.
        g.SetForegroundWindow(self.hwnd)
        choice = g.TrackPopupMenu(
            menu, c.TPM_LEFTALIGN | c.TPM_RIGHTBUTTON | c.TPM_RETURNCMD,
            pos[0], pos[1], 0, self.hwnd, None,
        )
        g.PostMessage(self.hwnd, c.WM_NULL, 0, 0)
        if choice == self.ID_OPEN:
            webbrowser.open(self.url)
        elif choice == self.ID_COPY:
            self._copy(self.url)
        elif choice == self.ID_STOP:
            self.stop()

    def _copy(self, text):
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text)
            win32clipboard.CloseClipboard()
        except Exception:
            pass

    def _remove(self):
        try:
            self.win32gui.Shell_NotifyIcon(
                self.win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception:
            pass

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.on_stop()
        self._remove()
        try:
            self.win32gui.DestroyWindow(self.hwnd)
        except Exception:
            self.win32gui.PostQuitMessage(0)

    def run(self):
        self.win32gui.PumpMessages()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="folder to share")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--no-token", action="store_true",
                    help="skip the access code (anyone on the WiFi can browse)")
    ap.add_argument("--no-tray", action="store_true",
                    help="do not show a notification-area icon")
    args = ap.parse_args()

    root = os.path.abspath(args.folder)
    if not os.path.isdir(root):
        print(f"Not a folder: {root}")
        return 1

    token = None if args.no_token else secrets.token_urlsafe(9)
    handler = type("Bound", (Handler,), {"root": root, "token": token})

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    except OSError as exc:
        print(f"Could not listen on port {args.port}: {exc}")
        return 1
    httpd.daemon_threads = True

    suffix = f"/?k={token}" if token else "/"
    url = f"http://{lan_ip()}:{args.port}{suffix}"

    print(f"\n  Sharing: {root}")
    print(f"  Open on your phone (same WiFi):\n")
    print(f"      {url}\n")
    if token:
        print("  The link contains an access code -- keep it to yourself.")
    else:
        print("  WARNING: no access code. Anyone on this WiFi can browse it.")
    print("  Read-only.", flush=True)

    tray = None
    if not args.no_tray:
        try:
            tray = Tray(url, root, on_stop=httpd.shutdown)
        except Exception as exc:
            print(f"  (tray icon unavailable: {exc})")

    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    if tray:
        print("  Look for the icon in the notification area (bottom right).")
        print("  Right-click it to stop sharing. Closing this window also "
              "stops it.\n", flush=True)
        try:
            tray.run()
        except KeyboardInterrupt:
            tray.stop()
    else:
        print("  Press Ctrl+C to stop.\n", flush=True)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            httpd.shutdown()

    print("  Stopped sharing.")
    httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
