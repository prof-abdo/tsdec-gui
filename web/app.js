/* tsdec-gui front end.
 *
 * The server owns the job; this polls it while one is running and stops when
 * there is nothing to show. tsdec emits progress as JSON lines, so the numbers
 * on screen are measured rather than estimated from a timer. */

const $ = (id) => document.getElementById(id);

let poll = null;
let pickerTarget = null;
let pickedPids = new Set();
let lastPacketLine = 0;

/* ---------- theme ---------- */
const savedTheme = localStorage.getItem("tsdec-theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;

$("theme").addEventListener("click", () => {
  const now = document.documentElement.dataset.theme === "light" ? "" : "light";
  document.documentElement.dataset.theme = now;
  localStorage.setItem("tsdec-theme", now);
});

/* ---------- helpers ---------- */
function fmtInt(n) {
  return (n || 0).toLocaleString();
}

function fmtSize(bytes) {
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0, v = bytes || 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + " " + u[i];
}

function fmtDuration(seconds) {
  if (seconds === undefined || seconds === null || seconds < 0) return "—";
  // a short recording finishes in well under a second, and rounding that to
  // "0s" reads as if nothing happened rather than as a quick run
  if (seconds < 1) return Math.round(seconds * 1000) + "ms";
  if (seconds < 10) return seconds.toFixed(1) + "s";
  const s = Math.round(seconds);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m " + (s % 60) + "s";
  return Math.floor(m / 60) + "h " + (m % 60) + "m";
}

function log(text, cls) {
  const el = $("log");
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = text;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function clearLog() {
  $("log").textContent = "";
}

$("clear").addEventListener("click", clearLog);

/* ---------- form state ---------- */
function collect() {
  return {
    input: $("input").value.trim(),
    cwl: $("cwl").value.trim(),
    output: $("output").value.trim(),
    threads: $("threads").value.trim(),
    blocker: $("blocker").value.trim(),
    pids: Array.from(pickedPids).join(","),
    resync: $("resync").checked,
  };
}

/* Suggest an output name when the input is set and output is empty. */
function suggestOutput() {
  const inp = $("input").value.trim();
  if (inp && !$("output").value.trim()) {
    const dot = inp.lastIndexOf(".");
    const base = dot > 0 ? inp.slice(0, dot) : inp;
    $("output").value = base + "_decrypted.ts";
  }
}
$("input").addEventListener("change", suggestOutput);

/* Show the recording size once we can read it, so a wrong path is obvious. */
$("input").addEventListener("change", async () => {
  const path = $("input").value.trim();
  const hint = $("input-hint");
  if (!path) { hint.textContent = ""; return; }
  try {
    const r = await fetch("/api/browse?path=" + encodeURIComponent(path));
    const j = await r.json();
    hint.textContent = j.entries && j.entries.length === 1 && !j.entries[0].dir
      ? fmtSize(j.entries[0].size) + "  ·  "
        + fmtInt(Math.floor(j.entries[0].size / 188)) + " packets"
      : "";
  } catch { hint.textContent = ""; }
});

function showProblems(list) {
  const el = $("problems");
  if (!list || !list.length) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.innerHTML = "<ul>" + list.map((p) => "<li>" + escapeHtml(p) + "</li>").join("") + "</ul>";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- actions ---------- */
async function startRun(body) {
  let r = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let j = await r.json();

  // A file is already sitting at the output name. Ask rather than destroy it:
  // a stopped run leaves a partial file there, and overwriting it without
  // asking would throw away exactly the work the stop button protects.
  if (j.problems && j.problems.length === 1 &&
      j.problems[0].startsWith("output already exists:")) {
    if (!confirm(j.problems[0] + "\n\nReplace it?")) return null;
    r = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ overwrite: true }, body)),
    });
    j = await r.json();
  }
  return j;
}

