"""Driving the tsdec decoder, with no interface attached.

This holds no decoding code. It runs the tsdec binary as a child process and
drives it through the command line:

  progress  read from the JSON lines tsdec emits with --json
  stop      Ctrl+C on the child, which tsdec treats as a request rather than
            a kill, so whatever was decrypted is kept
  result    the exit status distinguishes "could not sync" from "nothing was
            encrypted" from "stopped on request"

Nothing here knows whether it is being used by the web front end or the Qt
one, so the two share the same behaviour and, more usefully, the same tests.
The only thing that differs is how a caller finds out what is happening, which
is why everything is polled from a lock rather than pushed through a signal.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

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

HERE = os.path.dirname(os.path.abspath(__file__))


def status_text(code):
    if code in STATUS_TEXT:
        return STATUS_TEXT[code]
    return "tsdec exited with status %d" % code


def human_size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TiB" % n


def is_frozen():
    return getattr(sys, "frozen", False)


def bundle_dir():
    """Where a frozen build unpacked its data files, or None when running
    from source."""
    return getattr(sys, "_MEIPASS", None)


def find_tsdec(explicit=None):
    """Locate the decoder.

    Order matters. A frozen build ships the decoder inside itself, and that is
    the one it should use, so the bundle is consulted before PATH; a source
    checkout has no bundle and falls through to PATH and then to a sibling
    repository, which is how this is developed.
    """
    if explicit:
        return explicit

    env = os.environ.get("TSDEC_BIN")
    if env and os.path.isfile(env):
        return env

    packed = bundle_dir()
    if packed:
        name = "tsdec.exe" if os.name == "nt" else "tsdec"
        candidate = os.path.join(packed, name)
        if os.path.isfile(candidate):
            return candidate

    from shutil import which
    found = which("tsdec") or which("tsdec.exe")
    if found:
        return found

    for name in ("tsdec.exe", "tsdec"):
        candidate = os.path.normpath(os.path.join(HERE, "..", "tsdec", name))
        if os.path.isfile(candidate):
            return candidate

    return None


def validate(form, allow_overwrite=False):
    """Check the request before handing anything to a subprocess.

    Returns (info, problems). info is None when there is anything to say, so a
    caller cannot accidentally start a job from a half filled in form.

    An output that already exists is a problem unless the caller says it is
    allowed. Overwriting is a real risk here rather than a formality: a
    half finished run leaves a partial file, and starting again would destroy
    the part that did get written, which is the very thing the stop button
    exists to protect.
    """
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
        elif ts and os.path.abspath(out) == os.path.abspath(ts):
            problems.append("output must not overwrite the input")
        elif not allow_overwrite and os.path.isfile(out):
            problems.append("output already exists: %s" % out)

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

    # -n names a service rather than pids, and the decoder resolves it through
    # the program tables. Passing both is refused rather than one winning
    # quietly, which is the same rule the command line applies.
    program = (form.get("program") or "").strip()
    if pids and program:
        problems.append("choose a service or individual pids, not both")
    elif program:
        args += ["-n", program]
    elif pids:
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


PID_ROW = re.compile(
    r"\s*(0x[0-9a-fA-F]+)\s+(\d+)\s+(\d+)\s+([\d.]+)%\s+(\d+)")


def parse_pid_survey(text):
    """Turn the decoder's pid survey into rows.

    The decoder prefixes every line with its name, which has to come off
    before the columns are matched.
    """
    rows = []
    for line in text.splitlines():
        if line.startswith("TSDEC: "):
            line = line[7:]
        m = PID_ROW.match(line)
        if m:
            rows.append({
                "pid": m.group(1),
                "packets": int(m.group(2)),
                "scrambled": int(m.group(3)),
                "share": float(m.group(4)),
                "cc_errors": int(m.group(5)),
            })
    return rows


def survey(tsdec_path, input_path):
    """Ask the decoder what is in a recording.

    Returns (rows, error). A survey of a large recording takes a while, so
    callers that care about responsiveness run this off the main thread.
    """
    if not input_path or not os.path.isfile(input_path):
        return None, "recording not found: %s" % input_path
    try:
        r = subprocess.run([tsdec_path, "-a", "-i", input_path],
                           capture_output=True, text=True, timeout=300)
    except OSError as e:
        return None, str(e)
    except subprocess.TimeoutExpired:
        return None, "the survey took too long; the recording may be very large"
    return parse_pid_survey(r.stderr), None


def decoder_has_stop_file(tsdec_path):
    """Whether this decoder understands -S, checked once.

    An older build would treat -S as a usage error and refuse to start, so
    the flag is only ever used when the binary in hand is known to have it.
    Cached per path because it is a subprocess and this runs on every start.
    """
    key = os.path.normpath(tsdec_path)
    cached = _STOP_FILE_SUPPORT.get(key)
    if cached is not None:
        return cached
    try:
        r = subprocess.run([tsdec_path, "-h"], capture_output=True, text=True,
                           timeout=30)
        # the help goes to stderr, which is where a person would see it too
        text = (r.stdout or "") + (r.stderr or "")
        supported = "-S <file>" in text or " -S " in text
    except (OSError, subprocess.SubprocessError):
        supported = False
    _STOP_FILE_SUPPORT[key] = supported
    return supported


_STOP_FILE_SUPPORT = {}


def _has_console():
    """Whether a console control event can be delivered from this process.

    On Windows a frozen build has no console whatever, so a signal is not
    merely unreliable there, it has nowhere to go. Everywhere else, including
    a console application on Windows, the signal is both available and
    cheaper, so it stays the first choice. Deciding this is the difference
    between a stop that keeps the work and one that silently degrades into a
    kill.
    """
    return os.name != "nt" or not is_frozen()


def programs(tsdec_path, input_path):
    """Ask the decoder what services a recording holds.

    Returns (programs, error). A transponder normally carries several services
    with their own control words, so this is what turns a capture into
    something a person can pick a service out of, rather than a pile of
    interleaved pids to read off a hex dump. The tables are control
    information and are not scrambled, so this works on a recording that has
    not been decrypted.
    """
    if not input_path or not os.path.isfile(input_path):
        return None, "recording not found: %s" % input_path
    try:
        r = subprocess.run([tsdec_path, "-a", "-i", input_path, "--json",
                            "-v", "0"],
                           capture_output=True, text=True, timeout=300)
    except OSError as e:
        return None, str(e)
    except subprocess.TimeoutExpired:
        return None, "the survey took too long; the recording may be very large"

    for line in reversed((r.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("event") == "programs":
            return obj.get("programs") or [], None

    return None, "the decoder did not report any program tables"


class Job:
    """One run of tsdec, with its output readable while it is going."""

    def __init__(self, tsdec_path, args, use_stop_file=None):
        self.tsdec = tsdec_path
        self.args = args
        self.lock = threading.Lock()
        self.proc = None
        self.lines = []            # everything tsdec said, for the log pane
        self.result = None         # final statistics dict
        self.done = False
        self.started = 0.0
        self.id = 0
        # A stop delivered by a file works everywhere, including from a
        # windowed build with no console, which a signal does not. It is a
        # temporary file, created here and removed when the run ends. Only used
        # when the decoder actually has -S.
        self.stop_file = None
        if use_stop_file is None:
            use_stop_file = _has_console() is False and \
                decoder_has_stop_file(tsdec_path)
        if use_stop_file:
            fd, self.stop_file = tempfile.mkstemp(prefix="tsdec-stop-",
                                                  suffix=".flag")
            os.close(fd)
            os.unlink(self.stop_file)     # tsdec watches for it to appear
        self.use_stop_file = bool(self.stop_file)

    def _clear_stop_file(self):
        if self.stop_file and os.path.exists(self.stop_file):
            try:
                os.unlink(self.stop_file)
            except OSError:
                pass

    def _emit(self, obj):
        with self.lock:
            self.lines.append(obj)

    def append_log(self, text):
        self._emit({"event": "log", "text": text})

    def run(self):
        self.started = time.time()
        cmd = [self.tsdec, "--json", "-v", "0"]
        if self.stop_file:
            cmd += ["-S", self.stop_file]
        cmd += self.args
        self.append_log("$ " + " ".join(cmd))
        try:
            # CREATE_NEW_PROCESS_GROUP on Windows, without it a console
            # control event aimed at this child would also hit us. No line
            # buffering rather than buffering=1: the decoder emits whole JSON
            # lines as they complete, and line buffering a pipe is only
            # honoured for interactive streams on some platforms, which would
            # stall the progress feed.
            creationflags = 0
            if os.name == "nt" and not self.stop_file:
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, bufsize=0,
                creationflags=creationflags)
        except OSError as e:
            self._clear_stop_file()
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
        self._clear_stop_file()

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

        # tsdec treats Ctrl+C as "please stop, keep what is done". Delivering
        # that needs a console to deliver it from, and a windowed build has
        # none, so where the stop file is available use it and keep the signal
        # for the cases where it is reliable. A hard terminate would throw
        # away the packets already decrypted, so it is only the fallback.
        if self.stop_file:
            try:
                with open(self.stop_file, "wb"):
                    pass
            except OSError as e:
                self.append_log("could not ask for a stop (%s), killing" % e)
                try:
                    proc.kill()
                except OSError:
                    pass
                return False
            return self._await_exit()

        try:
            if os.name == "nt":
                # tsdec installs a SIGINT handler via signal(), which Windows
                # backs with SetConsoleCtrlHandler. CTRL_BREAK maps to
                # SIGBREAK and would bypass it, so ask for CTRL_C, and target
                # the child's own process group so the event does not also
                # reach this process.
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

        return self._await_exit()

    def _await_exit(self):
        # if it has not noticed within a few seconds, it is wedged
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.append_log("still running after the stop, killing it")
            try:
                self.proc.kill()
            except OSError:
                pass
        return True

    def pop_since(self, index):
        """Lines added since index, and the index to use next time.

        Read and advance under one lock, so a caller that polls cannot miss a
        line that arrives between reading the list and remembering how far it
        got. That is the whole reason this is not a slice of self.lines.
        """
        with self.lock:
            out = self.lines[index:]
            return out, len(self.lines)

    def history(self, limit=400):
        """The tail of the log, for a caller that has just connected and has
        nothing to catch up from."""
        with self.lock:
            return list(self.lines[-limit:])

    def state(self):
        """Done, running and the result, as one consistent read."""
        with self.lock:
            return {
                "done": self.done,
                "running": bool(self.proc and self.proc.poll() is None),
                "result": self.result,
                "started": self.started,
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
        return job
