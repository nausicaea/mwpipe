"""Find textures that meshes refer to but no data folder or BSA provides.

OpenMW only logs "Failed to open image: Resource 'textures/x.dds' not found" when a mesh is first shown, and
says nothing about which mod wanted the texture. This scans the NIF meshes that win in the data order (later
data= entries override earlier ones) for texture names and reports every name that cannot be resolved, grouped
by the mod the mesh comes from.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from mwpipe.util import log

TEXTURE_EXTS = (".dds", ".tga", ".bmp", ".jpg", ".png")  # OpenMW falls back between these
# A run of printable ASCII ending in a texture extension. NIF strings are length-prefixed, and that length
# byte is itself printable when it is 0x20-0x7e, so `_clean` removes it again.
_REF = re.compile(rb"[\x20-\x7e]{3,260}?\.(?:dds|tga|bmp|jpg|png)(?![\x20-\x7e])", re.I)


@dataclass
class MissingTextures:
    # texture (lower case, "textures/" stripped, no extension) -> [(mod, mesh path inside the data folder)]
    missing: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    meshes: int = 0

    def by_mod(self) -> dict[str, list[str]]:
        mods: dict[str, set[str]] = {}
        for texture, users in self.missing.items():
            for mod, _ in users:
                mods.setdefault(mod, set()).add(texture)
        return {mod: sorted(names) for mod, names in sorted(mods.items())}


def _clean(raw: bytes) -> str:
    text = raw.decode("latin-1")
    if len(text) > 1 and ord(text[0]) == len(text) - 1:  # the string's own length prefix
        text = text[1:]
    text = text.replace("\\", "/").strip().lower()
    return text[len("textures/"):] if text.startswith("textures/") else text


def texture_refs(nif: bytes) -> set[str]:
    """Texture names (lower case, relative to textures/, without extension) mentioned in a NIF file."""
    refs = set()
    for match in _REF.finditer(nif):
        name = _clean(match.group())
        if name:
            refs.add(os.path.splitext(name)[0])
    return refs


def bsa_names(path: Path) -> list[str]:
    """File names inside a Morrowind (TES3) BSA; [] if the file is not one or cannot be read."""
    try:
        with path.open("rb") as f:
            head = f.read(12)
            if len(head) < 12:
                return []
            magic, hash_offset, count = struct.unpack("<III", head)
            if magic != 0x100:
                return []
            f.seek(12 + 8 * count + 4 * count)  # file records, then name offsets
            block = f.read(max(hash_offset - 12 * count, 0))
    except OSError:
        return []
    return [n.decode("latin-1") for n in block.split(b"\0") if n]


def _child(folder: Path, name: str) -> Path | None:
    exact = folder / name
    if exact.exists():
        return exact
    try:
        return next((c for c in folder.iterdir() if c.name.lower() == name), None)
    except OSError:
        return None


def _files(root: Path):
    for dirpath, _, names in os.walk(root):
        for name in names:
            yield Path(dirpath, name)


def scan_missing_textures(data_dirs: list[Path], archives: list[Path], mods_dir: Path) -> MissingTextures:
    """`data_dirs` in load order (later wins); `archives` are BSAs that count as texture sources."""
    available: set[str] = set()
    for archive in archives:
        for name in bsa_names(archive):
            rel = name.replace("\\", "/").lower()
            if rel.startswith("textures/"):
                available.add(os.path.splitext(rel[len("textures/"):])[0])

    meshes: dict[str, tuple[Path, Path]] = {}  # mesh path (lower case) -> (data dir, file); later dirs win
    for root in data_dirs:
        if not root.is_dir():
            continue
        if (tex := _child(root, "textures")) and tex.is_dir():
            for path in _files(tex):
                available.add(os.path.splitext(path.relative_to(tex).as_posix().lower())[0])
        if (mesh_dir := _child(root, "meshes")) and mesh_dir.is_dir():
            for path in _files(mesh_dir):
                if path.suffix.lower() == ".nif":
                    meshes[path.relative_to(mesh_dir).as_posix().lower()] = (root, path)

    result = MissingTextures(meshes=len(meshes))
    for rel, (root, path) in sorted(meshes.items()):
        try:
            refs = texture_refs(path.read_bytes())
        except OSError:
            continue
        for ref in sorted(refs - available):
            result.missing.setdefault(ref, []).append((_mod_name(root, mods_dir), rel))
    return result


def _mod_name(root: Path, mods_dir: Path) -> str:
    """`<collection>/<mod>[/<sub folder>]` below ModData -> the mod's folder name; else the folder's own name."""
    try:
        parts = root.relative_to(mods_dir).parts
    except ValueError:
        return root.name
    return parts[1] if len(parts) > 1 else (parts[0] if parts else root.name)


def report(result: MissingTextures, out_file: Path | None = None, examples: int = 3) -> None:
    if not result.missing:
        log.info("textures: every texture used by the %d scanned meshes was found", result.meshes)
        return
    by_mod = result.by_mod()
    log.warning("textures: %d textures used by meshes were not found in any data folder or BSA", len(result.missing))
    for mod, names in sorted(by_mod.items(), key=lambda kv: -len(kv[1])):
        shown = ", ".join(names[:examples]) + (", ..." if len(names) > examples else "")
        log.warning("  %s: %d missing, e.g. %s", mod, len(names), shown)
    if out_file is not None:
        lines = [f"{tex}\t{mod}\t{mesh}" for tex, users in sorted(result.missing.items()) for mod, mesh in users]
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("texture\tmod\tmesh\n" + "\n".join(lines) + "\n", encoding="utf-8")
        log.warning("  full list: %s", out_file)
