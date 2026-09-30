#!/usr/bin/env python3
"""Draw the application icon.

Generated rather than shipped as a binary so it stays reviewable and can be
tweaked without an image editor. Renders a small shape with Qt and writes both
the .ico the packager wants and a .png for the readme.

    python make_icon.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt                  # noqa: E402
from PySide6.QtGui import (QColor, QImage, QLinearGradient,  # noqa: E402
                           QPainter, QBrush)
from PySide6.QtWidgets import QApplication               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def draw(size):
    """The mark: a transport packet turning into a readable one.

    A rounded tile in the accent colour, with a stack of packets on it where
    the top two are lighter, which is the same idea the progress bar in the
    window is showing.
    """
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    s = float(size)
    r = s * 0.22

    # the tile
    grad = QLinearGradient(0, 0, s, s)
    grad.setColorAt(0.0, QColor("#3d7fc1"))
    grad.setColorAt(1.0, QColor("#2b5c96"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(QRectF(0, 0, s, s), r, r)

    # three packets, topmost brightest, as if a run just finished
    pad = s * 0.24
    w = s - 2 * pad
    h = max(1.0, s * 0.10)
    gap = max(1.0, s * 0.055)
    for i, alpha in enumerate((90, 170, 255)):
        y = pad + i * (h + gap)
        p.setBrush(QColor(255, 255, 255, alpha))
        p.drawRoundedRect(QRectF(pad, y, w, h), h / 2, h / 2)

    p.end()
    return img


def main():
    QApplication(sys.argv)
    out_ico = os.path.join(HERE, "tsdec-gui.ico")
    out_png = os.path.join(HERE, "shots", "icon.png")

    image = draw(256)
    image.save(out_ico, "ICO")

    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    image.save(out_png, "PNG")

    for path in (out_ico, out_png):
        print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
