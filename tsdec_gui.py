#!/usr/bin/env python3
"""tsdec-gui: a local web front end for the tsdec offline decrypter.

This holds no decoding code. It runs the tsdec binary as a child process and
drives it through the command line:

  progress  read from the JSON lines tsdec emits with --json
  stop      Ctrl+C on the child, which tsdec treats as a request rather than
            a kill, so whatever was decrypted is kept
  result    the exit status distinguishes "could not sync" from "nothing was
            encrypted" from "stopped on request"

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
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")

PCKTSIZE = 188

# exit codes from tsdec.h
RET_OK = 0
RET_INFILE_NOTOPEN = 10
RET_CWLFILEOPEN = 21
RET_TOOLESSCWS = 22
RET_OUTFILEOPEN = 30
RET_NOSYNC = 50
RET_OUTOFCWS = 51
RET_TSCORRUPT = 52
RET_NOTCRYPTED = 53
RET_CANCELED = 55
RET_USAGE = 61

STATUS_TEXT = {
    RET_OK: "done",
    RET_INFILE_NOTOPEN: "cannot open the recording",
    RET_CWLFILEOPEN: "cannot open the control word log",
    RET_TOOLESSCWS: "the control word log has no usable control words",
    RET_OUTFILEOPEN: "cannot write the output file",
    RET_NOSYNC: "could not sync the control word log to this recording",
    RET_OUTOFCWS: "the control word log ran out before the recording",
    RET_TSCORRUPT: "the transport stream is corrupt",
    RET_NOTCRYPTED: "this recording has no encrypted packets",
    RET_CANCELED: "stopped, the part already decrypted was kept",
}


def status_text(code):
    if code in STATUS_TEXT:
        return STATUS_TEXT[code]
    return "tsdec exited with status %d" % code


class Job:
    """One run of tsdec, with its output stream fanned out to any listener."""

    def __init__(self, tsdec_path, args):
        self.tsdec = tsdec_path
        self.args = args
        self.lock = threading.Lock()
        self.proc = None
        self.lines = []            # everything tsdec said, for the log pane
        self.result = None         # final statistics dict
        self.done = False
        self.started = 0.0
        self.id = 0

    def _emit(self, obj):
        with self.lock:
            self.lines.append(obj)

    def append_log(self, text):
        self._emit({"event": "log", "text": text})

    def run(self):
        self.started = time.time()
        cmd = [self.tsdec, "--json", "-v", "0"] + self.args
        self.append_log("$ " + " ".join(cmd))
        try:
            # CREATE_NEW_PROCESS_GROUP on Windows, without it a console
            # control event aimed at this child would also hit us. Newlines
            # rather than buffering=1: the decoder emits whole JSON lines as
            # they complete, and line buffering a pipe is only honoured for
            # interactive streams on some platforms, which would stall the
            # progress feed.
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, bufsize=0,
                creationflags=creationflags)
        except OSError as e:
            self.result = {"event": "result", "status": -1,
                           "message": "cannot run tsdec: %s" % e}
            self.done = True
            return

        out_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        err_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        out_thread.start()
        err_thread.start()

        code = self.proc.wait()
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)

        with self.lock:
            if self.result is None:
                self.result = {"event": "result", "status": code}
            self.result.setdefault("status", code)
            self.result["elapsed_wall"] = round(time.time() - self.started, 3)
            # tsdec words its own result, so prefer that over our table, which
            # is only a fallback for an older binary or an unexpected code
            if not self.result.get("message"):
                self.result["message"] = status_text(self.result["status"])
            self.done = True

    def _pump_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                self.append_log(line)
                continue
            with self.lock:
                if obj.get("event") == "result":
                    self.result = obj
                self.lines.append(obj)

    def _pump_stderr(self):
        for line in self.proc.stderr:
            self.append_log("tsdec: " + line.rstrip())

    def stop(self):
        with self.lock:
            proc = self.proc
        if not proc or proc.poll() is not None:
            return False

        # tsdec treats Ctrl+C as "please stop, keep what is done". On Windows
        # that is a console control event, which needs the child to be in its
        # own process group; on POSIX it is SIGINT. A hard terminate would
        # throw away the packets already decrypted, so it is only the fallback.
        try:
            if os.name == "nt":
                # tsdec installs a SIGINT handler via signal(), which Windows
                # backs with SetConsoleCtrlHandler. CTRL_BREAK maps to
                # SIGBREAK and would bypass it, so ask for CTRL_C, and target
                # the child's own process group so the event does not also
                # reach this server.
                proc.send_signal(signal.CTRL_C_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        except (OSError, ValueError) as e:
            self.append_log("soft stop failed (%s), falling back to kill" % e)
            try:
                proc.kill()
            except OSError:
                pass
            return False

        # if it has not noticed within a few seconds, it is wedged
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.append_log("still running after the stop, killing it", "warn")
            try:
                proc.kill()
            except OSError:
                pass
        return True

    def snapshot(self):
        """Everything the UI needs right now, as one JSON object."""
        with self.lock:
            lines = list(self.lines)
            result = self.result
            done = self.done
        return {
            "event": "snapshot",
            "done": done,
            "started": self.started,
            "running": bool(self.proc and self.proc.poll() is None),
            "lines": lines[-400:],
            "result": result,
        }


class Manager:
    """Holds the one job that may be running at a time."""

    def __init__(self, tsdec_path):
        self.tsdec = tsdec_path
        self.job = None
        self.counter = 0
        self.lock = threading.Lock()

    def start(self, args):
        with self.lock:
            if self.job and not self.job.done:
                return {"error": "a job is already running"}
            self.counter += 1
            job = Job(self.tsdec, args)
            job.id = self.counter
            self.job = job
        threading.Thread(target=job.run, daemon=True).start()
        return {"job": job.id}

    def stop(self):
        with self.lock:
            job = self.job
        if not job:
            return {"error": "nothing is running"}
        return {"stopped": job.stop()}

    def current(self):
        with self.lock:
            job = self.job
        if not job:
            return {"event": "snapshot", "done": True, "running": False,
                    "lines": [], "result": None}
        return job.snapshot()


def human_size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TiB" % n


def validate(form):
    """Check the request before handing anything to a subprocess."""
    problems = []

    def field(name):
        v = (form.get(name) or "").strip()
        if not v:
            problems.append("%s is required" % name)
            return ""
        return v

    ts = field("input")
    cwl = field("cwl")

    for label, path in (("recording", ts), ("control word log", cwl)):
        if path and not os.path.isfile(path):
            problems.append("%s not found: %s" % (label, path))

    out = (form.get("output") or "").strip()
    if not out:
        problems.append("output is required")
    else:
        d = os.path.dirname(os.path.abspath(out))
        if not os.path.isdir(d):
            problems.append("output folder does not exist: %s" % d)
        elif os.path.abspath(out) in (os.path.abspath(ts) if ts else "",):
            problems.append("output must not overwrite the input")

    blocker = (form.get("blocker") or "300").strip()
    if not blocker.isdigit():
        problems.append("blocker must be a number")

    threads = (form.get("threads") or "0").strip()
    if not threads.isdigit():
        problems.append("threads must be a number")

    pids = (form.get("pids") or "").strip()
    if pids:
        for part in re.split(r"[,\s]+", pids):
            if not part:
                continue
            try:
                v = int(part, 0)
            except ValueError:
                problems.append("pid list must be numbers: %s" % part)
                break
            if not 0 <= v <= 0x1FFF:
                problems.append("pid out of range: %s" % part)
                break

    if problems:
        return None, problems

    args = ["-f", cwl, "-i", ts, "-o", out, "-b", blocker]
    if threads != "0":
        args += ["-t", threads]
    if pids:
        args += ["-p", ",".join(p.strip() for p in re.split(r"[,\s]+", pids) if p.strip())]
    if form.get("resync"):
        args.append("-r")

    info = {
        "input": ts,
        "input_size": human_size(os.path.getsize(ts)),
        "output": out,
        "args": args,
    }
    return info, []


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
            return self._json(self.server.manager.current())
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
            info, problems = validate(body)
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
            return self._analyze(body)

        self._json({"error": "not found"}, status=404)

    def _analyze(self, body):
        path = (body.get("input") or "").strip()
        if not path or not os.path.isfile(path):
            return self._json({"ok": False, "problems": ["recording not found"]},
                              status=400)
        try:
            r = subprocess.run([self.server.tsdec, "-a", "-i", path],
                               capture_output=True, text=True, timeout=120)
        except OSError as e:
            return self._json({"ok": False, "problems": [str(e)]}, status=500)

        rows = []
        for line in r.stderr.splitlines():
            # every line is prefixed by the decoder, drop it before matching
            text = line
            if text.startswith("TSDEC: "):
                text = text[7:]
            m = re.match(r"\s*(0x[0-9a-fA-F]+)\s+(\d+)\s+(\d+)\s+([\d.]+)%\s+(\d+)",
                         text)
            if m:
                rows.append({
                    "pid": m.group(1),
                    "packets": int(m.group(2)),
                    "scrambled": int(m.group(3)),
                    "share": float(m.group(4)),
                    "cc_errors": int(m.group(5)),
                })
        return self._json({"ok": True, "pids": rows,
                           "raw": r.stderr.splitlines()})

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, tsdec_path):
        self.tsdec = tsdec_path
        self.manager = Manager(tsdec_path)
        super().__init__(addr, handler)


def find_tsdec(explicit):
    if explicit:
        return explicit
    from os import environ
    env = environ.get("TSDEC_BIN")
    if env and os.path.isfile(env):
        return env
    from shutil import which
    found = which("tsdec") or which("tsdec.exe")
    if found:
        return found
    # fall back to a sibling checkout, which is how this is developed
    for candidate in (
        os.path.join(HERE, "..", "tsdec", "tsdec"),
        os.path.join(HERE, "..", "tsdec", "tsdec.exe"),
    ):
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)
    return None


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
