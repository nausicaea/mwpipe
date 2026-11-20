"""Offline tests: `python3 -m unittest discover -s tests` from the repository root."""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run from a checkout without installing

from mwpipe import tools  # noqa: E402
from mwpipe.collection import Collection, normalize_source  # noqa: E402
from mwpipe.fomod import FomodInstaller  # noqa: E402
from mwpipe.install import find_data_roots, install_from_hashes  # noqa: E402
from mwpipe.util import PipelineError, save_json  # noqa: E402
from mwpipe.nexus import ApiError, CollectionRef, NexusClient, encode_url, parse_collection_ref  # noqa: E402
from mwpipe.openmw_cfg import OpenMWConfig, import_ini_fallbacks, quote, unquote  # noqa: E402
from mwpipe.pipeline import Pipeline, _drop_missing_masters, _needs_lua_records, collection_dir_name, plugin_masters  # noqa: E402
from mwpipe.settings import Settings, default_dirs  # noqa: E402


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestNexusRefs(unittest.TestCase):
    def test_collection_refs(self):
        self.assertEqual(parse_collection_ref("https://www.nexusmods.com/games/morrowind/collections/8euesh"),
                         CollectionRef("morrowind", "8euesh", None))
        self.assertEqual(parse_collection_ref("nxm://morrowind/collections/8euesh/revisions/1"),
                         CollectionRef("morrowind", "8euesh", 1))
        self.assertEqual(parse_collection_ref("https://next.nexusmods.com/morrowind/collections/8euesh?tab=mods"),
                         CollectionRef("morrowind", "8euesh", None))
        self.assertEqual(parse_collection_ref("8euesh@3"), CollectionRef("morrowind", "8euesh", 3))


class TestConfig(TempDir):
    def test_quote_roundtrip(self):
        for value in ["/Users/x/Library/Application Support/openmw", 'odd"&name']:
            self.assertEqual(unquote(quote(value)), value)

    def test_preserves_unmanaged_lines(self):
        cfg_path = touch(self.tmp / "openmw.cfg", b"encoding=win1252\nfallback=A_B,1\n"
                         b'data="/old"\ncontent=Old.esp\nno-sound=0\n')
        cfg = OpenMWConfig.load(cfg_path)
        cfg.data, cfg.content = [Path("/new dir")], ["Morrowind.esm"]
        cfg.save(cfg_path)
        text = cfg_path.read_text()
        self.assertIn("fallback=A_B,1", text)
        self.assertIn("no-sound=0", text)
        self.assertIn('data="/new dir"', text)
        self.assertNotIn("Old.esp", text)
        self.assertEqual(OpenMWConfig.load(cfg_path).render(), text)  # stable on reload

    def test_ini_import(self):
        ini = touch(self.tmp / "Morrowind.ini", b"[Weather Thunderstorm]\nThunder Sound ID 0=Thunder0\n"
                    b"[General]\nWerewolf FOV=100 ; comment\nMax FPS=240\n[Level Up]\nLevel2=Hi\n")
        pairs = dict(import_ini_fallbacks(ini))
        self.assertEqual(pairs["Weather_Thunderstorm_Thunder_Sound_ID_0"], "Thunder0")
        self.assertEqual(pairs["General_Werewolf_FOV"], "100 ")
        self.assertEqual(pairs["Level_Up_Level2"], "Hi")
        self.assertNotIn("General_Max_FPS", pairs)