$("start").addEventListener("click", async () => {
  clearLog();
  showProblems(null);

  const j = await startRun(collect());
  if (j === null) return;

  if (!j.ok) {
    showProblems(j.problems || ["could not start"]);
    return;
  }

  log("decrypting " + j.info.input + " → " + j.info.output, "dim");
  lastPacketLine = 0;
  $("progress-card").hidden = false;
  $("progress-title").textContent = "Progress";
  $("start").disabled = true;
  $("stop").hidden = false;
  $("verdict").hidden = true;
  $("fill").classList.add("indeterminate");
  startPolling();
});

$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  const r = await fetch("/api/stop", { method: "POST" });
  const j = await r.json();
  if (!j.ok) showProblems(j.problems);
  else log("stop requested, finishing what is already in flight", "warn");
});

$("analyze").addEventListener("click", async () => {
  const path = $("input").value.trim();
  if (!path) { showProblems(["choose a recording first"]); return; }
  showProblems(null);
  log("surveying PIDs in " + path, "dim");

  const r = await fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input: path }),
  });
  const j = await r.json();

  if (!j.ok) { showProblems(j.problems); return; }

  const tbody = $("pid-rows");
  tbody.innerHTML = "";
  for (const p of j.pids) {
    const tr = document.createElement("tr");
    if (p.scrambled > 0) tr.classList.add("scrambled");
    if (pickedPids.has(p.pid)) tr.classList.add("picked");
    tr.innerHTML =
      "<td>" + p.pid + "</td><td>" + fmtInt(p.packets) + "</td><td>" +
      fmtInt(p.scrambled) + "</td><td>" + p.share.toFixed(1) + "%</td><td>" +
      fmtInt(p.cc_errors) + "</td>";
    const td = document.createElement("td");
    if (p.scrambled > 0) {
      const b = document.createElement("button");
      b.className = "ghost";
      b.textContent = pickedPids.has(p.pid) ? "clear" : "use";
      b.addEventListener("click", () => {
        if (pickedPids.has(p.pid)) pickedPids.delete(p.pid);
        else pickedPids.add(p.pid);
        $("analyze").click();               /* re-render the picked state */
      });
      td.appendChild(b);
    }
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  $("pids-card").hidden = j.pids.length === 0;
  log("found " + j.pids.length + " PIDs", "dim");
});

/* ---------- polling ---------- */
function startPolling() {
  if (poll) return;
  poll = setInterval(tick, 200);
  tick();
}

function stopPolling() {
  if (poll) { clearInterval(poll); poll = null; }
}

async function tick() {
  let s;
  try {
    s = await (await fetch("/api/state")).json();
  } catch {
    return;
  }

  let sawProgress = false;
  for (const line of s.lines.slice(lastPacketLine)) {
    lastPacketLine++;
    if (line.event === "progress") {
      applyProgress(line);
      sawProgress = true;
    } else if (line.event === "log") {
      log(line.text);
    } else if (line.event === "error") {
      log(line.message, "bad");
    }
  }

  if (sawProgress) $("fill").classList.remove("indeterminate");

  if (s.done) {
    stopPolling();
    $("start").disabled = false;
    $("stop").hidden = true;
    $("stop").disabled = false;
    $("fill").classList.remove("indeterminate");
    if (s.result) applyResult(s.result);
  }
}

function applyProgress(p) {
  const pct = p.total ? Math.min(100, (p.done / p.total) * 100) : 0;
  $("fill").style.width = pct.toFixed(1) + "%";
  $("pct").textContent = pct.toFixed(0) + "%";
  $("rate").textContent = p.mib_per_second.toFixed(0) + " MiB/s";
  $("eta").textContent = p.total ? fmtDuration(p.eta) : "—";
  $("synced").textContent = fmtInt(p.syncs);
}

