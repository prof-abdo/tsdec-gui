#!/usr/bin/env python3
"""Check the packaged executable the way a person who downloaded it would.

The unit suites test the code. This tests the artefact: a single exe in an
empty folder, with nothing useful on PATH and no checkout next to it, has to
find the decoder inside itself, run a real decryption, stop one that is still
going, and open a window. Each of those is a packaging question rather than a
coding one, and each has its own way of quietly going wrong.

    python test_exe.py [path/to/tsdec-gui.exe]
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXE = os.path.join(HERE, "dist", "tsdec-gui.exe")
DECODER_SRC = os.environ.get("TSDEC_SRC") or os.path.normpath(
    os.path.join(HERE, "..", "tsdec"))

passed = failed = 0


def ok(name, cond, detail=""):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  [%s]" % detail) if (detail and not cond) else ""), flush=True)
    if cond:
        passed += 1
    else:
        failed += 1


def run(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 300)
    return subprocess.run(args, **kw)


def bare_env():
    """An environment where the decoder can only come from the bundle.

    PATH keeps System32 so python still resolves, but no checkout is reachable
    and TSDEC_BIN is cleared, which is the situation of someone who has
    downloaded one file and nothing else.
    """
    env = dict(os.environ)
    system = os.environ.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join([system + r"\System32", system])
    env.pop("TSDEC_BIN", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def make_sample(work, packets=2400000):
    """A recording long enough that a stop lands in the middle of it.

    The size matters more than it looks: the decoder does over a gigabyte a
    second, so a recording that finishes in half a second finishes before the
    stop even arrives and the test proves nothing. A 430 MiB sample takes a
    few seconds on one thread, which is long enough to interrupt.
    """
    tools = os.path.join(DECODER_SRC, "tools")
    decoder = os.path.join(DECODER_SRC, "tsdec.exe")
    if not os.path.isdir(tools) or not os.path.isfile(decoder):
        print("SKIP: no tsdec build at %s to make a sample with.\n"
              "      Set TSDEC_SRC." % DECODER_SRC)
        return None

    plain = os.path.join(work, "plain.ts")
    enc = os.path.join(work, "enc.ts")
    cwl = os.path.join(work, "sample.cwl")

    run([sys.executable, os.path.join(tools, "mk_ts.py"),
         "-o", plain, "-n", str(packets)])
    r = run([sys.executable, os.path.join(tools, "mk_test_pair.py"),
             "-i", plain, "-o", enc, "-c", cwl, "-t", decoder,
             "-w", "20000", "--quiet"], timeout=600)
    if r.returncode != 0 or not os.path.isfile(enc):
        return None
    return plain, enc, cwl


def decrypt(exe, env, enc, cwl, out, stop_after=0.0, threads="1"):
    args = [exe, "--decrypt", "-i", enc, "-f", cwl, "-o", out, "-t", threads]
    if stop_after:
        args += ["--stop-after", str(stop_after)]
    r = run(args, env=env)
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"ok": False, "problems": [r.stdout[-300:] + r.stderr[-300:]]}


def window_opens(exe, env):
    """Start the real exe and see whether it stays up.

    Checks that the process survives rather than that a window title appears:
    on a headless runner there is no window manager to report one, and a
    process that dies at once is the failure worth catching.
    """
    p = subprocess.Popen([exe], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            if p.poll() is not None:
                return False, "exited immediately with %s" % p.returncode
            time.sleep(0.5)
        return True, ""
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    exe = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE)
    if not os.path.isfile(exe):
        print("SKIP: %s is not built. Run pyinstaller tsdec_gui.spec first."
              % exe)
        return 0

    print("exe: %s (%.1f MiB)" % (exe, os.path.getsize(exe) / 1048576.0),
          flush=True)

    box = tempfile.mkdtemp(prefix="tsdec-exe-")
    copy = os.path.join(box, "tsdec-gui.exe")
    shutil.copy2(exe, copy)
    env = bare_env()

    try:
        # ---- it finds and runs the decoder it carries ----
        r = run([copy, "--self-test"], env=env)
        out = r.stdout
        ok("the exe runs at all", r.returncode == 0,
           (out + r.stderr).strip()[-300:])
        ok("it is a frozen build", "frozen         : True" in out,
           out.strip()[-300:])
        ok("it finds a decoder with no help", "decoder exists : True" in out,
           out.strip()[-300:])
        ok("that decoder runs", "decoder runs   : yes" in out, out.strip()[-300:])
        ok("the decoder came from inside the exe", "_MEI" in out,
           out.strip()[-300:])

        # ---- a real decryption, by the exe itself ----
        sample = make_sample(box)
        if sample is None:
            print("\n== %d passed, %d failed ==" % (passed, failed), flush=True)
            return 1 if failed else 0
        plain, enc, cwl = sample

        out_path = os.path.join(box, "decrypted.ts")
        j = decrypt(copy, env, enc, cwl, out_path)
        ok("the exe decrypts a recording", j.get("ok"), str(j)[:300])
        res = j.get("result") or {}
        ok("it reports success", res.get("status") == 0,
           "%s / %s" % (res.get("status"), res.get("message")))
        ok("nothing was left scrambled", (res.get("dropped") or 0) == 0,
           "dropped=%s" % res.get("dropped"))

        if os.path.isfile(out_path):
            v = run([sys.executable,
                     os.path.join(DECODER_SRC, "tools", "verify.py"),
                     plain, out_path])
            ok("the output matches the plaintext", v.stdout.startswith("PASS"),
               v.stdout.strip()[:200])
        else:
            ok("the output matches the plaintext", False, "no output file")

        # ---- and a stop, the one thing packaging can quietly break ----
        # the console control event needs a console to be delivered from, and
        # a windowed build has none, so this is where the stop file earns its
        # keep. A frozen build that quietly fell back to killing the decoder
        # would still produce an output file, just the wrong one, and only
        # comparing against the input size catches that.
        big_out = os.path.join(box, "stopped.ts")
        j = decrypt(copy, env, enc, cwl, big_out, stop_after=1.5)
        res = j.get("result") or {}
        ok("the exe can stop a run", res.get("status") == 55,
           "%s / %s" % (res.get("status"), res.get("message")))
        ok("it says the work was kept", "kept" in (res.get("message") or ""),
           res.get("message", ""))

        if os.path.isfile(big_out):
            size = os.path.getsize(big_out)
            ok("a stopped run keeps packet aligned output",
               size > 0 and size % 188 == 0, "%d bytes" % size)
            ok("a stopped run keeps less than the whole file",
               size < os.path.getsize(enc), "%d of %d" % (size,
                                                          os.path.getsize(enc)))
        else:
            ok("a stopped run keeps packet aligned output", False,
               "no output file")

        # ---- the interface comes up ----
        up, why = window_opens(copy, env)
        ok("the window opens", up, why)
    finally:
        shutil.rmtree(box, ignore_errors=True)

    print("\n== %d passed, %d failed ==" % (passed, failed), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
