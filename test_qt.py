#!/usr/bin/env python3
"""Exercise the native window without a screen.

Runs on the offscreen Qt platform plugin, so it works on a build machine and in
CI where there is no display. What it checks is the parts that can be wrong
quietly: that the window builds, that a run really starts a decoder and the
window follows it to the end, that a stop keeps the work, and that the pid
survey fills the table rather than leaving it empty.
"""

import os
import subprocess
import sys
import time

# must be set before QApplication is built, or Qt tries to find a display
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PySide6.QtCore import QEventLoop                   # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

import tsdec_gui_qt as gui                                     # noqa: E402

WORK = os.environ.get("GUI_WORK") or os.path.join(HERE, "testdata")

passed = failed = 0


def ok(name, cond, detail=""):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  [%s]" % detail) if (detail and not cond) else ""), flush=True)
    if cond:
        passed += 1
    else:
        failed += 1


def find_decoder():
    env_bin = os.environ.get("TSDEC_BIN")
    env_src = os.environ.get("TSDEC_SRC")
    candidates = [
        (env_bin, env_src),
        (os.path.join(HERE, "..", "tsdec", "tsdec.exe"), os.path.join(HERE, "..", "tsdec")),
        (os.path.join(HERE, "..", "tsdec", "tsdec"), os.path.join(HERE, "..", "tsdec")),
    ]
    for binary, source in candidates:
        if binary and os.path.isfile(binary) and \
                os.path.isfile(os.path.join(source or "", "tools", "mk_ts.py")):
            return os.path.normpath(binary), os.path.normpath(source)
    for binary, source in candidates:
        if binary and os.path.isfile(binary):
            return os.path.normpath(binary), source
    return None, None


TSDEC, TSDEC_SRC = find_decoder()


def build_sample():
    os.makedirs(WORK, exist_ok=True)
    plain = os.path.join(WORK, "plain.ts")
    enc = os.path.join(WORK, "enc.ts")
    cwl = os.path.join(WORK, "sample.cwl")
    if os.path.isfile(enc) and os.path.isfile(cwl):
        return plain, enc, cwl

    tools = os.path.join(TSDEC_SRC, "tools")
    if not os.path.isdir(tools):
        return None
    subprocess.run([sys.executable, os.path.join(tools, "mk_ts.py"),
                    "-o", plain, "-n", "30000"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, os.path.join(tools, "mk_test_pair.py"),
                    "-i", plain, "-o", enc, "-c", cwl,
                    "-t", TSDEC, "-w", "2000", "--quiet"], check=True)
    return plain, enc, cwl


def pump(app, seconds):
    """Let the window's timer run for a while."""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 50)
        time.sleep(0.01)


