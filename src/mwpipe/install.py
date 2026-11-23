"""Change a downloaded archive to one or more OpenMW data directories.

Strategy for each mod (the first match is used):
  1. overrides.json "data": explicit list of folders (paths relative to the archive)
  2. FOMOD installer with the recorded choices of the collection
  3. Vortex "replicate" mode: copy only the files in `hashes`
  4. Heuristic: the shallowest folders that are similar to a Data Files root
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
    """Return True if `path` looks like a Data Files root (a folder with that name, or a folder with plugin files, BSA files or asset folders)."""
    if path.name.lower() == "data files":
        return True
    for child in path.iterdir():
        if child.is_file() and child.suffix.lower() in PLUGIN_EXTS | ARCHIVE_EXTS:
            return True
        if child.is_dir() and child.name.lower() in ASSET_DIRS:
            return True
    return False


def find_data_roots(tree: Path) -> list[Path]:
    """Breadth-first search. Take a matching folder as a whole. Do not search inside it."""
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
            log.warning("replicate: %s is not in the archive", want)
            continue
        src = min(candidates, key=lambda p: len(p.parts))
        target = dest / want
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1
    return copied


def _fomod_hints(mod: Mod, archive_name: str, installer: FomodInstaller) -> list[str]:
    lines = [f"FOMOD options of {mod.name} ({archive_name}); [x] is installed now. To choose, add to the mod list: "
             f"+ {mod.list_url} | Step > Group > Option"]
    for step, group, kind, options in installer.report:
        lines += [f"  [{'x' if chosen else ' '}] {step} > {group} > {name}   ({kind})" for name, chosen in options]
    return lines


def _folder_hints(mod: Mod, archive_name: str, tree: Path, roots: list[Path], picked: list[Path]) -> list[str]:
    chosen = {r.resolve() for r in picked}
    lines = [f"{mod.name} ({archive_name}) has {len(roots)} data folders; [x] is installed now. To choose, add to the mod list: "
             f"+ {mod.list_url} | <folder>"]
    return lines + [f"  [{'x' if r.resolve() in chosen else ' '}] {r.relative_to(tree).as_posix()}" for r in roots]


def install_mod(mod: Mod, archive: Path, mod_dir: Path, scratch: Path, hints: list[str] | None = None) -> list[Path]:
    """Install `archive` into `mod_dir`. Return its data directories in load order.

    If you give `hints`, it receives lines that describe the choices of the archive (FOMOD options,
    alternative data folders). The lines also show which choices were installed.
    """
    tree = extract(archive, scratch / "extract")
    if mod_dir.exists():
        shutil.rmtree(mod_dir)
    try:
        if explicit := mod.overrides.get("data"):
            roots = []
            for rel in explicit:
                found = resolve_ci(tree, rel)
                if found is None or not found.is_dir():
                    raise PipelineError(f"{mod.name}: override data folder {rel!r} is not in the archive")
                roots.append(found)
            if hints is not None and len(candidates := find_data_roots(tree)) > 1:
                hints += _folder_hints(mod, archive.name, tree, candidates, roots)
            return _move_roots(roots, mod_dir)

        if (config := find_module_config(tree)) and (mod.choices or {}).get("type", "fomod") == "fomod":
            log.info("%s: running FOMOD installer", mod.name)
            installer = FomodInstaller(config, (mod.choices or {}).get("options"))
            installer.install(mod_dir)
            if hints is not None:
                hints += _fomod_hints(mod, archive.name, installer)
            return [mod_dir]

        if mod.hashes:
            count = install_from_hashes(tree, mod.hashes, mod_dir)
            log.info("%s: replicated %d/%d files", mod.name, count, len(mod.hashes))
            return [mod_dir]

        roots = find_data_roots(tree)
        if not roots:
            log.warning("%s: no Data Files layout found, using the archive root", mod.name)
            roots = [tree]
        if len(roots) > 1:
            log.warning("%s: %d candidate data folders, adding all of them: %s (to select one, use 'data' in the overrides)",
                        mod.name, len(roots), ", ".join(str(r.relative_to(tree)) for r in roots))
        if hints is not None and len(roots) > 1:
            hints += _folder_hints(mod, archive.name, tree, roots, roots)
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


def prune_to_plugins(roots: list[Path]) -> None:
    """Keep only the plugin files that are directly in each data folder. Delete everything else.

    `build-collection` needs the plugins (names, masters) to sort them. It does not need the textures, meshes or sounds.
    The folders stay, because the install manifest checks that they exist.
    """
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            elif child.suffix.lower() not in PLUGIN_EXTS:
                child.unlink()


def lowercase_textures(root: Path) -> None:
    """Rename *.DDS to *.dds (the old lowercase-dds-files.sh). This helps the tools that are case-sensitive.

    Only the extension changes. The rest of the name stays as the mod author wrote it. The code never
    overwrites a file. If another file has the lower-case name, the code keeps both files and writes a warning.
    """
    for dirpath, _, files in os.walk(root):
        for name in files:
            stem, ext = os.path.splitext(name)
            if ext.lower() == ".dds" and ext != ".dds":
                target = Path(dirpath, stem + ".dds")
                if target.exists() and not target.samefile(Path(dirpath, name)):
                    log.warning("not renaming %s: %s exists already", Path(dirpath, name), target.name)
                    continue
                os.replace(Path(dirpath, name), target)