class TestInstall(TempDir):
    def test_data_roots(self):
        touch(self.tmp / "Mod v1/00 Core/meshes/a.nif")
        touch(self.tmp / "Mod v1/01 Patch/Patch.esp")
        touch(self.tmp / "Mod v1/readme.txt")
        roots = [r.name for r in find_data_roots(self.tmp)]
        self.assertEqual(roots, ["00 Core", "01 Patch"])

    def test_data_files_folder(self):
        touch(self.tmp / "Mod/Data Files/textures/t.dds")
        self.assertEqual([r.name for r in find_data_roots(self.tmp)], ["Data Files"])

    def test_hashes(self):
        touch(self.tmp / "arc/Mod/Data Files/Meshes/A.nif")
        touch(self.tmp / "arc/Mod/Data Files/Extra.esp")
        n = install_from_hashes(self.tmp / "arc", [{"path": "meshes\\a.nif"}], self.tmp / "out")
        self.assertEqual(n, 1)
        self.assertTrue((self.tmp / "out/meshes/a.nif").exists())
        self.assertFalse((self.tmp / "out/Extra.esp").exists())

    def test_fomod_choices(self):
        root = self.tmp / "arc"
        touch(root / "core/Core.esp")
        touch(root / "opt_a/A.esp")
        touch(root / "opt_b/B.esp")
        touch(root / "flag/F.esp")
        touch(root / "fomod/ModuleConfig.xml", b"""<?xml version="1.0"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <requiredInstallFiles><folder source="core" destination=""/></requiredInstallFiles>
 <installSteps><installStep name="Main"><optionalFileGroups>
  <group name="Pick" type="SelectExactlyOne"><plugins>
   <plugin name="A"><files><folder source="opt_a" destination=""/></files>
     <typeDescriptor><type name="Recommended"/></typeDescriptor></plugin>
   <plugin name="B"><files><folder source="opt_b" destination=""/></files>
     <conditionFlags><flag name="b">On</flag></conditionFlags>
     <typeDescriptor><type name="Optional"/></typeDescriptor></plugin>
  </plugins></group></optionalFileGroups></installStep></installSteps>
 <conditionalFileInstalls><patterns><pattern>
  <dependencies><flagDependency flag="b" value="On"/></dependencies>
  <files><file source="FLAG\\F.esp" destination="F.esp"/></files></pattern></patterns></conditionalFileInstalls>
</config>""")
        choices = [{"name": "Main", "groups": [{"name": "Pick", "choices": [{"name": "B", "idx": 1}]}]}]
        FomodInstaller(root / "fomod/ModuleConfig.xml", choices).install(self.tmp / "out")
        self.assertEqual(sorted(p.name for p in (self.tmp / "out").iterdir()), ["B.esp", "Core.esp", "F.esp"])
        FomodInstaller(root / "fomod/ModuleConfig.xml", None).install(self.tmp / "defaults")
        self.assertEqual(sorted(p.name for p in (self.tmp / "defaults").iterdir()), ["A.esp", "Core.esp"])


class TestOrdering(TempDir):
    def test_rules(self):
        path = self.tmp / "collection.json"
        path.write_text(json.dumps({"mods": [
            {"name": "A", "source": {"type": "nexus", "modId": 1, "fileId": 10}},
            {"name": "B", "source": {"type": "nexus", "modId": 2, "fileId": 20}},
            {"name": "C", "source": {"type": "nexus", "modId": 3, "fileId": 30}},
        ], "modRules": [
            {"type": "before", "source": {"repo": {"fileId": "30"}}, "reference": {"repo": {"fileId": "10"}}},
        ]}))
        order = [m.name for m in Collection.load(path, {}, False).data_order()]
        self.assertLess(order.index("C"), order.index("A"))  # C must load before A
        self.assertEqual(sorted(order), ["A", "B", "C"])


