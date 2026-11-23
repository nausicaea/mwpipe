"""Runtime settings. Every path comes from a CLI flag or an environment variable. No path is hardcoded.

Priority for each value: command-line flag, then environment variable, then the default from
OPENMW_DATA_DIR / OPENMW_CONFIG_DIR. The code writes the paths without changes in openmw.cfg. Because of this,
the container must see the paths at the same location as the host (compose.yaml mounts them 1:1).

The OpenMW files and the mwpipe files are separate. OpenMW side (OPENMW_DATA_DIR): the base game,
the extracted mods (ModData/) and the generated plugins (Generated/). openmw.cfg points to these.
mwpipe side, in the usual locations for each user:

  cache   downloads/ (archives), md5-cache.json, plox/ (mlox rules), scratch/    you can delete it
  state   current collection, install manifest, openmw.cfg backups, builds/       if you delete it, you must install again
  config  overrides.json, lumenaire.toml                                          you edit it; mwpipe never writes it

Outside Docker, these are the directories for each user (XDG on Linux, ~/Library on macOS). In Docker the
image sets MWPIPE_{CACHE,STATE,CONFIG}_DIR to /cache, /state (named volumes) and /config (read-only
files from compose.yaml's `configs:`).
"""

from __future__ import annotations

import getpass
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from mwpipe.util import PipelineError

SECRET_FILE = Path("/run/secrets/nexus_api_key")
APP = "mwpipe"


def env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def default_dirs() -> tuple[Path, Path, Path]:
    """The (cache, state, config) directories: XDG on Linux, ~/Library on macOS (the same as platformdirs)."""
    home = Path.home()
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support" / APP
        return home / "Library" / "Caches" / APP, support, support

    def xdg(var: str, fallback: str) -> Path:
        value = os.environ.get(var, "")
        return (Path(value) if os.path.isabs(value) else home / fallback) / APP  # XDG: must be absolute
    return xdg("XDG_CACHE_HOME", ".cache"), xdg("XDG_STATE_HOME", ".local/state"), xdg("XDG_CONFIG_HOME", ".config")


@dataclass
class Settings:
    """The paths and options of one run. Use `from_args` to build it from the flags and the environment."""
    openmw_cfg: Path          # the openmw.cfg that OpenMW (and every tool) reads
    data_dir: Path            # root of everything mwpipe writes
    morrowind_data: Path      # vanilla "Data Files" (Morrowind.esm, Morrowind.bsa, ...)
    morrowind_ini: Path       # used to import fallback= values when openmw.cfg has none
    mods_dir: Path            # extracted mods, one directory per mod
    generated_dir: Path       # delta merge, groundcover, light fixes
    cache_dir: Path           # disposable: downloads, hashes, mlox rules, scratch space
    state_dir: Path           # collections, install manifest, cfg backups, builds
    config_dir: Path          # overrides.json, lumenaire.toml
    downloads_dir: Path       # archive cache (default: <cache>/downloads); drop manual downloads here
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
                raise PipelineError("Set OPENMW_CONFIG_DIR (or --openmw-cfg) to the location of openmw.cfg.")
            cfg = cfg_dir / "openmw.cfg"

        morrowind_data = (args.morrowind_data or env_path("MORROWIND_DATA_FILES")
                          or data_dir / "Morrowind" / "Data Files")
        cache, state, config = default_dirs()
        cache_dir = args.cache_dir or env_path("MWPIPE_CACHE_DIR") or cache
        config_dir = args.config_dir or env_path("MWPIPE_CONFIG_DIR") or config
        return cls(
            openmw_cfg=cfg,
            data_dir=data_dir,
            morrowind_data=morrowind_data,
            morrowind_ini=env_path("MORROWIND_INI") or morrowind_data.parent / "Morrowind.ini",
            mods_dir=args.mods_dir or env_path("MODS_DIR") or data_dir / "ModData",
            generated_dir=args.generated_dir or env_path("GENERATED_DIR") or data_dir / "Generated",
            cache_dir=cache_dir,
            state_dir=args.state_dir or env_path("MWPIPE_STATE_DIR") or state,
            config_dir=config_dir,
            downloads_dir=args.downloads_dir or env_path("DOWNLOADS_DIR") or cache_dir / "downloads",
            overrides_file=env_path("MWPIPE_OVERRIDES") or config_dir / "overrides.json",
            dry_run=args.dry_run,
        )

    def ensure_dirs(self) -> None:
        for path in (self.mods_dir, self.generated_dir, self.cache_dir, self.state_dir, self.downloads_dir):
            path.mkdir(parents=True, exist_ok=True)


def read_api_key(key_file: Path | None, from_stdin: bool) -> str:
    """Read the Nexus API key. The key does not appear on a command line.

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
        "No Nexus API key. Supply a key with a Docker secret, NEXUS_API_KEY_FILE, NEXUS_API_KEY "
        "or --api-key-stdin. Alternatively, run interactively (docker compose run -it)."
    )


def _check_key(key: str) -> str:
    if not key or any(c.isspace() for c in key):
        raise PipelineError("The Nexus API key is empty or has whitespace.")
    return key
