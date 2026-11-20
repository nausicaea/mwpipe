"""Read, rewrite and write openmw.cfg.

mwpipe owns four keys: data=, fallback-archive=, content= and groundcover=. Every other line
(encoding, fallback=, comments, ...) is preserved in place. The managed keys are written as one
block at the end, in OpenMW order: archives, data paths, plugins, groundcover.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from mwpipe.util import log

MANAGED = ("fallback-archive", "data", "content", "groundcover")
MARKER = "# ---- managed by mwpipe: edits to data/content/groundcover/fallback-archive below are overwritten ----"


def quote(path: Path | str) -> str:
    """OpenMW path quoting: wrap in double quotes, escape '&' and '"' with '&'."""
    return '"' + str(path).replace("&", "&&").replace('"', '&"') + '"'


def unquote(value: str) -> str:
    value = value.strip()
    if not value.startswith('"'):
        return value
    out, i = [], 1
    while i < len(value):
        c = value[i]
        if c == "&" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
            continue
        if c == '"':
            break
        out.append(c)
        i += 1
    return "".join(out)


@dataclass
class OpenMWConfig:
    other: list[str] = field(default_factory=list)  # preserved lines, in order
    archives: list[str] = field(default_factory=list)
    data: list[Path] = field(default_factory=list)
    content: list[str] = field(default_factory=list)
    groundcover: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "OpenMWConfig":
        cfg = cls()
        if not path.exists():
            cfg.other = ["encoding=win1252"]
            return cfg
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            key = key.strip()
            if line.strip() == MARKER:
                continue
            if sep and key == "data":
                cfg.data.append(Path(unquote(value)))
            elif sep and key == "fallback-archive":
                cfg.archives.append(value.strip())
            elif sep and key == "content":
                cfg.content.append(value.strip())
            elif sep and key == "groundcover":
                cfg.groundcover.append(value.strip())
            else:
                cfg.other.append(line)
        while cfg.other and not cfg.other[-1].strip():
            cfg.other.pop()
        return cfg

    # -- fallback= values --------------------------------------------------------------------
    @property
    def fallbacks(self) -> dict[str, str]:
        out = {}
        for line in self.other:
            if line.startswith("fallback="):
                name, _, value = line[len("fallback="):].partition(",")
                out[name] = value
        return out

    def set_fallback(self, name: str, value: str) -> None:
        line = f"fallback={name},{value}"
        for i, existing in enumerate(self.other):
            if existing.startswith(f"fallback={name},"):
                self.other[i] = line
                return
        self.other.append(line)

    # -- output ------------------------------------------------------------------------------
    def render(self) -> str:
        lines = list(self.other) + ["", MARKER]
        lines += [f"fallback-archive={a}" for a in dedupe(self.archives)]
        lines += [f"data={quote(d)}" for d in dedupe(self.data)]
        lines += [f"content={c}" for c in dedupe(self.content)]
        lines += [f"groundcover={g}" for g in dedupe(self.groundcover)]
        return "\n".join(lines) + "\n"

    def save(self, path: Path, backup_dir: Path | None = None) -> None:
        if backup_dir and path.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_dir / f"openmw.cfg.{time.strftime('%Y%m%d-%H%M%S')}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".cfg.tmp")
        tmp.write_text(self.render(), encoding="utf-8")
        tmp.replace(path)


def dedupe(items: list) -> list:
    """Drop case-insensitive duplicates, keeping the first occurrence."""
    seen, out = set(), []
    for item in items:
        k = str(item).lower()
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


def import_ini_fallbacks(ini: Path) -> list[tuple[str, str]]:
    """Convert Morrowind.ini settings to fallback= pairs, exactly like openmw-iniimporter."""
    keys_file = Path(__file__).parent / "data" / "fallback_keys.txt"
    wanted = [k for k in keys_file.read_text(encoding="utf-8").splitlines() if k and not k.startswith("#")]
    values: dict[str, list[str]] = {}
    section = ""
    for raw in ini.read_text(encoding="cp1252", errors="replace").splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        if line.startswith("["):
            if line.find("]") >= 2:
                section = line[1:line.find("]")]
            continue
        line = line.split(";", 1)[0]
        key, sep, value = line.partition("=")
        if sep and key and value:
            values.setdefault(f"{section}:{key}", []).append(value)
    pairs = []
    for key in wanted:
        for value in values.get(key, []):
            pairs.append((key.replace(" ", "_").replace(":", "_"), value))
    log.info("imported %d fallback values from %s", len(pairs), ini)
    return pairs
