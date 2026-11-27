"""Read, rewrite and write openmw.cfg.

mwpipe manages four keys: data=, fallback-archive=, content= and groundcover=. The code keeps every other line
(encoding, fallback=, comments, ...) in its position. It writes the managed keys as one
block at the end, in OpenMW order: archives, data paths, plugins, groundcover.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from mwpipe.util import PipelineError, log

MANAGED = ("fallback-archive", "data", "content", "groundcover")
# Characters that end a line for str.splitlines(), and NUL. A file name with one of them can add lines to openmw.cfg.
LINE_BREAKS = frozenset("\0\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
USERDATA = "?userdata?"  # OpenMW path token for the user data directory
MARKER = "# ---- managed by mwpipe: edits to data/content/groundcover/fallback-archive below are overwritten ----"


def quote(path: Path | str) -> str:
    """OpenMW path quoting: put the path in double quotes. Put '&' before each '&' and each '"'."""
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
    """The content of openmw.cfg: the lines that mwpipe keeps and the four keys that it manages."""
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

    def is_global(self) -> bool:
        """True if this is the global openmw.cfg that OpenMW installs. The user must not change that file."""
        return any(line.lstrip("# ").startswith("This is the global openmw.cfg file") for line in self.other)

    # -- path tokens -------------------------------------------------------------------------
    def resolve_userdata(self, default: Path) -> int:
        """Replace the OpenMW token ?userdata? in data=, data-local= and config= with an absolute path.

        Some tools (GOCoverify 1.1) stop at this token. The user-data= value of this file has priority,
        then `default` (OPENMW_DATA_DIR, which is the OpenMW user data directory). Return the number of
        values that changed.
        """
        userdata = default
        for line in self.other:
            key, sep, value = line.partition("=")
            if sep and key.strip() == "user-data" and Path(unquote(value)).is_absolute():
                userdata = Path(unquote(value))

        def resolve(value: str) -> str | None:
            if not value.startswith(USERDATA):
                return None
            return str(userdata / value[len(USERDATA):].lstrip("/\\"))

        changed = 0
        for i, path in enumerate(self.data):
            if (new := resolve(str(path))) is not None:
                self.data[i], changed = Path(new), changed + 1
        for i, line in enumerate(self.other):
            key, sep, value = line.partition("=")
            if sep and key.strip() in ("data-local", "config") and (new := resolve(unquote(value))) is not None:
                self.other[i], changed = f"{key.strip()}={quote(new)}", changed + 1
        return changed

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
        """Return the text of openmw.cfg. Raise PipelineError if a managed value has a line break."""
        lines = list(self.other) + ["", MARKER]
        for key, values in (("fallback-archive", self.archives), ("data", self.data),
                            ("content", self.content), ("groundcover", self.groundcover)):
            for value in dedupe(values):
                if LINE_BREAKS.intersection(str(value)):
                    raise PipelineError(f"openmw.cfg: the {key}= value {str(value)!r} has a line break. "
                                        "Rename the file or remove the mod.")
                lines.append(f"{key}={quote(value) if key == 'data' else value}")
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
    """Remove duplicates that differ only in case. Keep the first occurrence."""
    seen, out = set(), []
    for item in items:
        k = str(item).lower()
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


def import_ini_fallbacks(ini: Path) -> list[tuple[str, str]]:
    """Change the Morrowind.ini settings to fallback= pairs. The result is the same as openmw-iniimporter."""
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
