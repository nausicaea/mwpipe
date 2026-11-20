"""Archive extraction for untrusted mod archives (.7z, .zip, .rar, ...).

7-Zip is tried first, bsdtar (libarchive) second. Afterwards the tree is sanitised: symlinks are
removed, anything that escaped the destination is rejected, and permissions are normalised.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from mwpipe.util import PipelineError, log


def extract(archive: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    commands = [
        ["7z", "x", "-y", "-bso0", "-bsp0", f"-o{dest}", "--", str(archive)],
        ["bsdtar", "-x", "-f", str(archive), "-C", str(dest)],
    ]
    errors = []
    for cmd in commands:
        if shutil.which(cmd[0]) is None:
            continue
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            sanitise(dest)
            return dest
        errors.append(f"{cmd[0]}: {result.stderr.strip()[-300:]}")
        shutil.rmtree(dest)
        dest.mkdir()
    if zipfile.is_zipfile(archive):  # last resort: Python's own zip support
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if not (dest / member).resolve().is_relative_to(dest.resolve()):
                    raise PipelineError(f"{archive.name}: entry escapes extraction dir: {member}")
            zf.extractall(dest)
        sanitise(dest)
        return dest
    raise PipelineError(f"Could not extract {archive.name}:\n  " + "\n  ".join(errors or ["no extractor found"]))


def sanitise(root: Path) -> None:
    root_real = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            if path.is_symlink():
                log.warning("removing symlink from archive: %s", path.relative_to(root))
                path.unlink()
                continue
            if not path.resolve().is_relative_to(root_real):
                raise PipelineError(f"Archive entry escapes the extraction directory: {path}")
            path.chmod(0o755 if path.is_dir() else 0o644)
