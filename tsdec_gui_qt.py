#!/usr/bin/env python3
"""tsdec-gui: the native front end for the tsdec offline decrypter.

Running the decoder, and stopping it without losing the work already done, is
in tsdec_core.py. This is only the window around it.

Nothing here decodes anything. The decoder runs as a child process and is
driven through its --json output; see the README.
"""

import os
import sys
import time

from PySide6.QtCore import QObject, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
    QWidget)

import tsdec_core
from tsdec_core import Manager, find_tsdec, human_size, survey, validate

APP_NAME = "tsdec-gui"
APP_VERSION = "0.2.0"

# what a stop reports, so the window can say it without knowing the codes
RET_CANCELED = tsdec_core.RET_CANCELED


def fmt_duration(seconds):
    """Seconds as something a person reads at a glance.

    A short recording finishes in well under a second, and rounding that to
    "0s" reads as if nothing happened rather than as a quick run.
    """
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 1:
        return "%dms" % round(seconds * 1000)
    if seconds < 10:
        return "%.1fs" % seconds
    s = int(round(seconds))
    if s < 60:
        return "%ds" % s
    m = int(s // 60)
    if m < 60:
        return "%dm %02ds" % (m, s % 60)
    return "%dh %02dm" % (m // 60, m % 60)


def human_int(n):
    return "{:,}".format(int(n)).replace(",", " ")


def _only_problem_is_existing_output(problems):
    """True when the sole complaint is that the output is already there.

    The overwrite case is a question, not an error, so the window wants to ask
    about it rather than just refusing. Any other problem is a real error and
    must not be waved through by answering yes to a dialog about the wrong
    thing.
    """
    return len(problems) == 1 and problems[0].startswith("output already exists:")


class SurveyWorker(QObject):
    """Runs a pid survey off the main thread.

    A survey reads the whole recording, which on a large one is long enough
    that doing it inline would freeze the window and look like a hang.
    """

    finished = Signal(object, object)          # rows, error

    def __init__(self, tsdec, path):
        super().__init__()
        self.tsdec = tsdec
        self.path = path

    @Slot()
    def run(self):
        rows, err = survey(self.tsdec, self.path)
        self.finished.emit(rows, err)


class MainWindow(QMainWindow):
    def __init__(self, tsdec_path, tsdec_bundled=False):
        super().__init__()
        self.tsdec = tsdec_path
        self.tsdec_bundled = tsdec_bundled
        self.manager = Manager(tsdec_path)

        self.job = None
        self.log_index = 0          # how far the log has been drained
        self.picked = set()         # pids the run is limited to
        self.survey_rows = []
        self.survey_thread = None
        self.survey_worker = None
        self.dark = False

        self.setWindowTitle("%s  —  offline transport stream decrypter"
                            % APP_NAME)
        self.resize(880, 760)
        self.setMinimumSize(680, 560)

        self._build()

        # polling rather than signals: the core stays free of Qt, and a tick
        # every tenth of a second costs nothing while a decode is running
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

        self._apply_theme()

    # ------------------------------------------------------------------ ui
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        outer.addWidget(self._header())

        split = QSplitter(Qt.Vertical)
        split.addWidget(self._setup_card())
        split.addWidget(self._output_area())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 380])
        outer.addWidget(split, 1)

        self.status = self.statusBar()
        self.status.showMessage("ready")

    def _header(self):
        bar = QHBoxLayout()
        title = QLabel(APP_NAME)
        f = QFont()
        f.setPointSize(15)
        f.setBold(True)
        title.setFont(f)
        bar.addWidget(title)

        sub = QLabel("offline transport stream decrypter")
        sub.setProperty("class", "subtitle")
        bar.addWidget(sub)
        bar.addStretch(1)

        self.decoder_label = QLabel()
        self.decoder_label.setProperty("class", "subtitle")
        self.decoder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bar.addWidget(self.decoder_label)

        self.theme_button = QToolButton()
        self.theme_button.setText("◐")
        self.theme_button.setToolTip("Switch between light and dark")
        self.theme_button.clicked.connect(self._toggle_theme)
        bar.addWidget(self.theme_button)
        return self._wrap(bar)

    def _setup_card(self):
        box = QGroupBox("Setup")
        grid = QGridLayout(box)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(1, 1)

        row = 0
        self.input_edit, self.input_size = self._path_row(
            grid, row, "Recording", "*.ts *.trp *.mpg *.m2t *.vpr;;"
            "Transport stream (*.ts *.trp *.mpg *.m2t *.vpr);;All files (*)")
        row += 1

        self.cwl_edit, cwl_hint = self._path_row(
            grid, row, "Control word log", "*.cwl;;All files (*)")
        cwl_hint.setText("the keys logged elsewhere")
        row += 1

        self.output_edit, out_hint = self._path_row(
            grid, row, "Output", "*.ts;;Transport stream (*.ts);;All files (*)",
            save=True)
        out_hint.setText("leave empty for <name>_decrypted.ts next to the input")
        row += 1

        opts = QHBoxLayout()
        opts.setSpacing(14)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 256)
        self.threads_spin.setSpecialValueText("auto")
        self.threads_spin.setValue(0)
        self.threads_spin.setToolTip("0 uses every core")
        opts.addWidget(self._labelled("Threads", self.threads_spin))

        self.blocker_spin = QSpinBox()
        self.blocker_spin.setRange(1, 65536)
        self.blocker_spin.setValue(300)
        self.blocker_spin.setToolTip("Parity blocks tolerated before a resync")
        opts.addWidget(self._labelled("Parity blocker", self.blocker_spin))

        self.pids_edit = QLineEdit()
        self.pids_edit.setPlaceholderText("0x100, 0x101")
        self.pids_edit.setToolTip("empty means every pid")
        opts.addWidget(self._labelled("Only pids", self.pids_edit, 1))

        self.resync_check = QCheckBox("Resync past corrupt packets")
        self.resync_check.setToolTip(
            "Carry on after corrupt packets instead of dropping them")
        opts.addWidget(self.resync_check)
        opts.addStretch(1)
        grid.addLayout(opts, row, 0, 1, 3)

        # the buttons live in the header so they stay put while the form
        # scrolls, but they belong with the run, so put them under the form
        actions = QHBoxLayout()
        self.start_button = QPushButton("Decrypt")
        self.start_button.setObjectName("primary")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self._start)
        actions.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip(
            "Ask the decoder to stop and keep what is already decrypted")
        self.stop_button.clicked.connect(self._stop)
        actions.addWidget(self.stop_button)

        self.survey_button = QPushButton("Survey pids")
        self.survey_button.clicked.connect(self._survey)
        actions.addWidget(self.survey_button)
        actions.addStretch(1)

        self.open_button = QPushButton("Open output folder")
        self.open_button.setProperty("class", "quiet")
        self.open_button.clicked.connect(self._open_output_folder)
        actions.addWidget(self.open_button)
        grid.addLayout(actions, row + 1, 0, 1, 3)

        self.problems = QLabel()
        self.problems.setWordWrap(True)
        self.problems.setProperty("class", "problem")
        self.problems.hide()
        grid.addWidget(self.problems, row + 2, 0, 1, 3)

        # watching the input change is enough to keep the size hint honest
        self.input_edit.textChanged.connect(self._update_size_hint)
        self._update_size_hint()
        return box

    def _path_row(self, grid, row, label, filtername, save=False):
        cap = QLabel(label)
        cap.setProperty("class", "field-label")
        grid.addWidget(cap, row, 0)

        edit = QLineEdit()
        edit.setPlaceholderText("choose a file")
        edit.setProperty("class", "path")
        grid.addWidget(edit, row, 1)

        browse = QToolButton()
        browse.setText("…" if not save else "Save as…")
        browse.setToolTip("Choose where to write" if save else "Browse")
        browse.clicked.connect(
            (lambda: self._choose_output()) if save
            else (lambda: self._browse(edit, filtername)))
        grid.addWidget(browse, row, 2)

        hint = QLabel("")
        hint.setProperty("class", "subtitle")
        hint.setWordWrap(True)
        grid.addWidget(hint, row + 1, 1, 1, 2)
        return edit, hint

    def _labelled(self, text, widget, stretch=0):
        col = QVBoxLayout()
        col.setSpacing(3)
        cap = QLabel(text)
        cap.setProperty("class", "field-label")
        col.addWidget(cap)
        col.addWidget(widget)
        if stretch:
            col.addStretch(1)
        holder = QWidget()
        holder.setLayout(col)
        return holder

    def _output_area(self):
        col = QVBoxLayout()
        col.setSpacing(12)

        # ---- progress ----
        self.progress_group = QGroupBox("Progress")
        pv = QVBoxLayout(self.progress_group)
        pv.setContentsMargins(14, 12, 14, 12)
        pv.setSpacing(9)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        pv.addWidget(self.progress)

        stats = QHBoxLayout()
        stats.setSpacing(18)
        self.pct_value = self._stat(stats, "0%", "done")
        self.rate_value = self._stat(stats, "—", "throughput")
        self.eta_value = self._stat(stats, "—", "remaining")
        self.sync_value = self._stat(stats, "—", "syncs")
        stats.addStretch(1)
        pv.addLayout(stats)

        self.verdict = QLabel()
        self.verdict.setWordWrap(True)
        self.verdict.setProperty("class", "verdict")
        self.verdict.hide()
        pv.addWidget(self.verdict)

        col.addWidget(self.progress_group)
        self.progress_group.hide()

        # ---- pids ----
        self.pid_group = QGroupBox("Pids in this recording")
        pg = QVBoxLayout(self.pid_group)
        pg.setContentsMargins(14, 12, 14, 12)
        pg.setSpacing(6)

        self.pid_table = QTableWidget(0, 6)
        self.pid_table.setHorizontalHeaderLabels(
            ["Pid", "Packets", "Scrambled", "Share", "CC errors", ""])
        self.pid_table.verticalHeader().hide()
        self.pid_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pid_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pid_table.setAlternatingRowColors(True)
        header = self.pid_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for c in (1, 2, 3, 4):
            header.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.pid_table.setMinimumHeight(120)
        pg.addWidget(self.pid_table)

        hint = QLabel("Pick a scrambled pid to limit the run to it.")
        hint.setProperty("class", "subtitle")
        hint.setWordWrap(True)
        pg.addWidget(hint)
        col.addWidget(self.pid_group)
        self.pid_group.hide()

        # ---- log ----
        self.log_group = QGroupBox("Log")
        lg = QVBoxLayout(self.log_group)
        lg.setContentsMargins(14, 12, 14, 12)
        lg.setSpacing(6)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(9)
        self.log.setFont(mono)
        lg.addWidget(self.log)

        row = QHBoxLayout()
        row.addStretch(1)
        clear = QPushButton("Clear")
        clear.setProperty("class", "quiet")
        clear.clicked.connect(self.log.clear)
        row.addWidget(clear)
        lg.addLayout(row)

        col.addWidget(self.log_group, 1)
        return self._wrap(col)

    def _stat(self, row, value, caption):
        col = QVBoxLayout()
        col.setSpacing(1)
        val = QLabel(value)
        f = QFont()
        f.setPointSize(13)
        f.setBold(True)
        val.setFont(f)
        col.addWidget(val)
        cap = QLabel(caption)
        cap.setProperty("class", "subtitle")
        col.addWidget(cap)
        holder = QWidget()
        holder.setLayout(col)
        row.addWidget(holder)
        return val

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        w.setLayout(layout)
        w.setProperty("class", "plain")
        return w

    # ------------------------------------------------------------- actions
    def _form(self):
        return {
            "input": self.input_edit.text(),
            "cwl": self.cwl_edit.text(),
            "output": self.output_edit.text().strip(),
            "threads": str(self.threads_spin.value()),
            "blocker": str(self.blocker_spin.value()),
            "pids": self.pids_edit.text(),
            "resync": self.resync_check.isChecked(),
        }

    def _browse(self, edit, filtername):
        name, _ = QFileDialog.getOpenFileName(
            self, "Choose a file", edit.text().strip() or os.path.expanduser("~"),
            filtername)
        if name:
            edit.setText(name)

    def _choose_output(self):
        start = self.output_edit.text().strip()
        if not start:
            start = self._suggest_output()
        name, _ = QFileDialog.getSaveFileName(
            self, "Where to write", start, "Transport stream (*.ts);;All files (*)")
        if name:
            self.output_edit.setText(name)

    def _suggest_output(self):
        src = self.input_edit.text().strip()
        if not src:
            return os.path.join(os.path.expanduser("~"), "decrypted.ts")
        base, _ext = os.path.splitext(src)
        return base + "_decrypted.ts"

    def _update_size_hint(self):
        path = self.input_edit.text().strip()
        if path and os.path.isfile(path):
            self.input_size.setText(human_size(os.path.getsize(path)))
            if not self.output_edit.text().strip():
                self.output_edit.setPlaceholderText(self._suggest_output())
        else:
            self.input_size.setText("")

    def _open_output_folder(self):
        path = self.output_edit.text().strip()
        if not path:
            path = self._suggest_output()
        folder = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "No folder", "%s does not exist." % folder)
            return
        if os.name == "nt":
            os.startfile(folder)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _start(self):
        if self.job and not self.job.state()["done"]:
            return

        form = self._form()
        if not form["output"].strip():
            form["output"] = self._suggest_output()

        info, problems = validate(form)

        # A finished or partly written file is sitting there. Say so and let
        # the caller decide, rather than either refusing forever or quietly
        # destroying whatever the last run managed to save.
        if problems and _only_problem_is_existing_output(problems):
            if self._confirm_overwrite(form["output"]):
                info, problems = validate(form, allow_overwrite=True)
            else:
                return

        if problems:
            self._show_problems(problems)
            return
        self._show_problems([])

        self.log.appendPlainText("decrypting %s → %s"
                                 % (info["input"], info["output"]))
        self.log.appendPlainText("$ %s" % " ".join(
            [os.path.basename(self.tsdec)] + info["args"]))
        self.verdict.hide()
        self.progress_group.show()
        self.progress.setValue(0)
        self.progress.setFormat("")
        self.pct_value.setText("0%")
        self.rate_value.setText("—")
        self.eta_value.setText("—")
        self.sync_value.setText("—")

        reply = self.manager.start(info["args"])
        if "error" in reply:
            self._show_problems([reply["error"]])
            return

        self.job = self.manager.current()
        self.log_index = 0
        self.set_running(True)
        self.status.showMessage("decrypting…")

    def _stop(self):
        self.stop_button.setEnabled(False)
        self.status.showMessage("stopping, finishing what is already in flight…")
        self.log.appendPlainText("stop requested")
        reply = self.manager.stop()
        if "error" in reply:
            QMessageBox.information(self, "Nothing to stop", reply["error"])
            self.set_running(False)

    def _survey(self):
        path = self.input_edit.text().strip()
        if not path or not os.path.isfile(path):
            self._show_problems(["choose a recording first"])
            return
        if self.survey_thread is not None:
            return

        self.survey_button.setEnabled(False)
        self.status.showMessage("surveying pids in %s…" % os.path.basename(path))
        self._show_problems([])

        self.survey_thread = QThread(self)
        self.survey_worker = SurveyWorker(self.tsdec, path)
        self.survey_worker.moveToThread(self.survey_thread)
        self.survey_thread.started.connect(self.survey_worker.run)
        self.survey_worker.finished.connect(self._survey_done)
        self.survey_worker.finished.connect(self.survey_thread.quit)
        self.survey_thread.finished.connect(self._survey_cleanup)
        self.survey_thread.start()

    @Slot(object, object)
    def _survey_done(self, rows, err):
        if err:
            self._show_problems([err])
            return
        self.survey_rows = rows or []
        self._fill_pid_table()
        self._show_problems([])
        scrambled = sum(1 for r in self.survey_rows if r["scrambled"] > 0)
        self.status.showMessage("%d pids, %d scrambled"
                                % (len(self.survey_rows), scrambled))

    def _survey_cleanup(self):
        self.survey_thread.deleteLater()
        self.survey_worker.deleteLater()
        self.survey_thread = None
        self.survey_worker = None
        self.survey_button.setEnabled(True)

    def _fill_pid_table(self):
        rows = self.survey_rows
        self.pid_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            scrambled = r["scrambled"] > 0
            cells = [
                r["pid"],
                human_int(r["packets"]),
                human_int(r["scrambled"]),
                "%.1f%%" % r["share"],
                human_int(r["cc_errors"]),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                    item.setData(Qt.UserRole, r["pid"])
                    f = QFont()
                    f.setBold(scrambled)
                    item.setFont(f)
                else:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.pid_table.setItem(i, c, item)

            if scrambled:
                btn = QPushButton("use" if r["pid"] not in self.picked else "clear")
                btn.setProperty("class", "quiet")
                btn.clicked.connect(
                    lambda _checked, pid=r["pid"], b=btn: self._toggle_pid(pid, b))
                holder = QWidget()
                lay = QHBoxLayout(holder)
                lay.setContentsMargins(4, 2, 4, 2)
                lay.addWidget(btn)
                lay.addStretch(1)
                self.pid_table.setCellWidget(i, 5, holder)

        self.pid_group.show()
        if rows:
            self.pid_group.setTitle("Pids in this recording  (%d)" % len(rows))

    def _toggle_pid(self, pid, button):
        if pid in self.picked:
            self.picked.discard(pid)
        else:
            self.picked.add(pid)
        button.setText("clear" if pid in self.picked else "use")
        self.pids_edit.setText(",".join(sorted(self.picked)))

    # ------------------------------------------------------------- polling
    def _tick(self):
        job = self.job
        if job is None:
            return

        lines, self.log_index = job.pop_since(self.log_index)
        for line in lines:
            self._consume(line)

        st = job.state()
        if st["done"]:
            self.set_running(False)
            if st["result"]:
                self._finish(st["result"])

    def _consume(self, line):
        kind = line.get("event")
        if kind == "progress":
            self._apply_progress(line)
        elif kind == "log":
            self.log.appendPlainText(line.get("text", ""))
        elif kind == "result":
            # the result is applied by _finish, once the process is reaped
            pass

    def _apply_progress(self, p):
        total = p.get("total") or 0
        done = p.get("done") or 0
        pct = min(100.0, (done / total) * 100) if total else 0.0
        self.progress.setValue(int(pct * 10))
        self.pct_value.setText("%d%%" % round(pct))
        self.rate_value.setText("%d MiB/s" % round(p.get("mib_per_second") or 0))
        eta = p.get("eta")
        self.eta_value.setText(fmt_duration(eta) if eta and eta > 0 else "—")
        self.sync_value.setText(str(p.get("syncs") or 0))

    def _finish(self, r):
        total = r.get("total") or 0
        packets = r.get("packets") or 0
        # On a stop the run did not finish, so the bar stays where it got to.
        # Forcing it to 100% would claim work that was never done, which is the
        # one thing a progress bar must not do.
        pct = min(100.0, (packets / total) * 100) if total else 100.0
        finished = r.get("status") == 0

        self.progress.setValue(int(pct * 10))
        self.pct_value.setText("100%" if finished else "%d%%" % round(pct))
        self.rate_value.setText("%d MiB/s" % round(r.get("mib_per_second") or 0))
        self.eta_value.setText("—")
        self.sync_value.setText(str(r.get("syncs") or 0))

        self.progress_group.setTitle("Result" if finished else "Stopped")
        self.verdict.show()

        if finished:
            self.verdict.setProperty("class", "verdict good")
            self.verdict.setText("Decrypted %s of %s packets in %s." % (
                human_int(r.get("decrypted") or 0), human_int(packets),
                fmt_duration(r.get("seconds"))))
            self.status.showMessage("done")
        elif r.get("status") == RET_CANCELED:
            self.verdict.setProperty("class", "verdict warn")
            self.verdict.setText("%s %s packets (%d%%) were written." % (
                r.get("message") or "Stopped.", human_int(packets), round(pct)))
            self.status.showMessage("stopped at %d%%" % round(pct))
        else:
            self.verdict.setProperty("class", "verdict bad")
            self.verdict.setText(r.get("message")
                                 or ("Failed with status %s" % r.get("status")))
            self.status.showMessage("failed")
        self._restyle(self.verdict)

        self.log.appendPlainText("—")
        self.log.appendPlainText(self.verdict.text())
        self.log.appendPlainText(
            "%s packets, %s encrypted, %s left scrambled, %s sync(s), "
            "%s resync(s), %s" % (
                human_int(packets), human_int(r.get("encrypted") or 0),
                human_int(r.get("dropped") or 0), r.get("syncs") or 0,
                r.get("resyncs") or 0, fmt_duration(r.get("seconds"))))
        if r.get("corrupt"):
            self.log.appendPlainText("%s packets had a missing sync byte"
                                     % human_int(r["corrupt"]))

    def set_running(self, running):
        self.start_button.setEnabled(not running)
        self.survey_button.setEnabled(not running and self.survey_thread is None)
        self.stop_button.setEnabled(running)
        for w in (self.input_edit, self.cwl_edit, self.output_edit,
                  self.threads_spin, self.blocker_spin, self.pids_edit,
                  self.resync_check):
            w.setEnabled(not running)
        if not running:
            self.progress_group.setTitle("Result")
        else:
            self.progress_group.setTitle("Progress")

    def _confirm_overwrite(self, path):
        size = human_size(os.path.getsize(path)) if os.path.isfile(path) else "?"
        reply = QMessageBox.question(
            self, "Replace the existing file?",
            "%s already exists (%s).\n\nReplace it?" % (path, size),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return reply == QMessageBox.Yes

    def _show_problems(self, problems):
        if problems:
            self.problems.setText("  ".join(problems))
            self.problems.show()
        else:
            self.problems.hide()

    # -------------------------------------------------------------- theme
    def _toggle_theme(self):
        self.dark = not self.dark
        self._apply_theme()

    def _apply_theme(self):
        if self.dark:
            self._stylesheet("""
                QWidget { background: #14161a; color: #e6e8ec;
                          font-size: 13px; }
                QGroupBox {
                    border: 1px solid #2a2e35; border-radius: 10px;
                    margin-top: 14px; padding: 12px 12px 12px 12px;
                    background: #191c21;
                }
                QGroupBox::title {
                    subcontrol-origin: margin; left: 12px; padding: 0 5px;
                    color: #98a0ad;
                }
                QLineEdit, QSpinBox, QPlainTextEdit, QTableWidget {
                    background: #1f232a; border: 1px solid #2f343d;
                    border-radius: 6px; padding: 5px 7px;
                    selection-background-color: #2d5a8a;
                }
                QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {
                    border-color: #3d7fc1;
                }
                QPushButton {
                    background: #262b33; border: 1px solid #333943;
                    border-radius: 6px; padding: 7px 16px;
                }
                QPushButton:hover:!disabled { background: #2f353f; }
                QPushButton:disabled { color: #5b626d; }
                QPushButton#primary {
                    background: #2f6fbd; border: 1px solid #3d7fc1; color: white;
                    font-weight: bold;
                }
                QPushButton#primary:hover:!disabled { background: #3a7fd0; }
                QPushButton#danger {
                    background: #a43838; border: 1px solid #c14a4a; color: white;
                    font-weight: bold;
                }
                QPushButton#danger:hover:!disabled { background: #bb4141; }
                QPushButton[class="quiet"] { background: transparent;
                    border: 1px solid #333943; color: #b6bdc8; }
                QToolButton {
                    background: #262b33; border: 1px solid #333943;
                    border-radius: 6px; padding: 5px 9px;
                }
                QToolButton:hover { background: #2f353f; }
                QProgressBar {
                    background: #1f232a; border: none; border-radius: 5px;
                }
                QProgressBar::chunk {
                    background: #3d7fc1; border-radius: 5px;
                }
                QTableWidget {
                    gridline-color: #262b33;
                    alternate-background-color: #1c2026;
                }
                QHeaderView::section {
                    background: #22262d; color: #98a0ad; padding: 6px 8px;
                    border: none; border-bottom: 1px solid #2f343d;
                }
                QLabel[class="subtitle"] { color: #79818e; }
                QLabel[class="field-label"] { color: #98a0ad; }
                QLabel[class="problem"] {
                    color: #e2a3a3; background: #2a1d1d;
                    border: 1px solid #5a2f2f; border-radius: 6px;
                    padding: 7px 9px;
                }
                QLabel[class="verdict"] {
                    border-radius: 6px; padding: 8px 10px; border: 1px solid;
                }
                QLabel[class="verdict"][class~="good"] {
                    color: #a8d5b0; background: #17251c; border-color: #2f5a3d;
                }
                QLabel[class="verdict"][class~="warn"] {
                    color: #e0c48f; background: #262014; border-color: #5a4a2f;
                }
                QLabel[class="verdict"][class~="bad"] {
                    color: #e2a3a3; background: #2a1d1d; border-color: #5a2f2f;
                }
                QSplitter::handle { background: transparent; height: 10px; }
                QStatusBar { color: #98a0ad; }
                QStatusBar::item { border: none; }
            """)
        else:
            self._stylesheet("""
                QWidget { background: #f6f7f9; color: #1c2026;
                          font-size: 13px; }
                QGroupBox {
                    border: 1px solid #e0e3e8; border-radius: 10px;
                    margin-top: 14px; padding: 12px 12px 12px 12px;
                    background: #ffffff;
                }
                QGroupBox::title {
                    subcontrol-origin: margin; left: 12px; padding: 0 5px;
                    color: #6b7280;
                }
                QLineEdit, QSpinBox, QPlainTextEdit, QTableWidget {
                    background: #ffffff; border: 1px solid #d5d9e0;
                    border-radius: 6px; padding: 5px 7px;
                    selection-background-color: #cfe0f5;
                }
                QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {
                    border-color: #3d7fc1;
                }
                QPushButton {
                    background: #ffffff; border: 1px solid #d5d9e0;
                    border-radius: 6px; padding: 7px 16px;
                }
                QPushButton:hover:!disabled { background: #eef1f5; }
                QPushButton:disabled { color: #a8afba; }
                QPushButton#primary {
                    background: #2f6fbd; border: 1px solid #2f6fbd; color: white;
                    font-weight: bold;
                }
                QPushButton#primary:hover:!disabled { background: #3a7fd0; }
                QPushButton#danger {
                    background: #b03a3a; border: 1px solid #b03a3a; color: white;
                    font-weight: bold;
                }
                QPushButton#danger:hover:!disabled { background: #c24444; }
                QPushButton[class="quiet"] { background: transparent;
                    border: 1px solid #d5d9e0; color: #5b6472; }
                QToolButton {
                    background: #ffffff; border: 1px solid #d5d9e0;
                    border-radius: 6px; padding: 5px 9px;
                }
                QToolButton:hover { background: #eef1f5; }
                QProgressBar {
                    background: #e3e7ed; border: none; border-radius: 5px;
                }
                QProgressBar::chunk {
                    background: #2f6fbd; border-radius: 5px;
                }
                QTableWidget {
                    gridline-color: #edf0f4;
                    alternate-background-color: #fafbfc;
                }
                QHeaderView::section {
                    background: #f0f2f5; color: #6b7280; padding: 6px 8px;
                    border: none; border-bottom: 1px solid #e0e3e8;
                }
                QLabel[class="subtitle"] { color: #6b7280; }
                QLabel[class="field-label"] { color: #6b7280; }
                QLabel[class="problem"] {
                    color: #a3282a; background: #fdf0f0;
                    border: 1px solid #f0c9c9; border-radius: 6px;
                    padding: 7px 9px;
                }
                QLabel[class="verdict"] {
                    border-radius: 6px; padding: 8px 10px; border: 1px solid;
                }
                QLabel[class="verdict"][class~="good"] {
                    color: #1d6b34; background: #eef8f1; border-color: #bfe3cb;
                }
                QLabel[class="verdict"][class~="warn"] {
                    color: #8a6410; background: #fdf6e6; border-color:
                    #ecd9a8;
                }
                QLabel[class="verdict"][class~="bad"] {
                    color: #a3282a; background: #fdf0f0; border-color: #f0c9c9;
                }
                QSplitter::handle { background: transparent; height: 10px; }
                QStatusBar { color: #6b7280; }
                QStatusBar::item { border: none; }
            """)

    def _stylesheet(self, css):
        self.setStyleSheet(css)
        # a stylesheet does not always re-evaluate the dynamic properties, so
        # ask for it explicitly where a colour depends on one
        for w in (self.problems, self.verdict):
            self._restyle(w)

    @staticmethod
    def _restyle(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _update_decoder_label(self):
        name = os.path.basename(self.tsdec)
        if self.tsdec_bundled:
            self.decoder_label.setText("decoder %s (bundled)" % name)
        else:
            self.decoder_label.setText("decoder %s" % name)

    # ------------------------------------------------------------ shortcuts
    def _install_shortcuts(self):
        act = QAction(self)
        act.setShortcut(QKeySequence("Ctrl+O"))
        act.triggered.connect(lambda: self._browse(
            self.input_edit, "*.ts *.trp *.mpg *.m2t *.vpr;;All files (*)"))
        self.addAction(act)

        act2 = QAction(self)
        act2.setShortcut(QKeySequence("Ctrl+K"))
        act2.triggered.connect(self._browse_cwl)
        self.addAction(act2)

    def _browse_cwl(self):
        self._browse(self.cwl_edit, "*.cwl;;All files (*)")

    def keyPressEvent(self, event):
        # ctrl+s is the natural thing to press in a desktop app
        if event.matches(QKeySequence.Save):
            self._choose_output()
            return
        if event.matches(QKeySequence.Open):
            self._browse(self.input_edit, "*.ts *.trp *.mpg *.m2t *.vpr;;All files (*)")
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        job = self.job
        if job and not job.state()["done"]:
            reply = QMessageBox.question(
                self, "A decode is running",
                "Stop it and close?\n\nThe part already decrypted is kept.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.manager.stop()
        event.accept()


def selftest(tsdec_path):
    """Prove the frozen build can find and run its decoder.

    This is the check that matters for a packaged exe and the one that cannot
    be made by reading the code: whether the decoder really got into the
    bundle, got unpacked somewhere writable, and is runnable from there. It
    writes to stdout, which a windowed build has, so the release job can read
    it and fail loudly instead of shipping an exe that opens to an error.
    """
    import subprocess

    print("frozen         : %s" % tsdec_core.is_frozen())
    print("bundle dir     : %s" % (tsdec_core.bundle_dir() or "-"))
    print("decoder        : %s" % tsdec_path)
    print("decoder exists : %s" % os.path.isfile(tsdec_path))

    if not os.path.isfile(tsdec_path):
        return 1

    try:
        r = subprocess.run([tsdec_path, "-a", "-i", os.path.abspath(__file__)],
                           capture_output=True, text=True, timeout=60)
        print("decoder runs   : yes (status %d)" % r.returncode)
    except OSError as e:
        print("decoder runs   : NO  (%s)" % e)
        return 1
    except subprocess.TimeoutExpired:
        print("decoder runs   : NO  (it hung on a survey of its own source)")
        return 1

    print("OK")
    return 0


def headless(tsdec_path, argv):
    """Decrypt without a window and print the result as JSON.

    This is what makes the packaged build testable and scriptable. A windowed
    exe has no console to type into, but it can still be given arguments, and
    a decode is a perfectly good thing to ask for from a shell: from a build
    job that wants to know the exe really decrypts, or from someone who would
    rather not click through a window for a hundred files.

    It is the same Manager the window uses, so nothing here takes a shortcut
    the interface does not.
    """
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="tsdec-gui --decrypt", add_help=False)
    ap.add_argument("--decrypt", action="store_true")
    ap.add_argument("-i", "--input", required=True)
    ap.add_argument("-f", "--cwl", required=True)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("-b", "--blocker", default="300")
    ap.add_argument("-t", "--threads", default="0")
    ap.add_argument("-p", "--pids", default="")
    ap.add_argument("-r", "--resync", action="store_true")
    # stop after this many seconds, to check the stop path in a build
    ap.add_argument("--stop-after", type=float, default=0.0)
    opts = ap.parse_args(argv)

    info, problems = validate({
        "input": opts.input, "cwl": opts.cwl, "output": opts.output,
        "threads": opts.threads, "blocker": opts.blocker,
        "pids": opts.pids, "resync": opts.resync,
    }, allow_overwrite=True)
    if problems:
        print(json.dumps({"ok": False, "problems": problems}))
        return 2

    manager = Manager(tsdec_path)
    manager.start(info["args"])
    job = manager.current()

    if opts.stop_after > 0:
        time.sleep(opts.stop_after)
        manager.stop()

    while not job.state()["done"]:
        time.sleep(0.05)

    st = job.state()
    print(json.dumps({"ok": True, "result": st["result"] or {}}))
    return st["result"].get("status", 1) if st["result"] else 1


def main():
    QApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv)

    args = sys.argv[1:]

    # a decoder named on the command line wins, then one bundled inside a
    # frozen build, then PATH, then a sibling checkout
    explicit = None
    want_selftest = False
    headless_mode = False
    for i, a in enumerate(args):
        if a == "--tsdec" and i + 1 < len(args):
            explicit = args[i + 1]
        elif a.startswith("--tsdec="):
            explicit = a.split("=", 1)[1]
        elif a == "--self-test":
            want_selftest = True
        elif a == "--decrypt":
            headless_mode = True

    tsdec = find_tsdec(explicit)

    if headless_mode:
        if not tsdec:
            print("the tsdec decoder was not found", file=sys.stderr)
            return 1
        return headless(tsdec, args)

    if want_selftest:
        return selftest(tsdec)

    if not tsdec:
        QMessageBox.critical(
            None, APP_NAME,
            "The tsdec decoder was not found.\n\n"
            "Put tsdec.exe next to this program, or pass --tsdec <path>.")
        return 1

    win = MainWindow(tsdec, tsdec_bundled=bool(tsdec_core.bundle_dir()))
    win._install_shortcuts()
    win._update_decoder_label()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
