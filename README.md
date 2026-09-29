# tsdec-gui

A graphical front end for [TSDEC](../tsdec), the offline decrypter for recorded
DVB transport streams.

TSDEC does the work. This is only the interface around it.

## Why a separate repository

TSDEC is a command line tool that ships as a single self-contained binary. The
interface is a different concern with a different lifecycle, and keeping it
apart means neither has to compromise: tsdec keeps its zero dependency, single
file build, and this can iterate on look and feel without touching the decoder.

They talk over the command line. This repository contains no copy of the
decoder and links against none of its code.

![The pid survey showing which pids are scrambled](shots/pids.png)

## How it drives TSDEC

TSDEC is a normal program, so the front end runs it as a child process and
reads its machine readable mode, `--json`, which writes one JSON object per line
on stdout:

```sh
tsdec --json -f control-words.cwl -i recording.ts -o clear.ts
```

- **Progress** — `progress` events arrive a few times a second with packets
  done, a total, a percentage, a throughput and an estimate. They become a real
  progress bar rather than one that fills on a guess.
- **Stop** — the front end sends a console control event, which TSDEC treats as
  Ctrl+C: stop, and keep what was decrypted. The partial output stays a whole
  number of packets, so a long recording can be stopped without losing the work
  already done. A kill is only ever the fallback, and it says so in the log.
- **Result** — a final `result` event carries the counts, the status code and a
  sentence saying what happened, which is what separates "could not sync the
  control word log" from "the recording is not encrypted" from "stopped on
  request".
- **PID survey** — `tsdec -a -i recording.ts` lists the PIDs in a recording with
  packet counts and how much of each is scrambled, so a transponder carrying
  several services can be narrowed down to the one wanted.

![A finished decryption](shots/decrypted.png)

Stopping a long recording keeps what was done, and says how far it got rather
than pretending the run completed:

![A stopped run](shots/stopped.png)

This is a documented contract in the [TSDEC
changelog](https://github.com/prof-abdo/tsdec/blob/main/CHANGELOG.md), not
scraped log text.

## Running it

```sh
python tsdec_gui.py
```

It prints the address to open and serves on `127.0.0.1` only. There is no
build step and no dependency beyond the Python standard library, so it runs
wherever Python 3 does.

The decoder is found on `PATH`, or next to this checkout, or wherever you point
it:

```sh
python tsdec_gui.py --tsdec /path/to/tsdec
```

## Tests

```sh
python test_gui.py
```

It builds a recording, runs it through a real `tsdec`, checks the output byte
for byte against the plaintext, and checks that a stop really does stop rather
than kill. It needs a `tsdec` binary, which it looks for the same way the app
does, or takes from `TSDEC_BIN`.

`shot.js` regenerates the screenshots in `shots/`. It drives a real browser, so
it needs Node and Playwright, which is a development need only and not
something the app itself depends on.

## Status

First release. The decoder side is done and released; see
[the tsdec changelog](https://github.com/prof-abdo/tsdec/blob/main/CHANGELOG.md)
for what is in the current version.

## Requirements

TSDEC on `PATH`, or pointed at directly. Version 2.2 or later, which is where
`--json` and the fix that makes a stop from a parent work on Windows landed.

## Licence

GPL v3, matching TSDEC.
