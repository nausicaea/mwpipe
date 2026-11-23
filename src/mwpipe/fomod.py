"""A small FOMOD (fomod/ModuleConfig.xml) interpreter. It applies the choices of the collection author again.

Vortex records the choices in this format: [{name: step, groups: [{name: group, choices: [{name, idx}]}]}].
A group with no recorded choices uses the installer defaults (Required/Recommended).
Supported: requiredInstallFiles, installSteps with visibility, plugin types, condition flags and
conditionalFileInstalls. Not supported (rare for Morrowind): script-based installers.
"""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from mwpipe.archives import DEFAULT_LIMIT, GIB, tree_size
from mwpipe.util import PipelineError, is_inside, log, norm_rel, resolve_ci, safe_join


@dataclass
class _Install:
    source: str
    destination: str
    priority: int
    is_folder: bool


def find_module_config(root: Path) -> Path | None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts)):
        if path.name.lower() == "moduleconfig.xml" and path.parent.name.lower() == "fomod":
            return path
    return None


def _strip_ns(tree: ET.Element) -> ET.Element:
    for el in tree.iter():
        el.tag = el.tag.split("}", 1)[-1]
    return tree


class FomodInstaller:
    """Read a FOMOD ModuleConfig.xml and find the files that the recorded choices install.

    Use `plan` to get the list of files and `install` to copy them."""
    def __init__(self, module_config: Path, choices: list | None, limit: int = DEFAULT_LIMIT):
        self.base = module_config.parent.parent  # paths in the XML are relative to this folder
        self.root = _strip_ns(ET.fromstring(module_config.read_bytes()))
        self.choices = choices or []
        self.limit = limit  # bytes that `install` copies in total; one folder can be installed many times
        self.flags: dict[str, str] = {}
        self.planned: list[_Install] = []
        self.report: list[tuple[str, str, str, list[tuple[str, bool]]]] = []  # (step, group, type, [(option, chosen)])
        self._used: set[tuple[str, str]] = set()   # (step, group) pairs that recorded choices applied to

    # -- conditions ------------------------------------------------------------------------------
    def _deps_met(self, deps: ET.Element | None) -> bool:
        if deps is None:
            return True
        results = []
        for child in deps:
            if child.tag == "flagDependency":
                results.append(self.flags.get(child.get("flag", ""), "") == child.get("value", ""))
            elif child.tag == "fileDependency":
                planned = {norm_rel(i.destination).lower() for i in self.planned}
                present = norm_rel(child.get("file", "")).lower() in planned
                results.append(present == (child.get("state", "Active") == "Active"))
            elif child.tag == "dependencies":
                results.append(self._deps_met(child))
            else:  # gameDependency, fommDependency, ... always satisfied here
                results.append(True)
        return all(results) if deps.get("operator", "And") == "And" else any(results)

    def _plugin_type(self, plugin: ET.Element) -> str:
        desc = plugin.find("typeDescriptor")
        if desc is None:
            return "Optional"
        if (plain := desc.find("type")) is not None:
            return plain.get("name", "Optional")
        dep = desc.find("dependencyType")
        for pattern in dep.findall("patterns/pattern") if dep is not None else []:
            if self._deps_met(pattern.find("dependencies")):
                return pattern.find("type").get("name", "Optional")
        default = dep.find("defaultType") if dep is not None else None
        return default.get("name", "Optional") if default is not None else "Optional"

    # -- selection -----------------------------------------------------------------------------
    def _recorded(self, step: str, group: str) -> list[dict] | None:
        for s in self.choices:
            if s.get("name") == step:
                for g in s.get("groups", []):
                    if g.get("name") == group:
                        return g.get("choices", [])
        return None

    def _select(self, step: str, group: ET.Element) -> list[ET.Element]:
        plugins = group.findall("plugins/plugin")
        recorded = self._recorded(step, group.get("name", ""))
        if recorded is not None:
            self._used.add((step, group.get("name", "")))
            chosen = []
            for choice in recorded:
                by_name = [p for p in plugins if p.get("name") == choice.get("name")]
                idx = choice.get("idx")
                if by_name:
                    chosen.append(by_name[0])
                elif isinstance(idx, int) and 0 <= idx < len(plugins):
                    chosen.append(plugins[idx])
                else:
                    log.warning("FOMOD: option %r not found in %s > %s", choice.get("name"), step, group.get("name"))
            return chosen
        kind = group.get("type", "SelectAny")
        if kind == "SelectAll":
            return plugins
        defaults = [p for p in plugins if self._plugin_type(p) in ("Required", "Recommended")]
        if kind in ("SelectExactlyOne", "SelectAtLeastOne") and not defaults:
            usable = [p for p in plugins if self._plugin_type(p) != "NotUsable"]
            defaults = usable[:1]
        if kind in ("SelectExactlyOne", "SelectAtMostOne"):
            defaults = defaults[:1]
        return defaults

    def _queue_files(self, files: ET.Element | None) -> None:
        for item in list(files) if files is not None else []:
            if item.tag not in ("file", "folder"):
                continue
            source = item.get("source", "")
            dest = item.get("destination")
            self.planned.append(_Install(
                source=source,
                destination=source if dest is None and item.tag == "file" else (dest or ""),
                priority=int(item.get("priority", "0") or 0),
                is_folder=item.tag == "folder",
            ))

    # -- run -------------------------------------------------------------------------------------
    def plan(self) -> list[_Install]:
        self._queue_files(self.root.find("requiredInstallFiles"))
        for step in self.root.findall("installSteps/installStep"):
            if not self._deps_met(step.find("visible")):
                continue
            for group in step.findall("optionalFileGroups/group"):
                selected = self._select(step.get("name", ""), group)
                self.report.append((step.get("name", ""), group.get("name", ""), group.get("type", "SelectAny"),
                                    [(p.get("name", ""), p in selected) for p in group.findall("plugins/plugin")]))
                for plugin in selected:
                    self._queue_files(plugin.find("files"))
                    for flag in plugin.findall("conditionFlags/flag"):
                        self.flags[flag.get("name", "")] = (flag.text or "").strip()
        for pattern in self.root.findall("conditionalFileInstalls/patterns/pattern"):
            if self._deps_met(pattern.find("dependencies")):
                self._queue_files(pattern.find("files"))
        for s in self.choices:
            for g in s.get("groups", []):
                if (s.get("name"), g.get("name")) not in self._used:
                    log.warning("FOMOD: no step %r with group %r (hidden or misspelled); mwpipe ignores the choices",
                                s.get("name"), g.get("name"))
        return self.planned

    def install(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        copied = 0
        # Lower priority first, so that files with higher priority overwrite them. The sort is stable for equal priority.
        for item in sorted(self.plan(), key=lambda i: i.priority):
            src = resolve_ci(self.base, item.source) if item.source else self.base
            if src is None:
                log.warning("FOMOD: missing source %r", item.source)
                continue
            if not is_inside(self.base, src):  # a malicious ModuleConfig.xml can name '../..' or a symlinked folder
                raise PipelineError(f"FOMOD: the source {item.source!r} is outside the archive")
            try:
                target = safe_join(dest, item.destination)
            except PipelineError:
                raise PipelineError(f"FOMOD: the destination {item.destination!r} is outside the mod folder") from None
            copied += tree_size(src) if src.is_dir() else src.stat().st_size
            if copied > self.limit:
                raise PipelineError(f"FOMOD: the installed files are larger than the limit of {self.limit / GIB:g} GiB. "
                                    "If the installer is correct, increase the limit with --extract-limit.")
            if item.is_folder or src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        if not any(dest.iterdir()):
            raise PipelineError("The FOMOD installer produced no files. Examine the recorded choices.")
