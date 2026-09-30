# Changelog

All notable changes to tsdec-gui are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[semantic versioning](https://semver.org/).

## [0.2.0] - 2026-09-30

A native Windows program, which is what this should have been from the start.

### Added

- **A native window.** `tsdec-gui.exe`, built with PySide6, is a real desktop
  application: file pickers, a pid table you can pick from, light and dark,
  and the progress a decoder is actually making. No browser, no server, no
  port to think about.
- **The decoder travels with it.** `tsdec.exe` is inside the executable, so
  there is one file to download and nothing to put on PATH. Pointing it at a
  different decoder still works, which is how you try a newer build.
- **A headless mode.** `--decrypt` runs a decode with no window and prints the
  result as JSON, and `--self-test` reports which decoder it found and whether
  it runs. Both are how the release job proves the packaged program works, and
  the second is useful on its own for checking a build.

### Fixed

- **A stop did not stop in the packaged program.** Ctrl+C is a console control
  event, and a windowed program has no console to deliver one from, so on the
  real Windows build the stop either did nothing or degraded into a kill. A kill
  throws away every packet already decrypted, which is the one thing the stop
  button exists to prevent. The front end now asks for a stop with `tsdec -S`,
  a file appearing, which works the same on every platform and needs no
  terminal. The decoder side of that is [tsdec
  2.3.0](https://github.com/prof-abdo/tsdec/releases/tag/v2.3.0).
- **Nothing asked before replacing an existing output.** A run that was
  stopped leaves a partial file, and starting again silently destroyed the
  part that had been written. It asks now, which is the only behaviour that is
  both safe and usable.
- **A progress bar that claimed 100% on a stopped run.** It now stays where it
  got to, which is the one thing a progress bar must not get wrong.

### Changed

- The interesting half, running the decoder and stopping it, moved to
  `tsdec_core.py` and is shared with the web front end, so the two cannot
  drift apart. The web front end is still there and still works; it is now the
  one to reach for from a shell or over ssh.
- `test_exe.py` tests the packaged executable as an artefact, in an empty
  folder with nothing on PATH. It is how the stop bug was found: every check
  against the Python modules passed, and only the frozen build told the truth.

## [0.1.0] - 2026-09-29

First release.

### Added

- **Decrypt.** Pick a `.ts` recording and a `.cwl` control word log, choose an
  output, and run it. TSDEC does the work as a child process; this is the
  interface around it.
- **Live progress.** A real progress bar with percentage, throughput and an
  estimate of what is left, fed by the decoder's `--json` progress events.
- **Stop that keeps the work.** Stopping asks the decoder to finish cleanly, so
  the partial output stays packet aligned and playable instead of being thrown
  away. This is the button the 0.4.1 Win32 GUI had and the main thing this
  exists to replace.
- **PID survey.** Lists the PIDs in a recording with how much of each is
  scrambled, and lets the run be narrowed to the wanted ones, for a transponder
  carrying several services.
- **Tuning.** Thread count, CW blocker in bytes, and resync after losing sync.
- **Light and dark**, and it follows the system preference.
- **Log pane**, so a run that ends oddly can be read.

## Links

- Decoder: [prof-abdo/tsdec](https://github.com/prof-abdo/tsdec)
- Decoder changelog: [CHANGELOG.md](https://github.com/prof-abdo/tsdec/blob/main/CHANGELOG.md)

[0.2.0]: https://github.com/prof-abdo/tsdec-gui/releases/tag/v0.2.0
[0.1.0]: https://github.com/prof-abdo/tsdec-gui/releases/tag/v0.1.0