class TestPipeline(TempDir):
    """End to end from a collection.json + archives to openmw.cfg, then post-processing."""

    def make_settings(self):
        args = SimpleNamespace(data_dir=self.tmp / "data", openmw_cfg=self.tmp / "cfg/openmw.cfg",
                               morrowind_data=None, mods_dir=None, downloads_dir=None,
                               generated_dir=None, cache_dir=self.tmp / "cache", state_dir=self.tmp / "state",
                               config_dir=self.tmp / "config", dry_run=False)
        return Settings.from_args(args)

    def test_full_offline(self):
        s = self.make_settings()
        s.ensure_dirs()
        for name in ("Morrowind.esm", "Tribunal.esm", "Morrowind.bsa"):
            touch(s.morrowind_data / name)
        touch(s.morrowind_data.parent / "Morrowind.ini", b"[General]\nWerewolf FOV=100\n")
        touch(s.openmw_cfg.parent / "settings.cfg", b"[Groundcover]\nenabled = true\n")

        archive = s.downloads_dir / "ModA.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("ModA/Data Files/ModA.esp", "a")
            zf.writestr("ModA/Data Files/ModA.bsa", "b")
            zf.writestr("ModA/Data Files/scripts.omwscripts", "c")
        from mwpipe.util import md5sum
        coll_dir = s.state_dir / "collections/test-1"
        touch(coll_dir / "collection.json", json.dumps({
            "info": {"name": "Test"},
            "mods": [{"name": "Mod A", "source": {"type": "nexus", "modId": 1, "fileId": 2,
                                                   "md5": md5sum(archive)}}],
            "loadOrder": [{"id": "morrowind.esm", "name": "Morrowind.esm", "enabled": True},
                          {"id": "moda.esp", "name": "ModA.esp", "enabled": True},
                          {"id": "scripts.omwscripts", "name": "scripts.omwscripts", "enabled": False}],
        }).encode())
        (s.state_dir / "current.json").write_text(json.dumps(
            {"slug": "test", "game": "morrowind", "revision": 1, "name": "Test", "dir": str(coll_dir)}))

        pipe = Pipeline(s)
        pipe.run(["download", "install", "configure"], None)
        cfg = OpenMWConfig.load(s.openmw_cfg)
        # the collection lists scripts.omwscripts as disabled, so it is not appended like other Lua plugins
        self.assertEqual(cfg.content, ["Morrowind.esm", "Tribunal.esm", "ModA.esp"])
        self.assertEqual(cfg.archives, ["Morrowind.bsa", "ModA.bsa"])
        self.assertEqual(cfg.data[0], s.morrowind_data)
        self.assertTrue(any(d.is_relative_to(s.mods_dir / "test") for d in cfg.data), "mods go under the name slug")
        self.assertEqual(cfg.data[-1], s.generated_dir)
        self.assertEqual(cfg.fallbacks, {"General_Werewolf_FOV": "100"})

        # Post-processing with fake tools that create the expected outputs.
        def fake_run(cmd, cfg_path, cwd, dry_run=False):
            name = Path(cmd[0]).name
            outputs = {"delta_plugin": [tools.DELTA_OUTPUT],
                       "gocoverify": [tools.GROUNDCOVER_DELETED, tools.GROUNDCOVER_PLUGIN],
                       "lumenaire": [tools.LIGHTS_OUTPUT]}.get(name, [])
            calls.append(name)
            for out in outputs:
                touch(s.generated_dir / out)
            if name == "plox":  # simulate plox: rewrite content lines in a new order
                text = cfg_path.read_text().splitlines()
                content = [l for l in text if l.startswith("content=")]
                rest = [l for l in text if not l.startswith("content=")]
                cfg_path.write_text("\n".join(rest + content[::-1]) + "\n")
        calls = []
        post = ["sort", "delta", "groundcover", "lightfix"]
        with mock.patch.object(tools, "run", side_effect=fake_run):
            pipe.run(post, None)
            pipe.run(post, None)  # idempotent re-run
        self.assertNotIn("plox", calls)  # the collection's load order is trusted by default
        self.assertEqual(calls.count("lumenaire"), 2)
        self.assertFalse((s.config_dir / "lumenaire.toml").exists(), "config is never written")
        cfg = OpenMWConfig.load(s.openmw_cfg)
        self.assertEqual(cfg.content, ["Morrowind.esm", "Tribunal.esm", "ModA.esp",
                                       tools.DELTA_OUTPUT, tools.GROUNDCOVER_DELETED, tools.LIGHTS_OUTPUT])
        self.assertEqual(cfg.groundcover, [tools.GROUNDCOVER_PLUGIN])

        # --sort plox: plox runs (here it reverses the order) and generated plugins stay last.
        with mock.patch.object(tools, "run", side_effect=fake_run):
            Pipeline(s, sort_mode="plox").run(post, None)
        self.assertIn("plox", calls)
        cfg = OpenMWConfig.load(s.openmw_cfg)
        self.assertEqual(cfg.content[0], "ModA.esp")
        self.assertEqual(cfg.content[-3:], [tools.DELTA_OUTPUT, tools.GROUNDCOVER_DELETED, tools.LIGHTS_OUTPUT])


