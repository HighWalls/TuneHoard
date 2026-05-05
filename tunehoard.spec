# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller tunehoard.spec
# Output: dist/TuneHoard.exe (Windows) / dist/TuneHoard (mac/linux)

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)


# ── Bundled data ──────────────────────────────────────────────────────
# server.py serves dashboard/tunehoard/tunehoard.html via FileResponse and
# also mounts /static onto dashboard/tunehoard/. Bundle the whole `dashboard/`
# tree at the same relative path so both code paths resolve at runtime.
datas = [('dashboard', 'dashboard')]

# librosa ships pickled regression weights + sample-rate kernels in package
# data; numba bundles its CUDA intrinsics tables. Both are loaded by path
# at import-time and PyInstaller doesn't see them through static analysis.
datas += collect_data_files('librosa')
datas += collect_data_files('numba')

# pydantic v2 reads its own dist-info at import time for version detection;
# without --copy-metadata it'll raise PackageNotFoundError inside a frozen
# build. fastapi + uvicorn read theirs the same way for `--version` output
# and importlib.metadata lookups.
datas += copy_metadata('pydantic')
datas += copy_metadata('fastapi')
datas += copy_metadata('uvicorn')


# ── Hidden imports ────────────────────────────────────────────────────
# librosa/numba/llvmlite import each other lazily through `_lazy_loader`
# and `numba.core.entrypoints`; PyInstaller's static analyzer misses both
# chains, so we collect every submodule.
hiddenimports = [
    # Audio analysis stack
    'librosa',
    'numba',
    'llvmlite',
    'soxr',
    'pooch',
    # FastAPI / uvicorn protocol plugins — uvicorn picks these by string at
    # runtime ("uvicorn.protocols.http.h11_impl" via importlib), so static
    # analysis can't see them. Listing them explicitly is the canonical fix.
    'uvicorn',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.http.httptools_impl',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.websockets_impl',
    'uvicorn.protocols.websockets.wsproto_impl',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.loops.asyncio',
    'uvicorn.loops.uvloop',  # POSIX-only; PyInstaller silently skips on Windows
    'uvicorn.logging',
    # Network/IO backends uvicorn[standard] pulls in
    'httptools',
    'websockets',
    'watchfiles',
    # tkinter file picker (used by /api/browse). Usually picked up automatically
    # but listing it stops a stray "no module named _tkinter" on minimal Pythons.
    'tkinter',
    'tkinter.filedialog',
]

# Pull in every submodule of librosa for good measure — covers the lazy
# re-exports in librosa.feature / librosa.effects.
hiddenimports += collect_submodules('librosa')


# ── Build graph ───────────────────────────────────────────────────────
a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Empty excludes on purpose: a slow first-launch unpacking the onefile
    # bundle is preferable to a missing-dependency crash in the alpha.
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TuneHoard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Keep console=True for the alpha so users can copy/paste tracebacks.
    # Switch to False once we wire pywebview as the front door.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
