"""Wrappers for the external tools in the image.

All tools read the same openmw.cfg. The code exports OPENMW_CONFIG for every tool. It also gives the path
to a tool if the tool has a flag for it. To set the location of a tool outside the container, use environment
variables (PLOX_BIN, DELTA_PLUGIN_BIN, ...).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from mwpipe.util import PipelineError, log

BIN = {
    "plox": os.environ.get("PLOX_BIN", "plox"),
    "delta": os.environ.get("DELTA_PLUGIN_BIN", "delta_plugin"),
    "gocoverify": os.environ.get("GOCOVERIFY_BIN", "gocoverify"),
    "lumenaire": os.environ.get("LUMENAIRE_BIN", "lumenaire"),
    "tes3cmd": os.environ.get("TES3CMD_BIN", "tes3cmd"),
}

DELTA_OUTPUT = "delta-merged.omwaddon"
GROUNDCOVER_DELETED = "deleted_groundcover.omwaddon"
GROUNDCOVER_PLUGIN = "groundcover.omwaddon"
LIGHTS_OUTPUT = "Lumenaire.omwaddon"
GENERATED_CONTENT = (DELTA_OUTPUT, GROUNDCOVER_DELETED, LIGHTS_OUTPUT)


def run(cmd: list[str], cfg: Path, cwd: Path, dry_run: bool = False) -> None:
    if shutil.which(cmd[0]) is None:
        raise PipelineError(f"{cmd[0]} is not installed (do you run mwpipe inside the container?)")
    log.info("$ %s", " ".join(cmd))
    if dry_run:
        return
    env = dict(os.environ, OPENMW_CONFIG=str(cfg))
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        raise PipelineError(f"{Path(cmd[0]).name} failed with exit code {result.returncode}")


def _extra(var: str) -> list[str]:
    """Extra flags from an environment variable, for example LUMENAIRE_ARGS="--negative-lights preserve"."""
    return shlex.split(os.environ.get(var, ""))


PLOX_USER_RULES = "mlox_my_rules.txt"  # read by plox next to the downloaded mlox_base/mlox_user, never overwritten


def plox_sort(cfg: Path, work_dir: Path, dry_run: bool, rules: list[str] = ()) -> None:
    # plox downloads the mlox rules into ./mlox. Run it from a persistent (cache) directory.
    if not dry_run:
        work_dir.mkdir(parents=True, exist_ok=True)
        rules_file = work_dir / "mlox" / PLOX_USER_RULES
        if rules:
            rules_file.parent.mkdir(parents=True, exist_ok=True)
            rules_file.write_text("\n\n".join(rules) + "\n", encoding="utf-8")
        else:
            rules_file.unlink(missing_ok=True)
    run([BIN["plox"], "--non-interactive", "--game", "openmw", "sort"], cfg, work_dir, dry_run)


def delta_merge(cfg: Path, out_dir: Path, skip: list[str], dry_run: bool) -> None:
    cmd = [BIN["delta"], "-c", str(cfg), "merge"]
    for record in skip:
        cmd += ["--skip", record]
    run(cmd + [str(out_dir / DELTA_OUTPUT)], cfg, out_dir, dry_run)


def gocoverify(cfg: Path, out_dir: Path, dry_run: bool) -> None:
    """Change static grass to OpenMW groundcover. Write both plugins and a meshes/ folder to out_dir."""
    run([BIN["gocoverify"], "--openmw-config", str(cfg), "--output-dir", str(out_dir),
         *_extra("GOCOVERIFY_ARGS")], cfg, out_dir, dry_run)


def lumenaire(cfg: Path, out_dir: Path, settings: Path, enlightened_flames: bool, dry_run: bool) -> None:
    """Light fixes (Go port of waza_lightfixes). Write Lumenaire.omwaddon (and Meshes/ with -ef)."""
    cmd = [BIN["lumenaire"], "--config", str(settings), "--openmw-config", str(cfg),
           "--output-dir", str(out_dir)]
    if enlightened_flames:
        cmd.append("--enlightened-flames")
    run(cmd + _extra("LUMENAIRE_ARGS"), cfg, out_dir, dry_run)


def tes3cmd_clean(plugins: list[Path], all_plugins: list[Path], workspace: Path,
                  backup_dir: Path, dry_run: bool) -> list[Path]:
    """Clean the plugins in place with `tes3cmd clean`. The originals go to `backup_dir`.

    tes3cmd finds masters only in a "Data Files" folder next to a Morrowind.ini. Because of this, the code
    makes a fake Morrowind install from symlinks in `workspace` (which also keeps its cache).
    Return the plugins that changed.
    """
    data_files = workspace / "Data Files"
    shutil.rmtree(data_files, ignore_errors=True)
    data_files.mkdir(parents=True)
    (workspace / "Morrowind.ini").write_text("[Game Files]\n", encoding="cp1252")
    for plugin in all_plugins:
        (data_files / plugin.name).symlink_to(plugin)
    changed = []
    for plugin in plugins:
        work = data_files / plugin.name
        work.unlink()
        shutil.copy2(plugin, work)
        run([BIN["tes3cmd"], "clean", "--overwrite", plugin.name], workspace, data_files, dry_run)
        if not dry_run and work.read_bytes() != plugin.read_bytes():
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plugin, backup_dir / plugin.name)
            shutil.copy2(work, plugin)
            changed.append(plugin)
        work.unlink()
        work.symlink_to(plugin)
    shutil.rmtree(data_files)
    return changed