class TestCollectionDirName(unittest.TestCase):
    def test_slugified_name(self):
        self.assertEqual(collection_dir_name("I Heart Vanilla: Director's Cut"), "i-heart-vanilla-director-s-cut")
        self.assertEqual(collection_dir_name("!!!"), "collection")


def tes3(*masters: str) -> bytes:
    import struct
    subs = b"".join(b"MAST" + struct.pack("<I", len(m) + 1) + m.encode() + b"\0"
                    + b"DATA" + struct.pack("<I", 8) + bytes(8) for m in masters)
    return b"TES3" + struct.pack("<III", len(subs), 0, 0) + subs


class TestMasters(TempDir):
    def test_masters_read_and_missing_ones_dropped_transitively(self):
        plugins = {n: touch(self.tmp / n, data) for n, data in {
            "Morrowind.esm": tes3(), "A.esp": tes3("Morrowind.esm"),
            "B.esp": tes3("Morrowind.esm", "Absent.esm"), "C.esp": tes3("B.esp"), "junk.esp": b"x"}.items()}
        self.assertEqual(plugin_masters(plugins["B.esp"]), ["Morrowind.esm", "Absent.esm"])
        self.assertEqual(plugin_masters(plugins["junk.esp"]), [])
        available = {n.lower(): p for n, p in plugins.items()}
        self.assertEqual(_drop_missing_masters(list(plugins), available), ["Morrowind.esm", "A.esp", "junk.esp"])


    def test_lua_records_found_through_masters(self):
        lua = touch(self.tmp / "lua.esp", tes3("Morrowind.esm") + b"LUAL")
        dep = touch(self.tmp / "dep.esp", tes3("lua.esp"))
        other = touch(self.tmp / "other.esp", tes3("Morrowind.esm"))
        available = {"lua.esp": lua, "dep.esp": dep, "other.esp": other}
        self.assertEqual([_needs_lua_records(p, available) for p in (lua, dep, other)], [True, True, False])


