#!/usr/bin/env python3
"""tsdec-gui: a local web front end for the tsdec offline decrypter.

The interesting half, running the decoder and stopping it without losing work,
lives in tsdec_core.py and is shared with the native front end. This is only
the shell that puts it behind HTTP.

The server binds to the loopback interface only and refuses to serve anything
but the files below web/, so the only thing that can reach it is a browser on
this machine. It serves no path from the filesystem, which is what keeps a
browser from being able to read your disk through it.
"""

import argparse
import http.server
import json
import mimetypes
import os
import posixpath
import socketserver
import sys
import threading
import urllib.parse

from tsdec_core import Manager, find_tsdec, survey, validate

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "tsdec-gui"
    protocol_version = "HTTP/1.1"

    # keep the UI from caching itself during development
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass                          # the job log pane is the useful log

    # ---- static assets -------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/index.html"):
            return self._serve_file("index.html")
        if path == "/api/state":
            return self._json(self.server.state())
        if path == "/api/browse":
            return self._browse()
        if path.startswith("/web/"):
            return self._serve_file(path[len("/web/"):])
        self._json({"error": "not found"}, status=404)

    def _serve_file(self, name):
        # normalise and confine to the web directory: a crafted path must not
        # be able to read anything outside it
        clean = posixpath.normpath("/" + name).lstrip("/")
        full = os.path.normpath(os.path.join(WEB, clean))
        if not full.startswith(os.path.normpath(WEB)) or not os.path.isfile(full):
            return self._json({"error": "not found"}, status=404)

        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _browse(self):
        """List a folder so the UI can offer a picker without typing paths."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        path = (q.get("path") or [""])[0] or os.path.expanduser("~")
        if not os.path.isdir(path):
            return self._json({"error": "not a folder", "path": path})

        entries = []
        try:
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append({
                    "name": name,
                    "path": full,
                    "dir": os.path.isdir(full),
                    "size": st.st_size,
                })
        except PermissionError:
            return self._json({"error": "permission denied", "path": path})

        return self._json({
            "path": path,
            "parent": os.path.dirname(path.rstrip(os.sep)) or None,
            "entries": entries[:2000],
        })

    # ---- actions -------------------------------------------------------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._json({"error": "malformed request"}, status=400)

        if path == "/api/validate":
            info, problems = validate(body)
            return self._json({"ok": not problems, "problems": problems,
                               "info": info})
        if path == "/api/start":
            # the browser asks first and says so, rather than the server
            # guessing which of the two it should be
            info, problems = validate(
                body, allow_overwrite=bool(body.get("overwrite")))
            if problems:
                return self._json({"ok": False, "problems": problems},
                                  status=400)
            reply = self.server.manager.start(info["args"])
            if "error" in reply:
                return self._json({"ok": False, "problems": [reply["error"]]},
                                  status=409)
            return self._json({"ok": True, "job": reply["job"],
                               "info": info})
        if path == "/api/stop":
            reply = self.server.manager.stop()
            if "error" in reply:
                return self._json({"ok": False, "problems": [reply["error"]]},
                                  status=409)
            return self._json({"ok": True, "stopped": reply["stopped"]})
        if path == "/api/analyze":
            rows, err = survey(self.server.tsdec,
                               (body.get("input") or "").strip())
            if err:
                return self._json({"ok": False, "problems": [err]}, status=400)
            return self._json({"ok": True, "pids": rows})

        self._json({"error": "not found"}, status=404)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def snapshot(job):
    """The current state as the web front end wants it.

    The core hands back a Job; a poller wants one object with everything in it.
    """
    if not job:
        return {"event": "snapshot", "done": True, "running": False,
                "lines": [], "result": None}
    st = job.state()
    st["event"] = "snapshot"
    st["lines"] = job.history()
    return st


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, tsdec_path):
        self.tsdec = tsdec_path
        self.manager = Manager(tsdec_path)
        super().__init__(addr, handler)

    def state(self):
        return snapshot(self.manager.current())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", type=int, default=8756,
                    help="port to listen on (default 8756)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to bind, keep this on loopback")
    ap.add_argument("--tsdec", default=None,
                    help="path to the tsdec binary")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser")
    args = ap.parse_args()

    tsdec = find_tsdec(args.tsdec)
    if not tsdec:
        sys.exit("tsdec not found; put it on PATH or pass --tsdec")
    print("tsdec: %s" % tsdec)

    # Binding anywhere other than loopback would put an unauthenticated file
    # decrypting service on the network. Refuse rather than warn.
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("refusing to bind %s: this serves a local tool with no "
                 "authentication, keep it on loopback" % args.host)

    try:
        httpd = Server((args.host, args.port), Handler, tsdec)
    except OSError as e:
        sys.exit("cannot listen on %s:%d: %s" % (args.host, args.port, e))

    url = "http://%s:%d/" % (args.host, args.port)
    print("tsdec-gui on %s  (ctrl+c to quit)" % url)
    if not args.no_browser:
        threading.Timer(0.4, lambda: __import__("webbrowser").open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
