# PyInstaller build spec.
#
#     pip install pyinstaller
#     pyinstaller RedditArchiver.spec
#
# Produces dist/RedditArchiver.exe -- a single windowed executable, no console.
# ffmpeg is deliberately NOT bundled: it is ~80 MB, carries its own licence
# terms, and the app degrades gracefully and explains itself without it.

block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    # Archive.html and icon.ico are read at runtime via config.resource_dir(),
    # which resolves to _MEIPASS in a frozen build.
    datas=[
        ("Archive.html", "."),
        ("icon.ico", "."),
    ],
    hiddenimports=[
        # browser_cookie3 imports these lazily per browser; PyInstaller's static
        # analysis misses them and cookie auto-detect silently fails without.
        "browser_cookie3",
        "Crypto.Cipher.AES",
        "Cryptodome.Cipher.AES",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Trimmed: nothing here imports these, and they add tens of megabytes.
    excludes=["matplotlib", "numpy", "pandas", "scipy", "PIL", "pytest"],
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
    a.zipfiles,
    a.datas,
    [],
    name="RedditArchiver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX compression is a common antivirus false-positive trigger
    runtime_tmpdir=None,
    console=False,      # no console window -- this is the --windowed equivalent
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico",
)
