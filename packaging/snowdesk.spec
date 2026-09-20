# PyInstaller spec for SnowDesk (spec 12).
#
# Build from the repository root:
#
#     uv run pyinstaller packaging/snowdesk.spec --noconfirm
#
# then sign, notarize and staple as described in docs/spec.md section 12.
#
# Two things here are not obvious and were found by running the M0 spike
# (`--selftest` on the built bundle) rather than by reading PyInstaller's
# output, which reported success either way:
#
#   * The connector's Arrow result reader is a compiled Cython extension that
#     imports `snowflake.connector.snow_logging` and looks its type converters
#     up by name from C++.  None of that is visible to static analysis, so the
#     whole package is collected rather than a hand-written list, which would
#     only grow stale the next time the connector changes.
#   * PySide6's own hook pulls in QML, Quick and friends as frameworks, which
#     `excludes` does not touch because they are not imported modules.  They
#     are dropped from the tables below instead.

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))

hiddenimports = [
    # See the note above: the compiled Arrow reader's imports are invisible.
    *collect_submodules("snowflake.connector"),
    "snowflake.connector.snow_logging",
    "keyring",  # used by the connector's SSO token cache
]

datas = [
    *collect_data_files("snowflake.connector"),
    *collect_data_files("certifi"),
]

a = Analysis(
    [os.path.join(ROOT, "src", "snowdesk", "__main__.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "setuptools",
        "pip",
        "pytest",
        "IPython",
        "numpy",
        "pandas",
        "pyarrow",
    ],
    noarchive=False,
)

# PySide6 ships far more than a widgets app uses, and the connector ships its
# own C++ sources next to the built extension.  Neither belongs in the bundle.
_UNUSED = (
    "QtQml",
    "QtQuick",
    "QtVirtualKeyboard",
    "QtPdf",
    "QtOpenGL",
    "Qt3D",
    "QtWebEngine",
    "QtMultimedia",
    "QtCharts",
    "QtDataVisualization",
    "QtSensors",
    "QtTest",
    "QtDesigner",
    "QtSpatialAudio",
    "QtRemoteObjects",
    "nanoarrow_cpp",  # C++ sources, not the built extension
)


def _prune(entries):
    return [entry for entry in entries if not any(name in entry[0] for name in _UNUSED)]


a.binaries = _prune(a.binaries)
a.datas = _prune(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="snowdesk",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=os.path.join(ROOT, "packaging", "entitlements.plist"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="snowdesk",
)

# Drop a 1024x1024 PNG through packaging/make_icon.sh to produce icon.icns.
# Without one PyInstaller substitutes its own icon-windowed.icns, which is what
# a stock build shows in the Dock and in Finder.
_icon = os.path.join(ROOT, "packaging", "icon.icns")
icon = _icon if os.path.exists(_icon) else None

app = BUNDLE(
    coll,
    name="SnowDesk.app",
    icon=icon,
    bundle_identifier="dev.snowdesk.app",
    info_plist={
        "CFBundleShortVersionString": "0.1.0",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