function applyResult(r) {
  // On a stop the run did not finish, so the bar has to stay where it really
  // got to. Forcing it to 100% would claim work that was never done, which is
  // the one thing a progress bar must not do.
  const total = r.total || 0;
  const pct = total ? Math.min(100, (r.packets / total) * 100) : 100;
  const finished = r.status === 0;

  $("fill").style.width = pct.toFixed(1) + "%";
  $("pct").textContent = finished ? "100%" : pct.toFixed(0) + "%";
  $("pct-label").textContent = finished ? "done" : "stopped at";
  $("progress-title").textContent = finished ? "Result" : "Stopped";
  $("rate").textContent = (r.mib_per_second || 0).toFixed(0) + " MiB/s";
  $("eta").textContent = "—";
  $("synced").textContent = fmtInt(r.syncs);

  const v = $("verdict");
  v.hidden = false;

  if (r.status === 0) {
    v.className = "verdict ok";
    v.textContent = "Decrypted " + fmtInt(r.decrypted) + " of " +
      fmtInt(r.packets) + " packets in " + fmtDuration(r.seconds) + ".";
  } else if (r.status === 55) {
    v.className = "verdict warn";
    v.textContent = (r.message || "Stopped.") + " " +
      fmtInt(r.packets) + " packets (" + pct.toFixed(0) + "%) were written.";
  } else {
    v.className = "verdict bad";
    v.textContent = r.message || ("Failed with status " + r.status) + ".";
  }

  log("---");
  log(v.textContent, r.status === 0 ? "good" : r.status === 55 ? "warn" : "bad");
  log(fmtInt(r.packets) + " packets, " + fmtInt(r.encrypted) + " encrypted, " +
      fmtInt(r.dropped) + " left scrambled, " + fmtInt(r.syncs) + " sync(s), " +
      fmtInt(r.resyncs) + " resync(s), " + fmtDuration(r.seconds), "dim");
  if (r.corrupt) log(fmtInt(r.corrupt) + " packets had a missing sync byte", "warn");
}

/* ---------- file picker ---------- */
document.querySelectorAll(".pick").forEach((btn) => {
  btn.addEventListener("click", async () => {
    pickerTarget = btn.dataset.target;
    const start = $(pickerTarget).value.trim() || "";
    $("picker-title").textContent =
      pickerTarget === "output" ? "Choose where to write" : "Choose a file";
    await openPicker(start);
  });
});

$("picker-close").addEventListener("click", closePicker);
$("picker").addEventListener("click", (e) => {
  if (e.target === $("picker")) closePicker();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closePicker();
});

function closePicker() { $("picker").hidden = true; }

async function openPicker(path) {
  const r = await fetch("/api/browse?path=" + encodeURIComponent(path || ""));
  const j = await r.json();

  if (j.error) {
    // the path was a file, so start from the folder holding it
    const cut = path.replace(/[\\/][^\\/]*$/, "");
    return openPicker(cut || "");
  }

  $("picker-path").textContent = j.path;
  const list = $("picker-list");
  list.innerHTML = "";

  if (j.parent) {
    const up = document.createElement("button");
    up.className = "picker-item";
    up.innerHTML = '<span class="nm">..</span>';
    up.addEventListener("click", () => openPicker(j.parent));
    list.appendChild(up);
  }

  for (const e of j.entries) {
    if (e.name.startsWith(".") && !e.dir) continue;
    const b = document.createElement("button");
    b.className = "picker-item";
    b.innerHTML =
      '<span class="nm">' + (e.dir ? "📁 " : "") + escapeHtml(e.name) + "</span>" +
      (e.dir ? "" : '<span class="sz">' + fmtSize(e.size) + "</span>");
    b.addEventListener("click", () => {
      if (e.dir) return openPicker(e.path);
      $(pickerTarget).value = e.path;
      if (pickerTarget === "input") suggestOutput();
      closePicker();
    });
    list.appendChild(b);
  }

  $("picker").hidden = false;
}

/* ---------- first paint ---------- */
$("threads").placeholder = "auto (" + (navigator.hardwareConcurrency || "?") + " cores)";
log("ready", "dim");
