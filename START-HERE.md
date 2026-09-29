tsdec-gui: start here

This is a graphical front end for TSDEC, the offline DVB transport stream
decrypter. It holds no decoder code: it runs the tsdec binary as a child
process and drives it through its command line, so the two projects can be
released and debugged independently.

The plan is a small local web interface that the browser renders, with the
decoder running beside it. Nothing to install beyond tsdec itself.

Reading order:

  README.md      what this is, and how it talks to tsdec
  CHANGELOG.md   what has changed

Related:

  https://github.com/prof-abdo/tsdec        the decoder
  https://github.com/prof-abdo/tsdec/releases
