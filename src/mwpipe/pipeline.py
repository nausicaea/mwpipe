"""The pipeline steps. Each step is idempotent. Each step stores its results in Settings.state_dir
(downloads and hashes in Settings.cache_dir, editable files in Settings.config_dir).

  fetch       collection URL/nxm/slug -> collection.json
  download    every mod archive through the Premium API (the step uses the files that are in the downloads cache already)
  install     extract archives into ModData/<collection-name-slug>/, one data folder per mod
  configure   rewrite data=, fallback-archive=, content=, groundcover= (+ fallback= if missing)
  clean       (optional) tes3cmd clean on the collection plugins
  sort        keep the load order of the collection, or sort with plox (see Pipeline.sort)
  delta       delta_plugin merge  -> Generated/delta-merged.omwaddon
  groundcover GOCoverify          -> Generated/deleted_groundcover.omwaddon, groundcover.omwaddon, meshes/
  lightfix    Lumenaire           -> Generated/Lumenaire.omwaddon
  textures    report textures that meshes use and no data folder or BSA provides (read-only, optional: --check-textures)
"""

from __future__ import annotations

import configparser
import hashlib
import json
import re
import shutil
import struct
from pathlib import Path

from mwpipe import tools
from mwpipe.archives import extract
from mwpipe.collection import Collection, Mod
from mwpipe.install import PLUGIN_EXTS, install_mod, lowercase_textures, prune_to_plugins
from mwpipe.nexus import CollectionRef, NexusClient, download, filename_from_url
from mwpipe.openmw_cfg import OpenMWConfig, dedupe, import_ini_fallbacks
from mwpipe.settings import Settings
from mwpipe.textures import report as report_textures, scan_missing_textures
from mwpipe.util import PipelineError, load_json, log, md5sum, save_json

ALL_STEPS = ["fetch", "download", "install", "configure", "clean",
             "sort", "delta", "groundcover", "lightfix", "textures"]
DEFAULT_STEPS = [s for s in ALL_STEPS if s not in ("clean", "textures")]
POSTPROCESS_STEPS = ["sort", "delta", "groundcover", "lightfix"]
BASE_MASTERS = ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"]
BASE_ARCHIVES = ["Morrowind.bsa", "Tribunal.bsa", "Bloodmoon.bsa"]
LUMENAIRE_DEFAULTS = Path(__file__).parent / "data" / "lumenaire.toml"


