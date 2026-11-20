"""Turn a downloaded archive into one or more OpenMW data directories.

Strategy per mod, first match wins:
  1. overrides.json "data": explicit list of archive-relative folders
  2. FOMOD installer with the collection's recorded choices
  3. Vortex "replicate" mode: copy exactly the files listed in `hashes`
  4. Heuristic: the shallowest folders that look like a Data Files root
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from mwpipe.archives import extract
from mwpipe.collection import Mod
from mwpipe.fomod import FomodInstaller, find_module_config
from mwpipe.util import PipelineError, log, norm_rel, resolve_ci

PLUGIN_EXTS = {".esm", ".esp", ".omwaddon", ".omwgame", ".omwscripts"}
ARCHIVE_EXTS = {".bsa", ".ba2"}
ASSET_DIRS = {"meshes", "textures", "icons", "bookart", "sound", "music", "fonts", "splash",
              "video", "shaders", "l10n", "scripts", "grass", "animations", "distantland"}
IGNORED_DIRS = {"fomod", "__macosx", "docs", "documentation", "screenshots"}


def looks_like_data_root(path: Path) -> bool:
    if path.name.lower() == "data files":
        return True
    for child in path.iterdir():
        if child.is_file() and child.suffix.lower() in PLUGIN_EXTS | ARCHIVE_EXTS:
            return True
        if child.is_dir() and child.name.lower() in ASSET_DIRS:
            return True
    return False


def find_data_roots(tree: Path) -> list[Path]:
    """Breadth-first search; a matching folder is taken whole and not descended into."""
    roots, queue = [], [tree]
    while queue:
        level, queue = queue, []
        for folder in level:
            if looks_like_data_root(folder):
                roots.append(folder)
            else:
                queue += sorted(c for c in folder.iterdir()
                                if c.is_dir() and c.name.lower() not in IGNORED_DIRS)
        if roots:  # only keep the shallowest level that matched
            break
    return roots


def install_from_hashes(tree: Path, hashes: list[dict], dest: Path) -> int:
    """Replicate mode: find each listed file in the archive (by path suffix) and copy it."""
    index: dict[str, list[Path]] = {}
    for path in tree.rglob("*"):
        if path.is_file():
            index.setdefault(path.name.lower(), []).append(path)
    copied = 0
    for entry in hashes:
        want = norm_rel(entry["path"])
        candidates = [p for p in index.get(Path(want).name.lower(), [])
                      if norm_rel(str(p.relative_to(tree))).lower().endswith(want.lower())]
        if not candidates:
            log.warning("replicate: %s not found in archive", want)
            continue
        src = min(candidates, key=lambda p: len(p.parts))
        target = dest / want
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1
    return copied


def install_mod(mod: Mod, archive: Path, mod_dir: Path, scratch: Path) -> list[Path]:
    """Install `archive` into `mod_dir`; return its data directories in load order."""
    tree = extract(archive, scratch / "extract")
    if mod_dir.exists():
        shutil.rmtree(mod_dir)
    try:
        if explicit := mod.overrides.get("data"):
            roots = []
            for rel in explicit:
                found = resolve_ci(tree, rel)
                if found is None or not found.is_dir():
                    raise PipelineError(f"{mod.name}: override data folder {rel!r} not in archive")
                roots.append(found)
            return _move_roots(roots, mod_dir)

        if (config := find_module_config(tree)) and (mod.choices or {}).get("type", "fomod") == "fomod":
            log.info("%s: running FOMOD installer", mod.name)
            FomodInstaller(config, (mod.choices or {}).get("options")).install(mod_dir)
            return [mod_dir]

        if mod.hashes:
            count = install_from_hashes(tree, mod.hashes, mod_dir)
            log.info("%s: replicated %d/%d files", mod.name, count, len(mod.hashes))
            return [mod_dir]

        roots = find_data_roots(tree)
        if not roots:
            log.warning("%s: no Data Files layout detected, using archive root", mod.name)
            roots = [tree]
        if len(roots) > 1:
            log.warning("%s: %d candidate data folders, adding all: %s (pin with overrides 'data')",
                        mod.name, len(roots), ", ".join(str(r.relative_to(tree)) for r in roots))
        return _move_roots(roots, mod_dir)
    finally:
        shutil.rmtree(scratch / "extract", ignore_errors=True)


def _move_roots(roots: list[Path], mod_dir: Path) -> list[Path]:
    if len(roots) == 1:
        mod_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(roots[0], mod_dir)
        return [mod_dir]
    result = []
    for n, root in enumerate(roots):
        target = mod_dir / f"{n:02d} {root.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(root, target)
        result.append(target)
    return result


def lowercase_textures(root: Path) -> None:
    """Rename *.DDS -> *.dds (the old lowercase-dds-files.sh); helps tools that are case-sensitive."""
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".dds") and name != name.lower():
                os.replace(Path(dirpath, name), Path(dirpath, name.lower()))
