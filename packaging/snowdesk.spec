# PyInstaller spec for SnowDesk (spec 12).
#
# The Snowflake connector ships compiled extensions and Arrow components that
# PyInstaller does not find on its own, hence the explicit hidden imports and
# collected data files.  Build with:
#
#     uv run pyinstaller packaging/snowdesk.spec --noconfirm
#
# then sign, notarize and staple as described in docs/spec.md section 12.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = [
    "snowflake.connector",
    "snowflake.connector.arrow_iterator",
    "snowflake.connector.nanoarrow_arrow_iterator",
    "snowflake.connector.auth",
    "snowflake.connector.auth.webbrowser",
    "snowflake.connector.auth.keypair",
    "snowflake.connector.auth.okta",
    "snowflake.connector.vendored.requests",
    *collect_submodules("snowflake.connector.auth"),
]

datas = [
    *collect_data_files("snowflake.connector"),
    *collect_data_files("certifi"),
]

a = Analysis(
    ["../src/snowdesk/__main__.py"],
    pathex=["../src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # PySide6 ships far more than SnowDesk uses; excluding the heavy modules
    # keeps the bundle to a sane size.
    excludes=[
        "PySide6.Qt3DCore",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "tkinter",
    ],
    noarchive=False,
)

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
    entitlements_file="packaging/entitlements.plist",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="snowdesk",
)

app = BUNDLE(
    coll,
    name="SnowDesk.app",
    icon=None,
    bundle_identifier="dev.snowdesk.app",
    info_plist={
        "CFBundleShortVersionString": "0.1.0",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
