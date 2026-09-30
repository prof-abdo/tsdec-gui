tsdec-gui: start here

This is a graphical front end for TSDEC, the offline DVB transport stream
decrypter. It holds no decoder code: it runs the tsdec binary as a child
process and drives it through its command line, so the two projects can be
released and debugged independently.

To use it, download tsdec-gui.exe from the releases page and run it. The
decoder is inside the program, so that one file is the whole installation.

The interface is a native window built with PySide6. There is also a local web
version in tsdec_gui.py, which shares the code that runs the decoder and is
what to reach for from a shell.

Reading order:

  README.md      what this is, and how it talks to tsdec
  CHANGELOG.md   what has changed

Where things are:

  tsdec_gui_qt.py   the window
  tsdec_gui.py      the web version
  tsdec_core.py     running and stopping the decoder, shared by both
  test_qt.py        the window, driven headless
  test_exe.py       the packaged exe as an artefact

Related:

  https://github.com/prof-abdo/tsdec        the decoder
  https://github.com/prof-abdo/tsdec/releases
  https://github.com/prof-abdo/tsdec-gui/releases
