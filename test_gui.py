#!/usr/bin/env python3
"""End to end test for tsdec-gui against a real tsdec run.

Starts the server in a thread, exercises the API the way the browser does,
and checks that a real decryption runs, reports progress and produces output
that matches the plaintext.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tsdec_gui  # noqa: E402

PORT = 8766
BASE = "http://127.0.0.1:%d" % PORT


def find_decoder():
    """The decoder, and the checkout it came from so the sample can be built.

    The tools that generate a test recording live in the decoder repository, so
    wherever the binary came from is also where they are looked for. Without
    the source tree the suite cannot build its own input and has nothing to
    test against, so say so rather than failing later in a confusing way.
    """
    env_bin = os.environ.get("TSDEC_BIN")
    env_src = os.environ.get("TSDEC_SRC")

    candidates = [
        (env_bin, env_src),
        (os.path.join(HERE, "..", "tsdec", "tsdec.exe"), os.path.join(HERE, "..", "tsdec")),
        (os.path.join(HERE, "..", "tsdec", "tsdec"), os.path.join(HERE, "..", "tsdec")),
        (os.path.join(HERE, "..", "_decoder", "tsdec"), os.path.join(HERE, "..", "_decoder")),
    ]

    for binary, source in candidates:
        if binary and os.path.isfile(binary) and \
                os.path.isfile(os.path.join(source, "tools", "mk_ts.py")):
            return os.path.normpath(binary), os.path.normpath(source)

    for binary, source in candidates:
        if binary and os.path.isfile(binary):
            return os.path.normpath(binary), source

    return None, None


TSDEC, TSDEC_SRC = find_decoder()

WORK = os.environ.get("GUI_WORK") or os.path.join(HERE, "testdata")

passed = failed = 0


def ok(name, cond, detail=""):
    global passed, failed
    if cond:
        print("  PASS  %s" % name)
        passed += 1
    else:
        print("  FAIL  %s%s" % (name, ("  [%s]" % detail) if detail else ""))
        failed += 1


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode())


def post(path, payload):
    """POST and return the body, whatever the status.

    A 400 here is an answer, not a failure: the browser asks about a bad form
    and the server explains why, so raising on the status would throw away the
    message the test is actually after.
    """
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def build_sample():
    """Make a small encrypted recording and its control word log."""
    os.makedirs(WORK, exist_ok=True)
    plain = os.path.join(WORK, "plain.ts")
    enc = os.path.join(WORK, "enc.ts")
    cwl = os.path.join(WORK, "sample.cwl")
    if os.path.isfile(enc) and os.path.isfile(cwl):
        return plain, enc, cwl

    tools = os.path.join(TSDEC_SRC, "tools")
    if not os.path.isdir(tools):
        print("SKIP: no decoder source at %s, cannot build a sample.\n"
              "      Set TSDEC_SRC to the tsdec checkout." % TSDEC_SRC)
        return None
    subprocess.run([sys.executable, os.path.join(tools, "mk_ts.py"),
                    "-o", plain, "-n", "30000"], check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, os.path.join(tools, "mk_test_pair.py"),
                    "-i", plain, "-o", enc, "-c", cwl,
                    "-t", TSDEC, "-w", "2000", "--quiet"], check=True)
    return plain, enc, cwl


def main():
    if not TSDEC:
        print("SKIP: tsdec not found. Set TSDEC_BIN, or clone tsdec next to "
              "this checkout.")
        return 0

    built = build_sample()
    if built is None:
        return 0
    plain, enc, cwl = built
    print("sample: %s (%d bytes)" % (os.path.basename(enc), os.path.getsize(enc)))

    handler = tsdec_gui.Handler
    httpd = tsdec_gui.Server(("127.0.0.1", PORT), handler, TSDEC)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)

    def wait_done(timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            state = get("/api/state")
            if state["done"]:
                return state
            time.sleep(0.15)
        return get("/api/state")

    try:
        # ---- the page and its assets are served ----
        for path, needle in (("/", b"tsdec"), ("/web/app.css", b"--accent"),
                             ("/web/app.js", b"api/state")):
            with urllib.request.urlopen(BASE + path, timeout=10) as r:
                body = r.read()
            ok("serves %s" % path, needle in body)

        # ---- path traversal is refused ----
        try:
            urllib.request.urlopen(BASE + "/web/..%2f..%2ftsdec.h", timeout=10)
            ok("refuses path traversal", False, "served it")
        except urllib.error.HTTPError as e:
            ok("refuses path traversal", e.code == 404)

        # ---- idle state ----
        s = get("/api/state")
        ok("idle state is idle", s["done"] and not s["running"])

        # ---- validation catches bad input ----
        r = post("/api/validate", {"input": "", "cwl": "", "output": ""})
        ok("rejects an empty form", not r["ok"] and len(r["problems"]) >= 3)

        r = post("/api/validate", {"input": "nope.ts", "cwl": "nope.cwl",
                                   "output": "out.ts", "threads": "1",
                                   "blocker": "300", "pids": ""})
        ok("rejects missing files", not r["ok"])

        r = post("/api/validate", {"input": enc, "cwl": cwl, "output": enc,
                                   "threads": "1", "blocker": "300",
                                   "pids": ""})
        ok("refuses to overwrite the input", not r["ok"])

        r = post("/api/validate", {"input": enc, "cwl": cwl,
                                   "output": os.path.join(WORK, "o.ts"),
                                   "threads": "1", "blocker": "abc", "pids": ""})
        ok("rejects a non numeric blocker", not r["ok"])

        r = post("/api/validate", {"input": enc, "cwl": cwl,
                                   "output": os.path.join(WORK, "o.ts"),
                                   "threads": "1", "blocker": "300",
                                   "pids": "0x100,notapid"})
        ok("rejects a bad pid list", not r["ok"])

        # ---- a good form validates ----
        out = os.path.join(WORK, "gui.out.ts")
        if os.path.exists(out):
            os.remove(out)
        form = {"input": enc, "cwl": cwl, "output": out, "threads": "1",
                "blocker": "300", "pids": "", "resync": False}
        r = post("/api/validate", form)
        ok("accepts a good form", r["ok"], str(r.get("problems")))
        ok("reports the recording size", r["info"]["input_size"].endswith(("MiB", "KiB")),
           r["info"].get("input_size", ""))

        # ---- the pid survey ----
        r = post("/api/analyze", {"input": enc})
        ok("surveys PIDs", r["ok"] and len(r["pids"]) >= 2,
           "%d pids" % len(r.get("pids", [])))
        ok("finds the scrambled video pid",
           any(p["scrambled"] > 0 for p in r.get("pids", [])))

        # ---- a real decryption, driven exactly as the browser does ----
        if os.path.exists(out):
            os.remove(out)
        r = post("/api/start", form)
        ok("starts a job", r["ok"], str(r.get("problems")))

        s = wait_done()
        ok("job finishes", s["done"])
        res = s["result"] or {}
        ok("reports success", res.get("status") == 0,
           "%s / %s" % (res.get("status"), res.get("message")))
        ok("decrypted every encrypted packet",
           res.get("decrypted", 0) > 0 and res.get("dropped", 1) == 0,
           "decrypted=%s dropped=%s" % (res.get("decrypted"), res.get("dropped")))
        ok("synced exactly once", res.get("syncs") == 1,
           "syncs=%s" % res.get("syncs"))
        ok("wrote the output file", os.path.isfile(out))

        progress = [l for l in s["lines"] if l.get("event") == "progress"]
        # the small sample can finish inside a single rate limited tick, so it
        # is the big run below that must show a live progress feed
        ok("no bogus progress on a short run",
           all(0 <= l.get("done", 0) <= l.get("total", 0) for l in progress))

        # ---- the output is actually the plaintext ----
        r = subprocess.run([sys.executable,
                            os.path.join(TSDEC_SRC, "tools", "verify.py"),
                            plain, out], capture_output=True, text=True)
        ok("output matches the plaintext", r.stdout.startswith("PASS"),
           r.stdout.strip().splitlines()[:1])

        # ---- the log is kept for the pane ----
        ok("keeps a log for the pane",
            any(l.get("event") == "log" for l in s["lines"]))

        # ---- an existing output is not silently replaced ----
        # the run above left one behind, so this is the case that matters:
        # a stopped run leaves a partial file, and replacing it without asking
        # would throw away exactly the work the stop button protects
        r = post("/api/start", form)
        ok("an existing output is refused by default",
           not r["ok"] and r["problems"]
           and r["problems"][0].startswith("output already exists:"),
           str(r.get("problems")))
        # the browser confirms and retries, which is what the ui does
        r = post("/api/start", dict(form, overwrite=True))
        ok("a confirmed overwrite goes through", r["ok"], str(r.get("problems")))
        ok("and it finishes", wait_done()["done"])

        # ---- stop is honoured ----
        # use a big enough sample that one thread is still working
        big = os.path.join(WORK, "big.enc.ts")
        bigcwl = os.path.join(WORK, "big.cwl")
        bigplain = os.path.join(WORK, "big.ts")
        if not os.path.isfile(big):
            src = open(enc, "rb").read()
            with open(bigplain, "wb") as f:
                for r_ in range(12):
                    f.write(src)
            subprocess.run([sys.executable,
                            os.path.join(TSDEC_SRC, "tools", "mk_test_pair.py"),
                            "-i", bigplain, "-o", big, "-c", bigcwl,
                            "-t", TSDEC, "-w", "20000", "--quiet"], check=True)

        bigout = os.path.join(WORK, "big.stopped.ts")
        if os.path.exists(bigout):
            os.remove(bigout)
        post("/api/start", {"input": big, "cwl": bigcwl, "output": bigout,
                            "threads": "1", "blocker": "300", "pids": "",
                            "resync": False})
        time.sleep(0.6)

        # the big sample is slow enough at one thread to show a live feed
        live = [l for l in get("/api/state")["lines"]
                if l.get("event") == "progress"]
        ok("emits progress while working", len(live) >= 1,
           "%d lines" % len(live))
        if live:
            p = live[0]
            ok("progress carries a total", p.get("total", 0) > 0)
            ok("progress carries a rate", p.get("mib_per_second", 0) > 0)
            ok("progress stays within bounds",
               0 <= p.get("done", -1) <= p.get("total", 0), str(p))
            ok("progress counts up", p.get("done", 0) > 0, str(p))

        r = post("/api/stop", {})
        ok("stop is accepted", r["ok"], str(r.get("problems")))

        deadline = time.time() + 60
        while time.time() < deadline:
            s = get("/api/state")
            if s["done"]:
                break
            time.sleep(0.15)

        res = s["result"] or {}
        ok("stop is reported as stopped", res.get("status") == 55,
           "%s / %s" % (res.get("status"), res.get("message")))
        ok("stop says the work already done was kept",
           "kept" in (res.get("message") or ""), res.get("message", ""))

        size = os.path.getsize(bigout) if os.path.isfile(bigout) else 0
        ok("partial output is packet aligned", size > 0 and size % 188 == 0,
           "%d bytes" % size)
        ok("partial output is shorter than the input",
           0 < size < os.path.getsize(big))

    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n== %d passed, %d failed ==" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