class TestPloxRules(TempDir):
    def settings(self):
        args = SimpleNamespace(data_dir=self.tmp / "data", openmw_cfg=self.tmp / "cfg/openmw.cfg",
                               morrowind_data=None, mods_dir=None, downloads_dir=None,
                               generated_dir=None, cache_dir=self.tmp / "cache", state_dir=self.tmp / "state",
                               config_dir=self.tmp / "config", dry_run=False)
        return Settings.from_args(args)

    def test_rules_file_written_and_removed(self):
        s = self.settings()
        s.openmw_cfg.parent.mkdir(parents=True)
        with mock.patch.object(tools, "run"):
            tools.plox_sort(s.openmw_cfg, self.tmp / "work", False, ["[Order]\na.esm\nb.esm"])
            rules = self.tmp / "work/mlox" / tools.PLOX_USER_RULES
            self.assertEqual(rules.read_text(), "[Order]\na.esm\nb.esm\n")
            tools.plox_sort(s.openmw_cfg, self.tmp / "work", False)
        self.assertFalse(rules.exists())

    def test_sort_runs_plox_when_rules_set(self):
        s = self.settings()
        s.ensure_dirs()
        s.overrides_file.parent.mkdir(parents=True, exist_ok=True)
        s.overrides_file.write_text(json.dumps({"plox_rules": ["[Order]\na.esm\nb.esm"]}))
        touch(s.state_dir / "collections/c/collection.json", json.dumps(
            {"info": {"name": "C"}, "mods": [], "loadOrder": [{"id": "a.esm", "name": "a.esm", "enabled": True}]}).encode())
        (s.state_dir / "current.json").write_text(json.dumps(
            {"slug": "c", "game": "morrowind", "revision": 1, "name": "C", "dir": "collections/c"}))
        calls = []
        with mock.patch.object(tools, "plox_sort", side_effect=lambda *a, **k: calls.append(a)):
            pipe = Pipeline(s)
            pipe.save_cfg(pipe.load_cfg())
            pipe.sort()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], ["[Order]\na.esm\nb.esm"])

    def test_collection_order_before_base_master_is_kept(self):
        s = self.settings()
        for name in ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"):
            touch(s.morrowind_data / name)
        mod = s.mods_dir / "c" / "001 Seafloor"
        touch(mod / "Distant_Seafloor_2.00.esm")
        order = ["Morrowind.esm", "Tribunal.esm", "Distant_Seafloor_2.00.esm", "Bloodmoon.esm"]
        touch(s.state_dir / "collections/c/collection.json", json.dumps({
            "info": {"name": "C"}, "mods": [],
            "loadOrder": [{"id": n.lower(), "name": n, "enabled": True} for n in order]}).encode())
        (s.state_dir / "current.json").write_text(json.dumps(
            {"slug": "c", "game": "morrowind", "revision": 1, "name": "C", "dir": "collections/c"}))
        s.ensure_dirs()
        save_json(s.state_dir / "manifest.json", {"_order": [str(mod)]})
        pipe = Pipeline(s)
        pipe.configure()
        self.assertEqual(OpenMWConfig.load(s.openmw_cfg).content, order)


class TestNexusClient(unittest.TestCase):
    def client(self, responses):
        c = NexusClient("key")
        c._api = mock.Mock(side_effect=responses)
        return c

    def test_v3_download_link(self):
        c = self.client([{"data": {"links": [{"url": "https://cdn/a"}, {"url": "https://cdn/b"}]}}])
        self.assertEqual(c.file_url("morrowind", 1, 2), "https://cdn/a")
        self.assertIn("/v3/games/morrowind/mod-file-versions/2/download-link", c._api.call_args[0][0])

    def test_falls_back_to_v1_on_404(self):
        c = self.client([ApiError("gone", 404), [{"URI": "https://cdn/v1"}]])
        self.assertEqual(c.file_url("morrowind", 1, 2), "https://cdn/v1")
        self.assertIn("/v1/games/morrowind/mods/1/files/2/download_link.json", c._api.call_args[0][0])

    def test_permission_errors_are_not_retried_on_v1(self):
        c = self.client([ApiError("forbidden", 403)])
        with self.assertRaises(ApiError):
            c.file_url("morrowind", 1, 2)
        self.assertEqual(c._api.call_count, 1)

    def test_refuses_foreign_hosts(self):
        with self.assertRaises(PipelineError):
            NexusClient("key")._api("https://evil.example/x")


class TestConfigFiles(TempDir):
    def lightfix_settings(self, create_config):
        from mwpipe.pipeline import LUMENAIRE_DEFAULTS
        args = SimpleNamespace(data_dir=self.tmp / "data", openmw_cfg=self.tmp / "cfg/openmw.cfg",
                               morrowind_data=None, mods_dir=None, downloads_dir=None, generated_dir=None,
                               cache_dir=self.tmp / "cache", state_dir=self.tmp / "state",
                               config_dir=self.tmp / "config", dry_run=False)
        s = Settings.from_args(args)
        mine = touch(s.config_dir / "lumenaire.toml", b"# mine") if create_config else None
        pipe = Pipeline(s)
        with mock.patch.object(tools, "lumenaire") as lum, mock.patch.object(Pipeline, "_register"):
            pipe.lightfix()
        return lum.call_args[0][2], mine, LUMENAIRE_DEFAULTS

    def test_user_config_wins(self):
        used, mine, _ = self.lightfix_settings(True)
        self.assertEqual(used, mine)

    def test_bundled_defaults_without_config(self):
        used, _, defaults = self.lightfix_settings(False)
        self.assertEqual(used, defaults)

    def test_shipped_config_matches_bundled_defaults(self):
        """./config/lumenaire.toml (mounted by compose.yaml) is a copy of the packaged defaults."""
        from mwpipe.pipeline import LUMENAIRE_DEFAULTS
        shipped = Path(__file__).resolve().parents[1] / "config"
        self.assertEqual((shipped / "lumenaire.toml").read_bytes(), LUMENAIRE_DEFAULTS.read_bytes())
        self.assertEqual(json.loads((shipped / "overrides.json").read_text()), {})


