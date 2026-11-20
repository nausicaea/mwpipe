"""The collection manifest (collection.json, as written by Vortex) and mod ordering.

Relevant collection.json fields:
  mods[]       name, version, optional, source{type, modId, fileId, md5, logicalFilename,
               fileExpression, url, instructions}, choices (FOMOD), hashes (replicate mode), phase
  modRules[]   {source, type: before|after|..., reference}: decides which mod's files win
  loadOrder[]  Morrowind plugin order exported by Vortex: [{id, name, enabled}]

The Nexus v3 OpenAPI spec documents the upload-side manifest with snake_case source keys
(mod_id, file_id, logical_filename, ...) where Vortex writes camelCase, so both are accepted.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from pathlib import Path

from mwpipe.util import load_json, log, safe_name


def normalize_source(source: dict) -> dict:
    """Return `source` with snake_case keys (file_id) converted to camelCase (fileId)."""
    def camel(key: str) -> str:
        head, *rest = key.split("_")
        return head + "".join(part.capitalize() for part in rest)
    return {camel(key): value for key, value in source.items()}


@dataclass
class Mod:
    index: int                 # position in collection.json, used as a stable tie-breaker
    name: str
    source: dict
    optional: bool = False
    phase: int = 0
    choices: dict | None = None
    hashes: list | None = None
    patches: dict | None = None
    instructions: str = ""
    overrides: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        s = self.source
        if s.get("type") == "nexus":
            return f"{s.get('modId')}-{s.get('fileId')}"
        return safe_name(s.get("logicalFilename") or self.name, 60)

    @property
    def dir_name(self) -> str:
        return f"{self.index:03d} {safe_name(self.name)}"

    def matches(self, ref: dict) -> bool:
        """Does a Vortex IModReference point at this mod?"""
        s = self.source
        repo = ref.get("repo") or {}
        if repo.get("fileId") and str(repo["fileId"]) == str(s.get("fileId")):
            return True
        if ref.get("fileMD5") and ref["fileMD5"] == s.get("md5"):
            return True
        for key, own in (("logicalFileName", s.get("logicalFilename")),
                         ("fileExpression", s.get("fileExpression"))):
            if ref.get(key) and own and ref[key].lower() == own.lower():
                return True
        return False


@dataclass
class Collection:
    name: str
    mods: list[Mod]
    rules: list[dict]
    load_order: list[dict]

    @classmethod
    def load(cls, path: Path, overrides: dict, include_optional: bool) -> "Collection":
        raw = load_json(path, None)
        if raw is None:
            raise FileNotFoundError(path)
        mods = []
        for i, m in enumerate(raw.get("mods", [])):
            mod = Mod(index=i, name=m.get("name", f"mod{i}"), source=normalize_source(m.get("source") or {}),
                      optional=bool(m.get("optional")), phase=int(m.get("phase") or 0),
                      choices=m.get("choices"), hashes=m.get("hashes"), patches=m.get("patches"),
                      instructions=m.get("instructions") or "")
            mod.overrides = overrides.get("mods", {}).get(mod.key) or overrides.get("mods", {}).get(mod.name, {})
            if mod.overrides.get("skip") or (mod.optional and not include_optional
                                               and not mod.overrides.get("include")):
                log.info("skipping %s mod: %s", "optional" if mod.optional else "excluded", mod.name)
                continue
            mods.append(mod)
        return cls(name=raw.get("info", {}).get("name", path.stem), mods=mods,
                   rules=raw.get("modRules", []), load_order=raw.get("loadOrder", []))

    def data_order(self) -> list[Mod]:
        """Order mods so that later entries override earlier ones (OpenMW data= semantics).

        Starts from (phase, collection index) and applies before/after rules with a stable
        topological sort. Cycles are reported and broken by falling back to the base order.
        """
        after: dict[int, set[int]] = {m.index: set() for m in self.mods}  # node -> must come after these
        by_ref = lambda ref: [m.index for m in self.mods if m.matches(ref)]  # noqa: E731
        for rule in self.rules:
            if rule.get("type") not in ("before", "after"):
                continue
            for src in by_ref(rule.get("source", {})):
                for dst in by_ref(rule.get("reference", {})):
                    if src == dst:
                        continue
                    if rule["type"] == "after":
                        after[src].add(dst)
                    else:
                        after[dst].add(src)
        mods = {m.index: m for m in self.mods}
        rank = {m.index: (m.phase, m.index) for m in self.mods}
        pending = {i: len(deps) for i, deps in after.items()}
        dependants: dict[int, list[int]] = {i: [] for i in after}
        for i, deps in after.items():
            for d in deps:
                dependants[d].append(i)
        ready = [rank[i] + (i,) for i, n in pending.items() if n == 0]
        heapq.heapify(ready)
        ordered: list[int] = []
        while ready:
            *_, i = heapq.heappop(ready)
            ordered.append(i)
            for j in dependants[i]:
                pending[j] -= 1
                if pending[j] == 0:
                    heapq.heappush(ready, rank[j] + (j,))
        if len(ordered) < len(mods):
            stuck = sorted(set(mods) - set(ordered), key=rank.get)
            log.warning("cyclic mod rules between: %s", ", ".join(mods[i].name for i in stuck))
            ordered += stuck
        return [mods[i] for i in ordered]

    def plugin_order(self) -> list[str] | None:
        """Enabled plugins in the author's order, or None if the collection has no load order."""
        if not self.load_order:
            return None
        return [e.get("name") or e.get("id") for e in self.load_order if e.get("enabled", True)]
