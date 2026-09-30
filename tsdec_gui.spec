# PyInstaller spec for the native Windows build.
#
#   pyinstaller --noconfirm tsdec_gui.spec
#
# Two things here are deliberate and would otherwise get lost.
#
# The decoder is bundled. tsdec.exe is copied in as a data file, and
# find_tsdec() looks in the unpack directory before it looks at PATH, so a
# person who has downloaded one thing gets a working program. Pointing
# --tsdec at a different build still works, which is what you want for trying
# a newer decoder.
#
# Most of Qt is left out. PySide6 pulls in roughly 200 MB of modules this
# window never touches: WebEngine, Quick, 3D, multimedia, the charts. Excluding
# them is most of the difference between a 90 MB download and a 25 MB one.

import os

block_cipher = None

# the decoder to embed; pass the path to a build, or let the release workflow
# drop one next to this file
DECODER = os.environ.get("TSDEC_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(SPEC)), "..", "tsdec", "tsdec.exe")

datas = [("tsdec_core.py", ".")]
if os.path.isfile(DECODER):
    datas.append((DECODER, "."))
else:
    print("warning: no decoder at %s, the build will expect it on PATH"
          % DECODER)

# Qt modules that are never imported by this program. Removing them is what
# keeps the download to a size people will actually fetch.
excludes = [
    # the web front end has its own entry point and is not part of this binary
    "tsdec_gui",
    # Qt we do not use
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQml", "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSerialPort", "PySide6.QtWebSockets",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtTest", "PySide6.QtUiTools",
    "PySide6.QtSql", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSensors", "PySide6.QtStateMachine", "PySide6.QtHttpServer",
    # python we do not use
    "tkinter", "unittest", "pydoc", "doctest", "test",
    "numpy", "PIL", "setuptools", "pip",
]

a = Analysis(
    ["tsdec_gui_qt.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="tsdec-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=False is what makes it a window rather than a terminal, and the
    # child process gets its own pipes regardless so --json still works
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="tsdec-gui.ico",
    version=os.path.join(os.path.dirname(os.path.abspath(SPEC)),
                         "version_info.txt"),
)
