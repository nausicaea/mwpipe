"""Archive extraction for untrusted mod archives (.7z, .zip, .rar, ...).

The code tries 7-Zip first and bsdtar (libarchive) second. Then it makes the tree safe:
it removes symlinks, rejects each entry that leaves the destination, and sets the permissions.

An archive can unpack to much more than its own size (a "zip bomb"). The extracted size of one archive
has a limit (default 10 GiB, see `--extract-limit`). Two controls enforce it while the extractor runs:
RLIMIT_FSIZE stops a single file that is too large at once, and a watcher stops the extractor when the
total of all files is too large.
"""

from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import tempfile
import zipfile
from pathlib import Path

from mwpipe.util import PipelineError, log

GIB = 1 << 30
DEFAULT_LIMIT = 10 * GIB   # the largest Morrowind texture packs unpack to a few GiB
_POLL_SECONDS = 0.5

_SEVEN_ZIP_OK: dict[str, bool] = {}


def tree_size(root: Path) -> int:
    """The total size in bytes of the files below `root`. Symlinks count as their own size."""
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:  # the extractor can rename a temporary file while we look
                pass
    return total


def _too_large(archive: Path, limit: int) -> PipelineError:
    return PipelineError(f"{archive.name}: the extracted files are larger than the limit of {limit / GIB:g} GiB. "
                         "If the archive is correct, increase the limit with --extract-limit or MWPIPE_EXTRACT_LIMIT.")


def _run_limited(cmd: list[str], dest: Path, limit: int) -> tuple[int | None, str]:
    """Run an extractor. Return (exit code, stderr), or (None, "") if the files in `dest` became larger than `limit`."""
    def cap_file_size() -> None:  # runs in the child before exec
        _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        cap = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, hard))
        signal.signal(signal.SIGXFSZ, signal.SIG_DFL)  # stop the extractor, do not only fail the write

    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:  # a pipe can fill and block
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                preexec_fn=cap_file_size)
        try:
            while True:
                try:
                    code = proc.wait(timeout=_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if tree_size(dest) > limit:
                        return None, ""
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        # A file that RLIMIT_FSIZE cut off has exactly `limit` bytes, and its extractor can still exit with 0.
        if code == -signal.SIGXFSZ or tree_size(dest) >= limit:
            return None, ""
        err.seek(0)
        return code, err.read()


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


def extract(archive: Path, dest: Path, limit: int = DEFAULT_LIMIT) -> Path:
    """Extract `archive` into a new `dest` directory and return `dest`.

    Try 7-Zip, then bsdtar, then Python zip support. Make the tree safe after each success.
    Raise PipelineError if no extractor can read the archive, an entry leaves `dest`, or the
    extracted files are larger than `limit` bytes. The code removes `dest` after an error about the size."""
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
        code, stderr = _run_limited(cmd, dest, limit)
        if code is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise _too_large(archive, limit)  # another extractor would make the same files: do not try it
        if code == 0:
            sanitise(dest)
            return dest
        errors.append(f"{cmd[0]}: {stderr.strip()[-300:]}")
        shutil.rmtree(dest)
        dest.mkdir()
    if zipfile.is_zipfile(archive):  # last resort: Python's own zip support
        with zipfile.ZipFile(archive) as zf:
            # zipfile never writes more than the size in the header of an entry, so the headers are enough.
            if sum(info.file_size for info in zf.infolist()) > limit:
                raise _too_large(archive, limit)
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
