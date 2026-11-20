"""Mod list -> Nexus collection.

  resolve   each Nexus URL -> mod name, version and file id (v3 API); other HTTPS URLs stay "direct"
  download  every archive into the downloads cache (cached by MD5, so re-runs are cheap)
  install   into a scratch openmw.cfg under the build directory, never into your real one
  sort      plox sorts the plugins; the result becomes the collection's load order
  bundle    collection.json (+ load order) packed as an archive
  upload    upload session -> finalise -> create the collection (or a new revision of one)

The collection is created as a draft revision. Order of the mods in the list is kept as the
data directory order, so later entries override earlier ones, as in OpenMW's data= semantics.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path

from mwpipe.modlist import ModEntry, parse_mod_list
from mwpipe.nexus import NexusClient, download, filename_from_url
from mwpipe.pipeline import BASE_MASTERS, Pipeline, _plugins_by_name
from mwpipe.settings import Settings
from mwpipe.util import PipelineError, load_json, log, save_json

COLLECTION_SCHEMA_VORTEX = 1  # GraphQL docs: 1 = Vortex, 2 = Wabbajack
DEFAULT_GAME = "morrowind"
NAME_LIMITS = (3, 36)         # enforced by the v3 spec
SUMMARY_LIMIT = 255
GENERIC_SEGMENTS = {"download", "raw", "-", ""}
MAGIC_SUFFIX = ((b"PK\x03\x04", ".zip"), (b"7z\xbc\xaf\x27\x1c", ".7z"), (b"Rar!", ".rar"))


@dataclass
class ListedMod:
    entry: ModEntry
    name: str
    version: str
    author: str | None = None
    adult: bool = False
    file_id: int | None = None
    archive: Path | None = None
    md5: str | None = None

    @property
    def cache_key(self) -> str:
        e = self.entry
        return f"{e.game}/{e.mod_id}/{self.file_id}" if e.is_nexus else e.url


# -- resolving and downloading ---------------------------------------------------------------------
def direct_name(url: str) -> str:
    """A readable name for a non-Nexus URL: the project in a GitLab/GitHub path, else the file stem."""
    parts = urllib.parse.urlsplit(url)
    segments = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
    if "-" in segments and segments.index("-") > 0:
        return segments[segments.index("-") - 1]
    if segments and segments[-1] not in GENERIC_SEGMENTS:
        return Path(segments[-1]).stem
    return parts.hostname or "download"


def resolve(client: NexusClient, entries: list[ModEntry], allow_missing: bool) -> list[ListedMod]:
    mods: list[ListedMod] = []
    problems: list[str] = []
    for entry in entries:
        if not entry.is_nexus:
            mods.append(ListedMod(entry, direct_name(entry.url), "unknown"))
            continue
        try:
            page = client.mod(entry.game, entry.mod_id)
            file_id = entry.file_id
            if file_id is None:
                main = page.get("main_file_version")
                if not main:
                    raise PipelineError("it has no downloadable main file (none passed the security scan)")
                file_id = int(main["game_scoped_id"])
            version = client.file_version(entry.game, file_id)
            mods.append(ListedMod(entry, page["name"], version.get("version") or page.get("version") or "unknown",
                                  page.get("author"), bool(page.get("adult_content")), file_id))
            log.info("resolved %s %s (file %d)", page["name"], mods[-1].version, file_id)
        except PipelineError as err:  # includes ApiError (404, 403, 451 for adult mods, ...)
            problems.append(f"- {entry.url}: {err}")
    if problems:
        report = "Could not resolve:\n" + "\n".join(problems)
        if not allow_missing:
            raise PipelineError(report + "\nFix the list, or use --allow-missing to leave these mods out.")
        log.warning("%s\nleaving them out", report)
    return mods


def _sniff_suffix(path: Path) -> Path:
    """Direct downloads often end in '/download'; name the archive after what it really is."""
    head = path.read_bytes()[:8] if path.stat().st_size else b""
    for magic, suffix in MAGIC_SUFFIX:
        if head.startswith(magic):
            return path.rename(path.with_name(path.name + suffix))
    return path


def fetch_archives(pipe: Pipeline, client: NexusClient, mods: list[ListedMod], cache_file: Path) -> None:
    """Fill in `archive` and `md5` for every mod, downloading what is not in the downloads cache yet."""
    cache = load_json(cache_file, {})
    downloads = pipe.s.downloads_dir
    for mod in mods:
        record = cache.get(mod.cache_key)
        if record and (path := downloads / record["file"]).is_file() and pipe._md5(path) == record["md5"]:
            mod.archive, mod.md5 = path, record["md5"]
            continue
        e = mod.entry
        if e.is_nexus:
            client.require_premium()
            url = client.file_url(e.game, e.mod_id, mod.file_id)
            name = filename_from_url(url, f"{e.mod_id}-{mod.file_id}.archive")
            if (downloads / name).exists():  # same name, maybe different content: keep both
                name = f"{e.mod_id}-{mod.file_id}-{name}"
        else:
            url = e.url
            ids = [s for s in urllib.parse.urlsplit(url).path.split("/") if s.isdigit()]
            name = "-".join([mod.name, *ids[:1]])
        dest = downloads / name
        download(url, dest)
        if not e.is_nexus:
            dest = _sniff_suffix(dest)
        mod.archive, mod.md5 = dest, pipe._md5(dest)
        cache[mod.cache_key] = {"file": dest.name, "md5": mod.md5}
        save_json(cache_file, cache)


# -- manifests -------------------------------------------------------------------------------------
def v3_manifest(info: dict, mods: list[ListedMod], game: str) -> dict:
    """The `collection_manifest` object of the v3 API (snake_case, no load order fields)."""
    entries = []
    for m in mods:
        source = {"type": "nexus" if m.entry.is_nexus else "direct", "md5": m.md5,
                  "update_policy": "exact", "logical_filename": m.archive.name}
        if m.entry.is_nexus:
            source |= {"mod_id": str(m.entry.mod_id), "file_id": str(m.file_id)}
        else:
            source["url"] = m.entry.url
        mod = {"name": m.name, "version": m.version, "optional": False, "domain_name": game, "source": source}
        if m.author:
            mod["author"] = m.author
        entries.append(mod)
    return {"info": {k: v for k, v in info.items() if v}, "mods": entries}


def vortex_manifest(info: dict, mods: list[ListedMod], game: str, load_order: list[str],
                    disabled: list[str] = ()) -> dict:
    """collection.json as stored inside a collection bundle (camelCase, with the plox load order).

    This is the shape `mwpipe run` reads back: with a loadOrder present it keeps the order as is.
    """
    camel = {"author": "author", "author_url": "authorUrl", "name": "name", "summary": "summary",
             "description": "description", "domain_name": "domainName"}
    entries = []
    for m in mods:
        source = {"type": "nexus" if m.entry.is_nexus else "direct", "md5": m.md5,
                  "updatePolicy": "exact", "logicalFilename": m.archive.name}
        if m.entry.is_nexus:
            source |= {"modId": m.entry.mod_id, "fileId": m.file_id}
        else:
            source["url"] = m.entry.url
        entries.append({"name": m.name, "version": m.version, "optional": False, "domainName": game,
                        "author": m.author or "", "source": source})
    return {"info": {camel[k]: v for k, v in info.items() if v}, "mods": entries, "modRules": [],
            "loadOrder": [{"id": n, "name": n, "enabled": True} for n in load_order]
                         + [{"id": n, "name": n, "enabled": False} for n in disabled]}


def make_bundle(out_dir: Path) -> Path:
    """Pack out_dir/collection.json: .7z (what Vortex produces) when 7-Zip exists, else .zip."""
    seven = shutil.which("7z")
    bundle = out_dir / ("collection.7z" if seven else "collection.zip")
    for stale in ("collection.7z", "collection.zip"):
        (out_dir / stale).unlink(missing_ok=True)
    if seven:
        result = subprocess.run([seven, "a", "-t7z", "-bso0", "-bsp0", bundle.name, "collection.json"],
                                cwd=out_dir, capture_output=True, text=True)
        if result.returncode != 0:
            raise PipelineError(f"7z failed: {result.stderr.strip()[-300:]}")
    else:
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(out_dir / "collection.json", "collection.json")
    return bundle


# -- orchestration -----------------------------------------------------------------------------
def build_collection(settings: Settings, client: NexusClient, modlist: str, *, name: str,
                     summary: str = "", description: str = "", game: str | None = None,
                     collection_id: str | None = None, upload: bool = True,
                     output_dir: Path | None = None, allow_missing: bool = False) -> dict:
    if not NAME_LIMITS[0] <= len(name) <= NAME_LIMITS[1]:
        raise PipelineError(f"The collection name must be {NAME_LIMITS[0]}-{NAME_LIMITS[1]} characters: {name!r}")
    if len(summary) > SUMMARY_LIMIT:
        raise PipelineError(f"The summary is limited to {SUMMARY_LIMIT} characters.")
    parsed = parse_mod_list(modlist)
    entries = parsed.entries
    game = game or next((e.game for e in entries if e.is_nexus), DEFAULT_GAME)
    client.require_premium()  # downloads need it; fail before doing any work
    mods = resolve(client, entries, allow_missing)
    if not mods:
        raise PipelineError("No mods left to put in the collection.")

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "collection"
    build_dir = settings.state_dir / "builds" / slug
    out_dir = output_dir or build_dir / "out"
    # A private pipeline: own state and openmw.cfg, so the user's real setup is left alone.
    build_dir.mkdir(parents=True, exist_ok=True)
    save_json(build_dir / "overrides.json", {"disable": parsed.disabled})  # your own overrides.json is not used
    scratch = dataclasses.replace(settings, openmw_cfg=build_dir / "openmw.cfg", state_dir=build_dir,
                                  overrides_file=build_dir / "overrides.json", dry_run=False)
    pipe = Pipeline(scratch, client_factory=lambda: client, sort_mode="plox")
    scratch.ensure_dirs()
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("== download ==")
    fetch_archives(pipe, client, mods, build_dir / "archives.json")

    draft_dir = build_dir / "draft"
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft = vortex_manifest({"name": name}, mods, game, [])
    save_json(draft_dir / "collection.json", draft)
    save_json(pipe.current_file, {"slug": f"build-{slug}", "game": game, "revision": 0, "name": name,
                                  "dir": str(draft_dir.relative_to(build_dir))})
    log.info("== install, configure, sort (plox) ==")
    pipe.run(["install", "configure", "sort"], None)
    present = _plugins_by_name(pipe.load_cfg().data)
    for name in parsed.disabled:
        if name.lower() not in present:
            log.warning("'- %s' matches no plugin of the listed mods (check the spelling)", name)
    base = {m.lower() for m in BASE_MASTERS}
    load_order = [c for c in pipe.load_cfg().content if c.lower() not in base]
    log.info("plox load order: %d plugins", len(load_order))

    user = client.user()
    info = {"author": user.get("name") or "unknown",
            "author_url": f"https://www.nexusmods.com/users/{user['user_id']}" if user.get("user_id") else "",
            "name": name, "summary": summary,
            "description": description or f"{len(mods)} mods for OpenMW, assembled with mwpipe. "
                                          "Plugins are sorted with plox.",
            "domain_name": game}
    manifest = v3_manifest(info, mods, game)
    collection_data = {"adult_content": any(m.adult for m in mods), "collection_manifest": manifest,
                       "collection_schema_id": COLLECTION_SCHEMA_VORTEX}
    save_json(out_dir / "collection.json", vortex_manifest(info, mods, game, load_order, parsed.disabled))
    save_json(out_dir / "collection-data.json", collection_data)
    (out_dir / "load-order.txt").write_text("\n".join(load_order) + "\n", encoding="utf-8")
    bundle = make_bundle(out_dir)
    result = {"mods": len(mods), "plugins": len(load_order), "disabled": parsed.disabled, "bundle": str(bundle), "output_dir": str(out_dir)}
    if not upload:
        log.info("--no-upload: nothing was sent. Files are in %s", out_dir)
        return result

    log.info("== upload ==")
    upload_id = client.upload_file(bundle, bundle.name)
    created = client.create_collection(upload_id, collection_data, collection_id)
    cid = collection_id or created["id"]
    result |= {"collection_id": cid, "revision": created.get("revision_number"),
               "status": created.get("revision_status")}
    if not collection_id:
        result["url"] = f"https://www.nexusmods.com/games/{game}/collections/{created['slug']}"
    return result
