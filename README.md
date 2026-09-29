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

## How it drives TSDEC

TSDEC is a normal program, so the front end runs it as a child process:

```sh
tsdec -f control-words.cwl -i recording.ts -o clear.ts -P -v 1
```

- **Progress** — `-P` reports as it goes. The front end reads those lines and
  turns them into a progress bar with a throughput figure and an estimate.
- **Stop** — Ctrl+C asks TSDEC to stop rather than killing it. Whatever was
  decrypted before the stop is kept, packet aligned and playable, so stopping
  a long recording never throws away the work already done.
- **Exit status** — the numeric result distinguishes "could not sync the control
  word log" from "the input was not encrypted" from "stopped on request", so
  the interface can say what actually happened instead of "error".

Running `tsdec -h` lists every option, and `tsdec -a -i recording.ts` prints a
PID survey the interface can offer as a picker when a transponder carries
several services.

## Status

Under construction. The decoder side is done and released; see
[the tsdec changelog](https://github.com/prof-abdo/tsdec/blob/main/CHANGELOG.md)
for what is in the current version.

## Requirements

TSDEC on `PATH`, or pointed at directly. Version 2.0 or later, which is where
the stop path and the progress reporting landed.

## Licence

GPL v3, matching TSDEC.
