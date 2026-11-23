"""Archive extraction for untrusted mod archives (.7z, .zip, .rar, ...).

The code tries 7-Zip first and bsdtar (libarchive) second. Then it makes the tree safe:
it removes symlinks, rejects each entry that leaves the destination, and sets the permissions.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from mwpipe.util import PipelineError, log

_SEVEN_ZIP_OK: dict[str, bool] = {}


def _seven_zip_is_safe(binary: str) -> bool:
    """Return False for p7zip. p7zip is not maintained, and it can write through a symlink from the archive.

    The Docker image has 7-Zip from Alpine, which is safe. p7zip is possible when mwpipe runs outside Docker."""
    if binary not in _SEVEN_ZIP_OK:
        try:
            banner = subprocess.run([binary], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                    timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            banner = "p7zip"  # cannot identify it: do not use it
        _SEVEN_ZIP_OK[binary] = "p7zip" not in banner.lower()
        if not _SEVEN_ZIP_OK[binary]:
            log.warning("not using %s: it is p7zip, which is not safe for untrusted archives. "
                        "mwpipe uses bsdtar. Install 7-Zip 21 or later, or run mwpipe in Docker.", binary)
    return _SEVEN_ZIP_OK[binary]


def extract(archive: Path, dest: Path) -> Path:
    """Extract `archive` into a new `dest` directory and return `dest`.

    Try 7-Zip, then bsdtar, then Python zip support. Make the tree safe after each success.
    Raise PipelineError if no extractor can read the archive or an entry leaves `dest`."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    commands = [
        ["7z", "x", "-y", "-bso0", "-bsp0", f"-o{dest}", "--", str(archive)],
        ["bsdtar", "-x", "-f", str(archive), "-C", str(dest)],
    ]
    errors = []
    for cmd in commands:
        if shutil.which(cmd[0]) is None or (cmd[0] == "7z" and not _seven_zip_is_safe(cmd[0])):
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
                    raise PipelineError(f"{archive.name}: entry leaves the extraction directory: {member}")
            zf.extractall(dest)
        sanitise(dest)
        return dest
    raise PipelineError(f"Cannot extract {archive.name}:\n  " + "\n  ".join(errors or ["no extractor available"]))


def sanitise(root: Path) -> None:
    """Make an extracted tree safe.

    Remove each symlink. Set the permissions of files to 0644 and of directories to 0755.
    Raise PipelineError if an entry resolves to a path outside `root`."""
    root_real = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            if path.is_symlink():
                log.warning("removing symlink from the archive: %s", path.relative_to(root))
                path.unlink()
                continue
            if not path.resolve().is_relative_to(root_real):
                raise PipelineError(f"Archive entry leaves the extraction directory: {path}")
            path.chmod(0o755 if path.is_dir() else 0o644)