def wait_for(app, predicate, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        app.processEvents(QEventLoop.AllEvents, 50)
        time.sleep(0.01)
    return predicate()


def main():
    if not TSDEC:
        print("SKIP: tsdec not found. Set TSDEC_BIN, or clone tsdec next to "
              "this checkout.")
        return 0

    built = build_sample()
    if built is None:
        print("SKIP: no decoder source at %s, cannot build a sample. Set "
              "TSDEC_SRC." % TSDEC_SRC)
        return 0
    plain, enc, cwl = built
    print("sample: %s (%d bytes)" % (os.path.basename(enc), os.path.getsize(enc)),
          flush=True)

    app = QApplication.instance() or QApplication([])
    win = gui.MainWindow(TSDEC)
    win._update_decoder_label()
    win.show()
    pump(app, 0.3)

    ok("the window builds", win.isVisible())
    ok("start is available when idle", win.start_button.isEnabled())
    ok("stop is not available when idle", not win.stop_button.isEnabled())
    ok("progress is hidden when idle", not win.progress_group.isVisible())

    # ---- a real run, driven through the widgets ----
    out = os.path.join(WORK, "qt.out.ts")
    if os.path.exists(out):
        os.remove(out)
    win.input_edit.setText(enc)
    win.cwl_edit.setText(cwl)
    win.output_edit.setText(out)
    win.threads_spin.setValue(0)
    pump(app, 0.2)

    ok("the recording size is shown", bool(win.input_size.text()), win.input_size.text())

    win.start_button.click()
    # the state has to be read straight away: this recording finishes in about
    # fifteen milliseconds, so any event loop turn would already be too late
    ok("a run starts", win.job is not None)
    ok("start is disabled while running", not win.start_button.isEnabled())
    ok("stop is offered while running", win.stop_button.isEnabled())
    ok("the form is locked while running", not win.input_edit.isEnabled())

    finished = wait_for(app, lambda: win.job.state()["done"], 90)
    pump(app, 0.4)
    ok("the run finishes", finished)
    ok("start is offered again", win.start_button.isEnabled())
    ok("progress is shown afterwards", win.progress_group.isVisible())

    r = win.job.state()["result"] or {}
    ok("it reports success", r.get("status") == 0,
       "%s / %s" % (r.get("status"), r.get("message")))
    ok("every packet was decrypted",
       (r.get("decrypted") or 0) > 0 and r.get("dropped", 1) == 0,
       "decrypted=%s dropped=%s" % (r.get("decrypted"), r.get("dropped")))
    ok("it synced exactly once", r.get("syncs") == 1, "syncs=%s" % r.get("syncs"))
    ok("it wrote the output", os.path.isfile(out))
    ok("the verdict is shown", win.verdict.isVisible())
    ok("the verdict says what happened", "Decrypted" in win.verdict.text(),
       win.verdict.text())
    ok("the bar is full", win.progress.value() == 1000, str(win.progress.value()))
    ok("the log was kept", "decrypting" in win.log.toPlainText())

    # ---- the output really is the plaintext ----
    vr = subprocess.run([sys.executable,
                         os.path.join(TSDEC_SRC, "tools", "verify.py"),
                         plain, out], capture_output=True, text=True)
    ok("the output matches the plaintext", vr.stdout.startswith("PASS"),
       vr.stdout.strip().splitlines()[:1])

    # ---- overwriting an existing output asks, it does not just refuse ----
    # a refusal would be safe but unusable for a second run over the same name,
    # and silence would throw away whatever the first run managed to keep.
    # The stub answers instead of showing a dialog: question() blocks until
    # something answers it, and there is nobody at the keyboard here.
    asked = []
    orig_question = gui.QMessageBox.question

    def answer_no(*a, **k):
        asked.append(a[1] if len(a) > 1 else "")
        return QMessageBox.No

    gui.QMessageBox.question = staticmethod(answer_no)
    try:
        win.input_edit.setText(enc)
        win.output_edit.setText(out)
        pump(app, 0.1)
        before = os.path.getsize(out)
        win.start_button.click()
        pump(app, 0.2)
        ok("an existing output raises a question, not an error",
           asked == ["Replace the existing file?"], str(asked))
        ok("answering no leaves the file alone",
           not (win.job and not win.job.state()["done"]) and
           os.path.getsize(out) == before)
    finally:
        gui.QMessageBox.question = orig_question

    # and answering yes actually goes ahead
    asked.clear()

    def answer_yes(*a, **k):
        asked.append(a[1] if len(a) > 1 else "")
        return QMessageBox.Yes

    gui.QMessageBox.question = staticmethod(answer_yes)
    try:
        win.start_button.click()
        done = wait_for(app, lambda: win.job.state()["done"], 90)
        pump(app, 0.3)
        ok("answering yes replaces it", done and asked == ["Replace the existing file?"],
           str(asked))
        ok("and the replaced output is still correct",
           (win.job.state()["result"] or {}).get("status") == 0)
    finally:
        gui.QMessageBox.question = orig_question

    # ---- the pid survey fills the table ----
    win.output_edit.setText(os.path.join(WORK, "qt2.out.ts"))
    win.survey_button.click()
    got = wait_for(app, lambda: win.pid_table.rowCount() > 0, 60)
    pump(app, 0.2)
    ok("the survey fills the table", got, "%d rows" % win.pid_table.rowCount())
    ok("the survey finds the scrambled pid",
       any(win.pid_table.item(i, 2) and win.pid_table.item(i, 2).text() != "0"
           for i in range(win.pid_table.rowCount())))
    ok("the survey is closed off again", win.survey_button.isEnabled())

    # picking a pid should put it in the form, which is the whole point
    scrambled_row = None
    for i in range(win.pid_table.rowCount()):
        item = win.pid_table.item(i, 2)
        if item and item.text() not in ("0", "", None):
            scrambled_row = i
            break
    if scrambled_row is not None:
        btn = win.pid_table.cellWidget(scrambled_row, 5).findChild(QPushButton)
        btn.click()
        pump(app, 0.1)
        pid = win.pid_table.item(scrambled_row, 0).text()
        ok("picking a pid fills the form", pid in win.pids_edit.text(),
           "%r vs %r" % (pid, win.pids_edit.text()))
        btn.click()
        pump(app, 0.1)
        ok("picking it again clears it", pid not in win.pids_edit.text(),
           win.pids_edit.text())
    else:
        ok("picking a pid fills the form", False, "no scrambled row")

    # ---- stop keeps the work ----
    big = os.path.join(WORK, "big.enc.ts")
    bigcwl = os.path.join(WORK, "big.cwl")
    bigout = os.path.join(WORK, "qt.stopped.ts")
    if os.path.isfile(big):
        if os.path.exists(bigout):
            os.remove(bigout)
        win.pids_edit.clear()
        win.input_edit.setText(big)
        win.cwl_edit.setText(bigcwl)
        win.output_edit.setText(bigout)
        win.threads_spin.setValue(1)
        win.start_button.click()
        pump(app, 0.7)
        ok("the stop is offered on a long run", win.stop_button.isEnabled())
        win.stop_button.click()
        stopped = wait_for(app, lambda: win.job.state()["done"], 60)
        pump(app, 0.4)
        ok("the stop ends the run", stopped)

        r = win.job.state()["result"] or {}
        ok("it reports a stop, not a failure", r.get("status") == 55,
           "%s / %s" % (r.get("status"), r.get("message")))
        ok("the verdict says the work was kept",
           "kept" in (r.get("message") or ""), r.get("message", ""))
        ok("the window says it stopped", not win.progress_group.title().startswith(
            "Progress"), win.progress_group.title())
        ok("the verdict is shown after a stop", win.verdict.isVisible())

        size = os.path.getsize(bigout) if os.path.isfile(bigout) else 0
        ok("the partial output is packet aligned", size > 0 and size % 188 == 0,
           "%d bytes" % size)
        ok("the partial output is shorter than the input",
           0 < size < os.path.getsize(big))
        # the bar must not claim the run finished
        ok("the bar does not claim 100% on a stop", win.progress.value() < 1000,
           str(win.progress.value()))
    else:
        print("  (big sample missing, skipping the stop checks)", flush=True)

    # ---- the theme is not a no-op ----
    before = win.styleSheet()
    win._toggle_theme()
    pump(app, 0.1)
    ok("the theme switches", win.styleSheet() != before)
    win._toggle_theme()
    pump(app, 0.1)
    ok("and switches back", win.styleSheet() == before)

    win.close()
    pump(app, 0.2)

    print("\n== %d passed, %d failed ==" % (passed, failed), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
