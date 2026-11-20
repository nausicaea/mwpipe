"""Small helpers shared by all modules: logging, hashing, case-insensitive paths, JSON state."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("mwpipe")


class PipelineError(RuntimeError):
    """An error the user can act on. Printed without a traceback."""


def md5sum(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(text: str, limit: int = 80) -> str:
    """Turn a mod name into a portable directory name."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._+-]+", "_", text).strip(" ._")
    return (cleaned or "mod")[:limit]


def norm_rel(path: str) -> str:
    """Normalise an archive-relative path: forward slashes, no leading './' or '/'."""
    return path.replace("\\", "/").strip("/").removeprefix("./")


def resolve_ci(base: Path, rel: str) -> Path | None:
    """Find `rel` below `base`, ignoring case (mod archives are authored on Windows)."""
    current = base
    for part in [p for p in norm_rel(rel).split("/") if p and p != "."]:
        exact = current / part
        if exact.exists():
            current = exact
            continue
        if not current.is_dir():
            return None
        match = next((c for c in current.iterdir() if c.name.lower() == part.lower()), None)
        if match is None:
            return None
        current = match
    return current


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
