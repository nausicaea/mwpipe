"""Parse a mod list: one URL per line, `#` starts a comment.

  https://www.nexusmods.com/morrowind/mods/43931                     newest primary file
  https://www.nexusmods.com/morrowind/mods/43931?tab=files&file_id=1  that exact file
  nxm://morrowind/mods/43931/files/1                                  same, as an nxm link
  https://gitlab.com/group/project/-/package_files/1/download         any other HTTPS file ("direct")
  + <mod url> | 00 Core                                                install only this archive folder of that mod
  + <mod url> | Step > Group > Option                                  choose this option in that mod's FOMOD installer
  - k_weather (louder sounds).esp                                    leave this plugin out of the load order
  ! Distant_Seafloor_2.00.esm < Bloodmoon.esm                        load the first plugin before the second (plox rule)

A plain mod URL takes the mod's primary file. A mod with several files (for example separate textures and meshes) needs
one `?file_id=` line per wanted file, in the order they should load; `build-collection` logs a hint when a line without
file_id leaves other current files of the mod unused.

A mod that ships either/or plugins would otherwise enable all of them, which plox reports as a [CONFLICT].
A mod that ships alternative data folders ("00 Core", "02 Vanilla style", ...) would otherwise be installed
with all of them; one `+ url | folder` line per folder picks the ones to keep (archive-relative path).
A `+` line names its mod by URL (the file id is optional), so these lines can go anywhere in the list.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from mwpipe.util import PipelineError, log

NEXUS_MOD = re.compile(r"^/(?:games/)?(?P<game>[a-z0-9]+)/mods/(?P<mod>\d+)", re.I)
NXM_MOD = re.compile(r"^/mods/(?P<mod>\d+)(?:/files/(?P<file>\d+))?", re.I)


@dataclass
class ModList:
    entries: list["ModEntry"]
    disabled: list[str]   # plugin file names from "- name" lines
    rules: list[str] = field(default_factory=list)   # mlox rule blocks from "! a < b" lines
    picks: list["Pick"] = field(default_factory=list)   # "+ url | ..." lines; a mod is matched once file ids are known


@dataclass(frozen=True)
class Pick:
    """A "+ url | folder" or "+ url | Step > Group > Option" line."""
    line: int
    ref: "ModEntry"
    folder: str | None = None
    option: tuple | None = None   # (step, group, option)

    def applies_to(self, game, mod_id, file_id, url=None) -> bool:
        """Does this pick name the mod (and, if the line gave one, the file) with these ids?"""
        if not self.ref.is_nexus:
            return url == self.ref.url
        return (game, mod_id) == (self.ref.game, self.ref.mod_id) and self.ref.file_id in (None, file_id)


@dataclass(frozen=True)
class ModEntry:
    url: str
    game: str | None = None      # Nexus game domain, None for direct downloads
    mod_id: int | None = None
    file_id: int | None = None   # None: pick the mod's primary file

    @property
    def is_nexus(self) -> bool:
        return self.mod_id is not None

    @property
    def ident(self):
        """What makes two list lines the same mod."""
        return (self.game, self.mod_id, self.file_id) if self.is_nexus else self.url


def parse_entry(line: str) -> ModEntry:
    url = line.strip()
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    file_id = int(query["file_id"][0]) if query.get("file_id", [""])[0].isdigit() else None
    host = (parts.hostname or "").lower()
    if parts.scheme in ("http", "https") and host in ("nexusmods.com", "www.nexusmods.com"):
        if m := NEXUS_MOD.match(parts.path):
            return ModEntry(url, m["game"].lower(), int(m["mod"]), file_id)
        raise PipelineError(f"not a Nexus mod page: {url}")
    if parts.scheme == "nxm":
        if m := NXM_MOD.match(parts.path):
            return ModEntry(url, host, int(m["mod"]), int(m["file"]) if m["file"] else file_id)
        raise PipelineError(f"not an nxm mod link: {url}")
    if parts.scheme == "https" and host:
        return ModEntry(url)
    raise PipelineError(f"unsupported URL (need https:// or nxm://): {url}")


def parse_mod_list(text: str) -> ModList:
    """All entries in order. Duplicates are dropped with a warning; bad lines are all reported at once."""
    entries: list[ModEntry] = []
    disabled: list[str] = []
    rules: list[str] = []
    picks: list[tuple[int, ModEntry, str]] = []   # "+" lines, resolved once every mod line is known
    errors: list[str] = []
    seen: set = set()
    for number, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        if line.startswith("!"):
            first, sep, second = (part.strip() for part in line[1:].partition("<"))
            if sep and first and second:
                rules.append(f"[Order]\n{first}\n{second}")
            else:
                errors.append(f"line {number}: '!' needs 'first.esp < second.esp'")
            continue
        if line.startswith("+"):
            ref, sep, what = (part.strip() for part in line[1:].partition("|"))
            if not (sep and ref and what):
                errors.append(f"line {number}: '+' needs 'mod url | archive folder' or 'mod url | step > group > option'")
                continue
            try:
                picks.append((number, parse_entry(ref), what))
            except PipelineError as err:
                errors.append(f"line {number}: {err}")
            continue
        if line.startswith("-"):
            if name := line[1:].strip():
                disabled.append(name)
            else:
                errors.append(f"line {number}: '-' needs a plugin file name")
            continue
        try:
            entry = parse_entry(line)
        except PipelineError as err:
            errors.append(f"line {number}: {err}")
            continue
        key = entry.ident
        if key in seen:
            log.warning("line %d: duplicate, ignored: %s", number, line)
            continue
        seen.add(key)
        entries.append(entry)
    parsed_picks: list[Pick] = []
    for number, ref, what in picks:
        same = [e for e in entries if (e.game, e.mod_id) == (ref.game, ref.mod_id) and (e.is_nexus or e.url == ref.url)]
        if not same:
            errors.append(f"line {number}: '+' names a mod that is not in the list: {ref.url}")
            continue
        if ref.is_nexus and ref.file_id is None and len(same) > 1:
            errors.append(f"line {number}: '+' matches several files of that mod, add ?file_id=N: {ref.url}")
            continue
        if ">" in what:
            parts = tuple(part.strip() for part in what.split(">"))
            if len(parts) != 3 or not all(parts):
                errors.append(f"line {number}: a FOMOD choice is 'step > group > option'")
                continue
            parsed_picks.append(Pick(number, ref, option=parts))
        else:
            parsed_picks.append(Pick(number, ref, folder=what))
    if errors:
        raise PipelineError("Invalid mod list:\n  " + "\n  ".join(errors))
    if not entries:
        raise PipelineError("The mod list contains no URLs.")
    games = {e.game for e in entries if e.is_nexus}
    if len(games) > 1:
        raise PipelineError(f"A collection belongs to one game, but the list mixes: {', '.join(sorted(games))}")
    return ModList(entries, disabled, rules, parsed_picks)