class TestDirs(TempDir):
    def test_linux_defaults_follow_xdg(self):
        env = {"XDG_CACHE_HOME": "/xdg/cache", "XDG_STATE_HOME": "relative/ignored", "XDG_CONFIG_HOME": ""}
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict("os.environ", env), \
                mock.patch("pathlib.Path.home", return_value=Path("/home/u")):
            self.assertEqual(default_dirs(), (Path("/xdg/cache/mwpipe"), Path("/home/u/.local/state/mwpipe"),
                                              Path("/home/u/.config/mwpipe")))

    def test_macos_defaults(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch("pathlib.Path.home", return_value=Path("/Users/u")):
            cache, state, config = default_dirs()
        self.assertEqual(cache, Path("/Users/u/Library/Caches/mwpipe"))
        self.assertEqual(state, config)
        self.assertEqual(state, Path("/Users/u/Library/Application Support/mwpipe"))

    def test_openmw_and_mwpipe_files_are_separate(self):
        args = SimpleNamespace(data_dir=self.tmp / "data", openmw_cfg=self.tmp / "cfg/openmw.cfg",
                               morrowind_data=None, mods_dir=None, downloads_dir=None, generated_dir=None,
                               cache_dir=self.tmp / "cache", state_dir=self.tmp / "state",
                               config_dir=self.tmp / "config", dry_run=False)
        s = Settings.from_args(args)
        self.assertEqual(s.downloads_dir, self.tmp / "cache/downloads")
        self.assertEqual(s.overrides_file, self.tmp / "config/overrides.json")
        self.assertEqual((s.mods_dir, s.generated_dir), (self.tmp / "data/ModData", self.tmp / "data/Generated"))
        for path in (s.downloads_dir, s.state_dir, s.config_dir, s.cache_dir):
            self.assertFalse(path.is_relative_to(s.data_dir))


class TestDownloadUrl(unittest.TestCase):
    def test_spaces_are_encoded(self):
        url = "https://cdn.example/cdn/100/43931/Unofficial Morrowind-43931-3-3-0.7z?expires=1&md5=a_b-c&user_id=2"
        self.assertEqual(encode_url(url), "https://cdn.example/cdn/100/43931/Unofficial%20Morrowind-43931-3-3-0.7z"
                                          "?expires=1&md5=a_b-c&user_id=2")

    def test_already_encoded_is_unchanged(self):
        url = "https://cdn.example/a%20b/c.7z?x=1%2B2"
        self.assertEqual(encode_url(url), url)

    def test_network_errors_become_pipeline_errors(self):
        import urllib.error
        from mwpipe.nexus import download
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            with self.assertRaises(PipelineError):
                download("https://cdn.example/x.7z", Path(tmp, "x.7z"))


class TestManifestKeys(unittest.TestCase):
    def test_snake_case_source(self):
        self.assertEqual(normalize_source({"mod_id": 1, "file_id": 2, "md5": "x", "logicalFilename": "a"}),
                         {"modId": 1, "fileId": 2, "md5": "x", "logicalFilename": "a"})


if __name__ == "__main__":
    unittest.main()