def collection_dir_name(name: str) -> str:
    """Directory name for a collection in ModData: its name in lower case. Each run of other characters changes to '-'."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "collection"


class Pipeline:
    def __init__(self, settings: Settings, client_factory=None, include_optional=False,
                 allow_missing=False, lowercase_dds=False, sort_mode="auto",
                 delta_skip=("Cell", "Dialogue", "DialogueInfo"), enlightened_flames=False, plugins_only=False,
                 show_options=False):
        self.s = settings
        self.show_options = show_options  # log the FOMOD options and data folders each archive offers
        self.plugins_only = plugins_only  # keep only plugin files of installed mods (build-collection's private copy)
        self._client_factory = client_factory
        self._client: NexusClient | None = None
        self.include_optional = include_optional
        self.allow_missing = allow_missing
        self.lowercase_dds = lowercase_dds
        self.delta_skip = list(delta_skip)
        self.sort_mode = sort_mode
        self.enlightened_flames = enlightened_flames
        self.overrides = load_json(settings.overrides_file, {})
        self.current_file = settings.state_dir / "current.json"
        self.manifest_file = settings.state_dir / "manifest.json"
        self.md5_cache_file = settings.cache_dir / "md5-cache.json"
        self._md5_cache: dict | None = None            # name -> [size, mtime, md5], persisted
        self._md5_index: dict[str, Path] | None = None  # md5 -> archive in downloads_dir, rebuilt lazily

    # -- helpers -----------------------------------------------------------------------------
    @property
    def client(self) -> NexusClient:
        if self._client is None:
            if self._client_factory is None:
                raise PipelineError("This step needs the Nexus API, but no key is configured.")
            self._client = self._client_factory()
        return self._client

    def current(self) -> dict:
        cur = load_json(self.current_file, None)
        if cur is None:
            raise PipelineError("No collection is fetched yet. Run the 'fetch' step with a collection URL.")
        return cur

    def _collection_dir(self) -> Path:
        return self.s.state_dir / self.current()["dir"]  # stored relative to state_dir; absolute also works

    def collection(self) -> Collection:
        return Collection.load(self._collection_dir() / "collection.json",
                               self.overrides, self.include_optional)

    def dedupe_downloads(self, only: Path | None = None) -> list[tuple[Path, Path]]:
        """Remove identical archives from the downloads directory.

        Return (removed, kept) pairs. The code keeps the shortest name. If you give `only`, the code compares
        only that file, and removes it if a twin exists. This method obeys --dry-run.
        """
        by_size: dict[int, list[Path]] = {}
        for f in sorted(self.s.downloads_dir.iterdir()) if self.s.downloads_dir.is_dir() else []:
            if f.is_file() and f.suffix != ".part":
                by_size.setdefault(f.stat().st_size, []).append(f)
        merged: list[tuple[Path, Path]] = []
        for size, files in by_size.items():
            if len(files) < 2 or (only is not None and only not in files):
                continue
            by_md5: dict[str, list[Path]] = {}
            for f in files:
                by_md5.setdefault(self._md5(f), []).append(f)
            for group in by_md5.values():
                if len(group) < 2 or (only is not None and only not in group):
                    continue
                if only is not None:
                    keep = min((f for f in group if f != only), key=lambda f: (len(f.name), f.name))
                    drop = [only]
                else:
                    keep = min(group, key=lambda f: (len(f.name), f.name))
                    drop = [f for f in group if f != keep]
                for f in drop:
                    log.info("%sduplicate %s == %s", "would remove " if self.s.dry_run else "removing ", f.name, keep.name)
                    if not self.s.dry_run:
                        f.unlink()
                    merged.append((f, keep))
        return merged

    def _md5(self, path: Path) -> str:
        """MD5 with a (size, mtime) cache. A new run does not hash the same archives again."""
        if self._md5_cache is None:
            self._md5_cache = load_json(self.md5_cache_file, {})
        st = path.stat()
        entry = self._md5_cache.get(path.name)
        if entry and entry[0] == st.st_size and entry[1] == st.st_mtime:
            return entry[2]
        digest = md5sum(path)
        self._md5_cache[path.name] = [st.st_size, st.st_mtime, digest]
        save_json(self.md5_cache_file, self._md5_cache)
        return digest

    def _archives(self) -> dict[str, Path]:
        """All files in the downloads cache, with the MD5 as key."""
        if self._md5_index is None:
            files = sorted(p for p in self.s.downloads_dir.iterdir() if p.is_file() and p.suffix != ".part")
            self._md5_index = {self._md5(p): p for p in files}
        return self._md5_index

    def _find_archive(self, mod: Mod) -> Path | None:
        """The archive of a mod in the downloads cache. Find it by MD5, or by file name if the collection has no MD5."""
        if md5 := (mod.source.get("md5") or "").lower():
            return self._archives().get(md5)
        name = (mod.source.get("logicalFilename") or "").lower()
        return next((p for p in self._archives().values() if p.name.lower() == name), None) if name else None

    def load_cfg(self) -> OpenMWConfig:
        return OpenMWConfig.load(self.s.openmw_cfg)

    def save_cfg(self, cfg: OpenMWConfig) -> None:
        if self.s.dry_run:
            log.info("[dry-run] would write %s:\n%s", self.s.openmw_cfg, cfg.render())
            return
        cfg.save(self.s.openmw_cfg, self.s.state_dir / "backups")

    # -- steps -------------------------------------------------------------------------------
    def fetch(self, ref: CollectionRef) -> None:
        self.client.require_premium()  # fail before downloading anything
        url, revision, name = self.client.collection_archive_url(ref)
        archive = self.s.downloads_dir / f"collection-{ref.slug}-{revision}{Path(filename_from_url(url, 'x.7z')).suffix}"
        previous = md5sum(archive) if archive.exists() else None
        download(url, archive)  # always: Nexus edits a draft revision in place, so a cached copy goes stale
        if previous and previous != md5sum(archive):
            log.info("collection %s revision %d changed upstream since the last run", ref.slug, revision)
        target = self.s.state_dir / "collections" / f"{ref.slug}-{revision}"
        extract(archive, target)
        manifest = next(target.rglob("collection.json"), None)
        if manifest is None:
            raise PipelineError(f"{archive.name} does not contain collection.json")
        save_json(self.current_file, {"slug": ref.slug, "game": ref.game, "revision": revision,
                                      "name": name, "dir": str(manifest.parent.relative_to(self.s.state_dir))})
        log.info("collection %r revision %d: %d mods", name, revision,
                 len(json.loads(manifest.read_text(encoding="utf-8")).get("mods", [])))

    def download(self) -> None:
        game = self.current()["game"]
        bundle_dir = self._collection_dir() / "bundled"
        missing: list[str] = []
        for mod in self.collection().mods:
            if self._find_archive(mod):
                continue
            src, kind = mod.source, mod.source.get("type")
            try:
                if kind == "nexus":
                    self.client.require_premium()
                    self._download(mod, self.client.file_url(game, int(src["modId"]), int(src["fileId"])))
                elif kind == "bundle":
                    bundled = next(bundle_dir.glob(src.get("fileExpression", "") + "*"), None)
                    if bundled is None:
                        raise PipelineError(f"the bundled file for {mod.name} is not in the collection")
                    shutil.copy2(bundled, self.s.downloads_dir / bundled.name)
                elif kind == "direct" and src.get("url", "").startswith("https://"):
                    self._download(mod, src["url"])
                else:
                    missing.append(f"- {mod.name}: {src.get('url', '')} {src.get('instructions', '')}".rstrip())
            except PipelineError as err:
                missing.append(f"- {mod.name}: {err}")
        if missing:
            report = ("These mods are not hosted on Nexus. Download them by hand into "
                      f"{self.s.downloads_dir} (mwpipe matches them by MD5) and run again:\n"
                      + "\n".join(missing))
            if not self.allow_missing:
                raise PipelineError(report)
            log.warning(report)

    def _download(self, mod: Mod, url: str) -> None:
        name = filename_from_url(url, f"{mod.key}.archive")
        if self.s.dry_run:
            log.info("[dry-run] would download %s", name)
            return
        dest = self.s.downloads_dir / name
        if dest.exists():  # same name, different content: keep both
            dest = dest.with_name(f"{mod.key}-{name}")
        download(url, dest, mod.source.get("md5"))
        self._md5_index = None  # pick up the new file on the next lookup

    def install(self) -> None:
        cur = self.current()
        manifest = load_json(self.manifest_file, {})
        installed = {}
        mods = self.collection().data_order()
        for mod in mods:
            archive = self._find_archive(mod)
            if archive is None:
                if self.allow_missing:
                    log.warning("not installed (archive is missing): %s", mod.name)
                    continue
                raise PipelineError(f"The archive for {mod.name} is missing. Run the download step.")
            mod_dir = self.s.mods_dir / (collection_dir_name(cur["name"]) if cur.get("name") else cur["slug"]) / mod.dir_name
            spec = _spec_hash(mod, self._md5(archive))
            previous = manifest.get(mod.key)
            if previous and previous["spec"] == spec and all(Path(r).is_dir() for r in previous["roots"]):
                if self.plugins_only and not self.s.dry_run:
                    prune_to_plugins([Path(r) for r in previous["roots"]])  # also slims installs from before this existed
                if self.show_options and previous.get("options"):  # recorded when it was installed
                    log.info("\n".join(previous["options"]))
                installed[mod.key] = previous
                continue
            if mod.patches:
                log.warning("%s: mwpipe does not apply the binary patches from the collection", mod.name)
            log.info("installing %s", mod.name)
            if self.s.dry_run:
                continue
            hints: list[str] = []
            roots = install_mod(mod, archive, mod_dir, self.s.cache_dir / "scratch", hints if self.show_options else None)
            if hints:
                log.info("\n".join(hints))
            if self.plugins_only:
                prune_to_plugins(roots)
            elif self.lowercase_dds:
                lowercase_textures(mod_dir)
            installed[mod.key] = {"name": mod.name, "spec": spec, "roots": [str(r) for r in roots],
                                  **({"options": hints} if hints else {})}
            save_json(self.manifest_file, {**manifest, **installed})
        # Store the data order for `configure`. Remove the mods that are no longer in the collection.
        if not self.s.dry_run:
            ordered = [installed[m.key] for m in mods if m.key in installed]
            save_json(self.manifest_file, {**installed, "_order": [r for e in ordered for r in e["roots"]]})

    def configure(self) -> None:
        manifest = load_json(self.manifest_file, {})
        mod_roots = [Path(r) for r in manifest.get("_order", [])]
        ov = self.overrides
        cfg = self.load_cfg()

        cfg.data = [self.s.morrowind_data, *mod_roots, *map(Path, ov.get("extra_data", [])),
                    self.s.generated_dir]
        cfg.archives = [a for a in BASE_ARCHIVES if (self.s.morrowind_data / a).exists()]
        cfg.archives += [p.name for root in mod_roots for p in sorted(root.glob("*"))
                         if p.suffix.lower() == ".bsa"]

        available = _plugins_by_name(cfg.data[:-1])
        groundcover = {g.lower() for g in ov.get("groundcover", [])}
        collection = self.collection() if self.current_file.exists() else None
        disabled = {d.lower() for d in ov.get("disable", [])} | groundcover
        if collection:
            disabled |= collection.disabled_plugins()
        order = collection.plugin_order() if collection else None
        if order is None:  # no author load order: masters first, then data order (plox refines it)
            is_master = lambda p: p.suffix.lower() in (".esm", ".omwgame")  # noqa: E731
            order = [p.name for p in sorted(available.values(), key=lambda p: not is_master(p))]
        # Base masters are first. If the order of the collection puts a plugin before a base master
        # (for example, an .esm that must load before Bloodmoon.esm), keep the positions of the collection.
        base_l = {m.lower() for m in BASE_MASTERS}
        names = [n.lower() for n in order]
        keep_positions = any(n in base_l and any(o not in base_l for o in names[:i]) for i, n in enumerate(names))
        content = [m for m in BASE_MASTERS if m.lower() in available and not (keep_positions and m.lower() in names)]
        for name in [*order, *ov.get("extra_content", [])]:
            path = available.get(name.lower())
            if path is None:
                log.warning("the load order has %s, but no installed mod provides it", name)
            elif name.lower() not in disabled:
                content.append(path.name)
        # The Vortex Morrowind load order usually does not have Lua mods and .omwaddon files.
        listed = {c.lower() for c in content}
        for key, path in available.items():
            if key not in listed and key not in disabled and path.suffix.lower() in (".omwscripts", ".omwaddon"):
                content.append(path.name)
        cfg.content = _drop_missing_masters(dedupe(content), available)
        cfg.groundcover = [available[g].name for g in groundcover if g in available]

        if not cfg.fallbacks:
            if self.s.morrowind_ini.is_file():
                for name, value in import_ini_fallbacks(self.s.morrowind_ini):
                    cfg.set_fallback(name, value)
            else:
                log.warning("openmw.cfg has no fallback= values and %s was not found; "
                            "OpenMW will not start without them", self.s.morrowind_ini)
        for name, value in ov.get("fallback", {}).items():
            cfg.set_fallback(name, value)
        self.save_cfg(cfg)
        log.info("openmw.cfg: %d data paths, %d plugins, %d archives",
                 len(cfg.data), len(cfg.content), len(cfg.archives))

    def clean(self) -> None:
        cfg = self.load_cfg()
        available = _plugins_by_name(cfg.data)
        plugins = [available[c.lower()] for c in cfg.content if c.lower() in available]
        targets = [p for p in plugins if p.suffix.lower() == ".esp" and p.is_relative_to(self.s.mods_dir)]
        lua = [p for p in targets if _needs_lua_records(p, available)]
        for p in lua:
            log.info("not cleaning %s: tes3cmd cannot read OpenMW Lua records (LUAL) in it or its masters", p.name)
        targets = [p for p in targets if p not in lua]
        changed = tools.tes3cmd_clean(targets, plugins, self.s.state_dir / "tes3cmd",
                                      self.s.state_dir / "uncleaned-originals", self.s.dry_run)
        log.info("tes3cmd cleaned %d of %d plugins", len(changed), len(targets))

    def strip_generated(self) -> None:
        """Remove the generated plugins. Then the tools do not merge their own previous output."""
        cfg = self.load_cfg()
        generated = {g.lower() for g in tools.GENERATED_CONTENT}
        cfg.content = [c for c in cfg.content if c.lower() not in generated]
        cfg.groundcover = [g for g in cfg.groundcover if g.lower() != tools.GROUNDCOVER_PLUGIN.lower()]
        if self.s.generated_dir not in cfg.data:
            cfg.data.append(self.s.generated_dir)
        self.save_cfg(cfg)

    def sort(self) -> None:
        """Set the plugin order.

        The author tested the load order of the collection. By default ("auto"), the code keeps it.
        plox runs only if the collection has no load order or if you add plugins in the overrides.
        "plox" always sorts. "collection" never sorts.
        """
        has_order = self.current_file.exists() and self.collection().plugin_order() is not None
        rules = list(self.overrides.get("plox_rules", []))
        added = bool(self.overrides.get("extra_content"))
        if self.sort_mode == "collection" or (self.sort_mode == "auto" and has_order and not added):
            log.info("keeping the load order of the collection")
            if rules:
                log.warning("plox_rules ignored: the load order of the collection is kept (use --sort plox to apply them)")
            return
        before = self.load_cfg()
        tools.plox_sort(self.s.openmw_cfg, self.s.cache_dir / "plox", self.s.dry_run, rules)
        after = self.load_cfg()
        if sorted(map(str.lower, before.content)) != sorted(map(str.lower, after.content)):
            log.error("plox changed the set of plugins (not only their order); restoring")
            self.save_cfg(before)
        else:
            self.save_cfg(after)  # normalise layout back into the managed block

    def delta(self) -> None:
        tools.delta_merge(self.s.openmw_cfg, self.s.generated_dir, self.delta_skip, self.s.dry_run)
        self._register([tools.DELTA_OUTPUT])

    def groundcover(self) -> None:
        tools.gocoverify(self.s.openmw_cfg, self.s.generated_dir, self.s.dry_run)
        self._register([tools.GROUNDCOVER_DELETED], groundcover=[tools.GROUNDCOVER_PLUGIN])
        settings_cfg = self.s.openmw_cfg.parent / "settings.cfg"
        if not _groundcover_enabled(settings_cfg):
            log.warning("enable groundcover: add 'enabled = true' under [Groundcover] in %s", settings_cfg)

    def lightfix(self) -> None:
        settings = self.s.config_dir / "lumenaire.toml"  # yours, if you made one (read-only in Docker)
        if not settings.is_file():
            settings = LUMENAIRE_DEFAULTS
        tools.lumenaire(self.s.openmw_cfg, self.s.generated_dir, settings,
                        self.enlightened_flames, self.s.dry_run)
        self._register([tools.LIGHTS_OUTPUT])

    def textures(self) -> None:
        """Report the textures that the winning meshes use and that nothing provides. Show which mod needs each texture."""
        cfg = self.load_cfg()
        wanted = {a.lower() for a in cfg.archives}
        archives = [p for d in cfg.data if d.is_dir() for p in sorted(d.iterdir())
                    if p.is_file() and p.name.lower() in wanted]
        result = scan_missing_textures(cfg.data, archives, self.s.mods_dir)
        report_textures(result, self.s.state_dir / "missing-textures.txt")

    def _register(self, content: list[str], groundcover: list[str] = ()) -> None:
        if not self.s.dry_run:
            for name in [*content, *groundcover]:
                if not (self.s.generated_dir / name).is_file():
                    raise PipelineError(f"{name} is not in {self.s.generated_dir}, but the tool must create it")
        cfg = self.load_cfg()
        cfg.content += content
        cfg.groundcover += list(groundcover)
        self.save_cfg(cfg)

    # -- driver ------------------------------------------------------------------------------
    def run(self, steps: list[str], ref: CollectionRef | None) -> None:
        self.s.ensure_dirs()
        stripped = False
        for step in [s for s in ALL_STEPS if s in steps]:  # always in pipeline order
            log.info("== %s ==", step)
            if step in POSTPROCESS_STEPS and not stripped:
                self.strip_generated()
                stripped = True
            if step == "fetch":
                if ref is None:
                    raise PipelineError("The fetch step needs a collection URL, an nxm link or a slug.")
                self.fetch(ref)
            else:
                getattr(self, step)()


def _plugins_by_name(data_dirs: list[Path]) -> dict[str, Path]:
    """Map each lower-case plugin name to its file. Use the OpenMW rule: later data directories have priority."""
    found: dict[str, Path] = {}
    for root in data_dirs:
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if path.is_file() and path.suffix.lower() in PLUGIN_EXTS:
                    found.pop(path.name.lower(), None)  # re-insert to keep data order
                    found[path.name.lower()] = path
    return found


def plugin_masters(path: Path) -> list[str]:
    """Master names from the header of a TES3-format plugin. Return [] for anything else (scripts, files that cannot be read)."""
    if path.suffix.lower() not in (".esp", ".esm", ".omwaddon", ".omwgame"):
        return []
    try:
        with path.open("rb") as f:
            head = f.read(16)
            if len(head) < 16 or head[:4] != b"TES3":
                return []
            data = f.read(struct.unpack("<I", head[4:8])[0])
    except OSError:
        return []
    masters, pos = [], 0
    while pos + 8 <= len(data):
        tag, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if tag == b"MAST":
            masters.append(data[pos + 8:pos + 8 + size].split(b"\0")[0].decode("cp1252", "replace"))
        pos += 8 + size
    return masters


def _needs_lua_records(path: Path, available: dict[str, Path], _seen: frozenset = frozenset()) -> bool:
    """Return True if the plugin or one of its masters has OpenMW LUAL records. tes3cmd rejects these records."""
    try:
        if b"LUAL" in path.read_bytes():
            return True
    except OSError:
        return False
    seen = _seen | {path}
    return any(m in available and available[m] not in seen and _needs_lua_records(available[m], available, seen)
               for m in (n.lower() for n in plugin_masters(path)))


def _drop_missing_masters(content: list[str], available: dict[str, Path]) -> list[str]:
    """Remove the plugins that have masters that are not in the content list. The OpenMW tools stop when they find such plugins."""
    content = list(content)
    while True:
        present = {c.lower() for c in content}
        broken = {}
        for name in content:
            missing = [m for m in plugin_masters(available[name.lower()]) if m.lower() not in present] \
                if name.lower() in available else []
            if missing:
                broken[name] = missing
        if not broken:
            return content
        for name, missing in broken.items():
            log.warning("skipping %s: missing master %s (not installed or not enabled)", name, ", ".join(missing))
        content = [c for c in content if c not in broken]


def _spec_hash(mod: Mod, archive_md5: str) -> str:
    spec = {"md5": archive_md5, "choices": mod.choices, "hashes": mod.hashes,
            "data": mod.overrides.get("data")}
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def _groundcover_enabled(settings_cfg: Path) -> bool:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(settings_cfg, encoding="utf-8")
    except configparser.Error:
        return False
    return parser.get("Groundcover", "enabled", fallback="false").strip().lower() == "true"
