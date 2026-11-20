"""Parse a mod list: one URL per line, `#` starts a comment.

  https://www.nexusmods.com/morrowind/mods/43931                     newest primary file
  https://www.nexusmods.com/morrowind/mods/43931?tab=files&file_id=1  that exact file
  nxm://morrowind/mods/43931/files/1                                  same, as an nxm link
  https://gitlab.com/group/project/-/package_files/1/download         any other HTTPS file ("direct")
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from mwpipe.util import PipelineError, log

NEXUS_MOD = re.compile(r"^/(?:games/)?(?P<game>[a-z0-9]+)/mods/(?P<mod>\d+)", re.I)
NXM_MOD = re.compile(r"^/mods/(?P<mod>\d+)(?:/files/(?P<file>\d+))?", re.I)


@dataclass(frozen=True)
class ModEntry:
    url: str
    game: str | None = None      # Nexus game domain, None for direct downloads
    mod_id: int | None = None
    file_id: int | None = None   # None: pick the mod's primary file

    @property
    def is_nexus(self) -> bool:
        return self.mod_id is not None


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


def parse_mod_list(text: str) -> list[ModEntry]:
    """All entries in order. Duplicates are dropped with a warning; bad lines are all reported at once."""
    entries: list[ModEntry] = []
    errors: list[str] = []
    seen: set = set()
    for number, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        try:
            entry = parse_entry(line)
        except PipelineError as err:
            errors.append(f"line {number}: {err}")
            continue
        key = (entry.game, entry.mod_id, entry.file_id) if entry.is_nexus else entry.url
        if key in seen:
            log.warning("line %d: duplicate, ignored: %s", number, line)
            continue
        seen.add(key)
        entries.append(entry)
    if errors:
        raise PipelineError("Invalid mod list:\n  " + "\n  ".join(errors))
    if not entries:
        raise PipelineError("The mod list contains no URLs.")
    games = {e.game for e in entries if e.is_nexus}
    if len(games) > 1:
        raise PipelineError(f"A collection belongs to one game, but the list mixes: {', '.join(sorted(games))}")
    return entries
