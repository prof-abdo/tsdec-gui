# Changelog

All notable changes to tsdec-gui are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[semantic versioning](https://semver.org/).

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

[0.1.0]: https://github.com/prof-abdo/tsdec-gui/releases/tag/v0.1.0
