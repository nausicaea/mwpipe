"""Wrappers around the external tools baked into the image.

All tools read the same openmw.cfg: OPENMW_CONFIG is exported for every tool and the path is also
passed explicitly where a flag exists. Tool locations can be overridden with environment
variables (PLOX_BIN, DELTA_PLUGIN_BIN, ...) for use outside the container.
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
        raise PipelineError(f"{cmd[0]} is not installed (are you running inside the container?)")
    log.info("$ %s", " ".join(cmd))
    if dry_run:
        return
    env = dict(os.environ, OPENMW_CONFIG=str(cfg))
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        raise PipelineError(f"{Path(cmd[0]).name} failed with exit code {result.returncode}")


def _extra(var: str) -> list[str]:
    """Extra flags from an environment variable, e.g. LUMENAIRE_ARGS="--negative-lights preserve"."""
    return shlex.split(os.environ.get(var, ""))


PLOX_USER_RULES = "mlox_my_rules.txt"  # read by plox next to the downloaded mlox_base/mlox_user, never overwritten


def plox_sort(cfg: Path, work_dir: Path, dry_run: bool, rules: list[str] = ()) -> None:
    # plox downloads the mlox rules into ./mlox, so run it from a persistent (cache) directory.
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
    """Static grass -> OpenMW groundcover. Writes both plugins and a meshes/ folder to out_dir."""
    run([BIN["gocoverify"], "--openmw-config", str(cfg), "--output-dir", str(out_dir),
         *_extra("GOCOVERIFY_ARGS")], cfg, out_dir, dry_run)


def lumenaire(cfg: Path, out_dir: Path, settings: Path, enlightened_flames: bool, dry_run: bool) -> None:
    """Light fixes (Go port of waza_lightfixes). Writes Lumenaire.omwaddon (+ Meshes/ with -ef)."""
    cmd = [BIN["lumenaire"], "--config", str(settings), "--openmw-config", str(cfg),
           "--output-dir", str(out_dir)]
    if enlightened_flames:
        cmd.append("--enlightened-flames")
    run(cmd + _extra("LUMENAIRE_ARGS"), cfg, out_dir, dry_run)


def tes3cmd_clean(plugins: list[Path], all_plugins: list[Path], workspace: Path,
                  backup_dir: Path, dry_run: bool) -> list[Path]:
    """Clean plugins in place with `tes3cmd clean`; originals go to `backup_dir`.

    tes3cmd only finds masters inside a "Data Files" folder next to a Morrowind.ini, so a fake
    Morrowind install is assembled from symlinks in `workspace` (which also keeps its cache).
    Returns the plugins that were changed.
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
