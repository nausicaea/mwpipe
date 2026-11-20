"""Runtime settings. Every path comes from a CLI flag or an environment variable; nothing is hardcoded.

Precedence for each value: command-line flag > environment variable > default derived from
OPENMW_DATA_DIR / OPENMW_CONFIG_DIR. Paths are used verbatim inside openmw.cfg, so the container
must see them at the same location as the host (compose.yaml mounts them 1:1).
"""

from __future__ import annotations

import getpass
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from mwpipe.util import PipelineError

SECRET_FILE = Path("/run/secrets/nexus_api_key")


def env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


@dataclass
class Settings:
    openmw_cfg: Path          # the openmw.cfg that OpenMW (and every tool) reads
    data_dir: Path            # root of everything mwpipe writes
    morrowind_data: Path      # vanilla "Data Files" (Morrowind.esm, Morrowind.bsa, ...)
    morrowind_ini: Path       # used to import fallback= values when openmw.cfg has none
    mods_dir: Path            # extracted mods, one directory per mod
    downloads_dir: Path       # archive cache; drop manually downloaded files here
    generated_dir: Path       # delta merge, groundcover, light fixes
    state_dir: Path           # collection.json, install manifest, cfg backups, mlox rules
    overrides_file: Path      # optional per-mod tweaks, see README
    dry_run: bool = False

    @classmethod
    def from_args(cls, args) -> "Settings":
        data_dir = args.data_dir or env_path("OPENMW_DATA_DIR")
        if data_dir is None:
            raise PipelineError("Set OPENMW_DATA_DIR (or --data-dir) to your OpenMW data directory.")

        cfg = args.openmw_cfg or env_path("OPENMW_CONFIG")
        if cfg is None:
            cfg_dir = env_path("OPENMW_CONFIG_DIR")
            if cfg_dir is None:
                raise PipelineError("Set OPENMW_CONFIG_DIR (or --openmw-cfg) to locate openmw.cfg.")
            cfg = cfg_dir / "openmw.cfg"

        morrowind_data = (args.morrowind_data or env_path("MORROWIND_DATA_FILES")
                          or data_dir / "Morrowind" / "Data Files")
        return cls(
            openmw_cfg=cfg,
            data_dir=data_dir,
            morrowind_data=morrowind_data,
            morrowind_ini=env_path("MORROWIND_INI") or morrowind_data.parent / "Morrowind.ini",
            mods_dir=args.mods_dir or env_path("MODS_DIR") or data_dir / "ModData",
            downloads_dir=args.downloads_dir or env_path("DOWNLOADS_DIR") or data_dir / "Downloads",
            generated_dir=args.generated_dir or env_path("GENERATED_DIR") or data_dir / "Generated",
            state_dir=env_path("MWPIPE_STATE_DIR") or data_dir / ".mwpipe",
            overrides_file=env_path("MWPIPE_OVERRIDES") or data_dir / "mwpipe-overrides.json",
            dry_run=args.dry_run,
        )

    def ensure_dirs(self) -> None:
        for path in (self.mods_dir, self.downloads_dir, self.generated_dir, self.state_dir):
            path.mkdir(parents=True, exist_ok=True)


def read_api_key(key_file: Path | None, from_stdin: bool) -> str:
    """Read the Nexus API key without it ever appearing on a command line.

    Order: --api-key-file / NEXUS_API_KEY_FILE, Docker secret, NEXUS_API_KEY, stdin, prompt.
    """
    candidates = [key_file, env_path("NEXUS_API_KEY_FILE"), SECRET_FILE]
    for path in candidates:
        if path and path.is_file() and (key := path.read_text(encoding="utf-8").strip()):
            return _check_key(key)
    if os.environ.get("NEXUS_API_KEY"):
        return _check_key(os.environ["NEXUS_API_KEY"].strip())
    if from_stdin:
        return _check_key(sys.stdin.readline().strip())
    if sys.stdin.isatty():
        return _check_key(getpass.getpass("Nexus Mods API key: ").strip())
    raise PipelineError(
        "No Nexus API key. Provide one via a Docker secret, NEXUS_API_KEY_FILE, NEXUS_API_KEY, "
        "--api-key-stdin, or run interactively (docker compose run -it)."
    )


def _check_key(key: str) -> str:
    if not key or any(c.isspace() for c in key):
        raise PipelineError("The Nexus API key is empty or contains whitespace.")
    return key
