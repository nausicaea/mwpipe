"""Tests for install, FOMOD internals, tes3cmd cleaning, pipeline helpers and the texture report."""

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run from a checkout without installing

from mwpipe import pipeline, tools  # noqa: E402
from mwpipe.collection import Mod  # noqa: E402
from mwpipe.fomod import FomodInstaller  # noqa: E402
from mwpipe.install import (_move_roots, find_data_roots, install_mod, looks_like_data_root,  # noqa: E402
                            prune_to_plugins)
from mwpipe.openmw_cfg import OpenMWConfig  # noqa: E402
from mwpipe.settings import Settings  # noqa: E402
from mwpipe.textures import MissingTextures, report  # noqa: E402
from mwpipe.util import PipelineError  # noqa: E402


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


def make_zip(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return path


class TestDataRootDetection(TempDir):
    def test_looks_like_data_root(self):
        touch(self.tmp / "with_plugin/A.esp")
        touch(self.tmp / "with_bsa/A.bsa")
        touch(self.tmp / "with_meshes/Meshes/a.nif")
        touch(self.tmp / "other/readme.txt")
        (self.tmp / "Data Files").mkdir()
        self.assertTrue(looks_like_data_root(self.tmp / "Data Files"))
        for name in ("with_plugin", "with_bsa", "with_meshes"):
            self.assertTrue(looks_like_data_root(self.tmp / name), name)
        self.assertFalse(looks_like_data_root(self.tmp / "other"))

    def test_only_the_shallowest_level_and_no_ignored_folders(self):
        touch(self.tmp / "a/Data Files/A.esp")
        touch(self.tmp / "a/deeper/Data Files/B.esp")
        touch(self.tmp / "fomod/C.esp")
        touch(self.tmp / "docs/D.esp")
        self.assertEqual(find_data_roots(self.tmp), [self.tmp / "a/Data Files"])

    def test_move_roots_one_or_many(self):
        touch(self.tmp / "src/one/A.esp")
        self.assertEqual(_move_roots([self.tmp / "src/one"], self.tmp / "out/m"), [self.tmp / "out/m"])
        self.assertTrue((self.tmp / "out/m/A.esp").exists())
        touch(self.tmp / "src/x/X.esp")
        touch(self.tmp / "src/y/Y.esp")
        moved = _move_roots([self.tmp / "src/x", self.tmp / "src/y"], self.tmp / "out/n")
        self.assertEqual([p.name for p in moved], ["00 x", "01 y"])
        self.assertTrue((self.tmp / "out/n/01 y/Y.esp").exists())


class TestInstallMod(TempDir):
    def install(self, members, mod=None, hints=None):
        mod = mod or Mod(index=0, name="Mod", source={})
        archive = make_zip(self.tmp / "m.zip", members)
        with mock.patch("mwpipe.archives.shutil.which", return_value=None):
            return install_mod(mod, archive, self.tmp / "mods/m", self.tmp / "scratch", hints)

    def test_heuristic_single_data_folder(self):
        roots = self.install({"Mod/Data Files/A.esp": "a", "Mod/readme.txt": "r"})
        self.assertEqual(roots, [self.tmp / "mods/m"])
        self.assertTrue((self.tmp / "mods/m/A.esp").exists())
        self.assertFalse((self.tmp / "scratch/extract").exists())

    def test_several_candidates_are_all_installed_and_hinted(self):
        hints: list[str] = []
        with self.assertLogs("mwpipe", "WARNING") as logs:
            roots = self.install({"00 Core/A.esp": "a", "01 Alt/B.esp": "b"}, hints=hints)
        self.assertEqual([r.name for r in roots], ["00 00 Core", "01 01 Alt"])
        self.assertTrue(any("2 candidate data folders" in line for line in logs.output))
        self.assertIn("[x] 00 Core", "\n".join(hints))

    def test_no_layout_uses_the_archive_root(self):
        with self.assertLogs("mwpipe", "WARNING") as logs:
            roots = self.install({"notes.txt": "n"})
        self.assertEqual(roots, [self.tmp / "mods/m"])
        self.assertTrue(any("no Data Files layout" in line for line in logs.output))

    def test_override_data_picks_folders_ignoring_case(self):
        mod = Mod(index=0, name="Mod", source={}, overrides={"data": ["02 patch"]})
        roots = self.install({"00 Core/A.esp": "a", "02 Patch/P.esp": "p"}, mod=mod)
        self.assertEqual(roots, [self.tmp / "mods/m"])
        self.assertTrue((self.tmp / "mods/m/P.esp").exists())
        self.assertFalse((self.tmp / "mods/m/A.esp").exists())

    def test_override_with_missing_folder_is_an_error(self):
        mod = Mod(index=0, name="Mod", source={}, overrides={"data": ["Nope"]})
        with self.assertRaises(PipelineError) as ctx:
            self.install({"00 Core/A.esp": "a"}, mod=mod)
        self.assertIn("'Nope'", str(ctx.exception))
        self.assertFalse((self.tmp / "scratch/extract").exists())

    def test_replicate_mode_copies_only_listed_files(self):
        mod = Mod(index=0, name="Mod", source={}, hashes=[{"path": "Data Files\\A.esp"}])
        self.install({"x/Data Files/A.esp": "a", "x/Data Files/B.esp": "b"}, mod=mod)
        self.assertTrue((self.tmp / "mods/m/Data Files/A.esp").exists())
        self.assertFalse((self.tmp / "mods/m/Data Files/B.esp").exists())

    def test_existing_mod_dir_is_replaced(self):
        touch(self.tmp / "mods/m/old.esp")
        self.install({"Data Files/A.esp": "a"})
        self.assertFalse((self.tmp / "mods/m/old.esp").exists())

    def test_prune_keeps_only_plugins_in_each_folder(self):
        touch(self.tmp / "d/A.ESP")
        touch(self.tmp / "d/b.omwaddon")
        touch(self.tmp / "d/readme.txt")
        touch(self.tmp / "d/meshes/a.nif")
        prune_to_plugins([self.tmp / "d", self.tmp / "missing"])
        self.assertEqual(sorted(p.name for p in (self.tmp / "d").iterdir()), ["A.ESP", "b.omwaddon"])


class TestFomodInternals(TempDir):
    def installer(self, xml: str = "<config/>") -> FomodInstaller:
        touch(self.tmp / "fomod/ModuleConfig.xml", xml.encode())
        return FomodInstaller(self.tmp / "fomod/ModuleConfig.xml", None)

    def test_flag_dependencies_with_and_or(self):
        inst = self.installer()
        inst.flags = {"a": "On"}
        both = ET.fromstring('<dependencies><flagDependency flag="a" value="On"/><flagDependency flag="b" value="On"/></dependencies>')
        either = ET.fromstring('<dependencies operator="Or"><flagDependency flag="a" value="On"/><flagDependency flag="b" value="On"/></dependencies>')
        self.assertFalse(inst._deps_met(both))
        self.assertTrue(inst._deps_met(either))
        self.assertTrue(inst._deps_met(None))

    def test_plugin_type_plain_conditional_and_missing(self):
        inst = self.installer()
        plain = ET.fromstring('<plugin><typeDescriptor><type name="Required"/></typeDescriptor></plugin>')
        none = ET.fromstring("<plugin/>")
        cond = ET.fromstring(
            '<plugin><typeDescriptor><dependencyType><defaultType name="Optional"/><patterns><pattern>'
            '<dependencies><flagDependency flag="x" value="1"/></dependencies><type name="Recommended"/>'
            "</pattern></patterns></dependencyType></typeDescriptor></plugin>")
        self.assertEqual(inst._plugin_type(plain), "Required")
        self.assertEqual(inst._plugin_type(none), "Optional")
        self.assertEqual(inst._plugin_type(cond), "Optional")
        inst.flags = {"x": "1"}
        self.assertEqual(inst._plugin_type(cond), "Recommended")

    def test_plan_lists_required_files(self):
        touch(self.tmp / "core/Core.esp")
        inst = self.installer('<config><requiredInstallFiles><folder source="core" destination=""/>'
                              "</requiredInstallFiles></config>")
        plan = inst.plan()
        self.assertEqual([i.source for i in plan], ["core"])

    def test_empty_installer_is_an_error(self):
        with self.assertRaises(PipelineError):
            self.installer().install(self.tmp / "out")


class TestTes3cmdClean(TempDir):
    def test_changed_plugin_is_backed_up_and_replaced(self):
        changed_plugin = touch(self.tmp / "mods/Changed.esp", b"old")
        same_plugin = touch(self.tmp / "mods/Same.esp", b"same")
        master = touch(self.tmp / "mods/Master.esm", b"m")

        def fake_run(cmd, cfg, cwd, dry_run):
            if cmd[-1] == "Changed.esp":
                (Path(cwd) / "Changed.esp").write_bytes(b"cleaned")

        with mock.patch.object(tools, "run", side_effect=fake_run) as run:
            result = tools.tes3cmd_clean([changed_plugin, same_plugin], [changed_plugin, same_plugin, master],
                                         self.tmp / "ws", self.tmp / "backup", False)
        self.assertEqual(result, [changed_plugin])
        self.assertEqual(changed_plugin.read_bytes(), b"cleaned")
        self.assertEqual((self.tmp / "backup/Changed.esp").read_bytes(), b"old")
        self.assertFalse((self.tmp / "backup/Same.esp").exists())
        self.assertEqual(same_plugin.read_bytes(), b"same")
        self.assertEqual(run.call_count, 2)
        self.assertFalse((self.tmp / "ws/Data Files").exists())
        self.assertTrue((self.tmp / "ws/Morrowind.ini").exists())

    def test_dry_run_changes_nothing(self):
        plugin = touch(self.tmp / "mods/A.esp", b"old")
        with mock.patch.object(tools, "run"):
            result = tools.tes3cmd_clean([plugin], [plugin], self.tmp / "ws", self.tmp / "backup", True)
        self.assertEqual(result, [])
        self.assertEqual(plugin.read_bytes(), b"old")
        self.assertFalse((self.tmp / "backup").exists())


class TestPipelineHelpers(TempDir):
    def make_pipeline(self, dry_run=False) -> pipeline.Pipeline:
        args = SimpleNamespace(data_dir=self.tmp / "data", openmw_cfg=self.tmp / "cfg/openmw.cfg",
                               morrowind_data=None, mods_dir=None, downloads_dir=None,
                               generated_dir=None, cache_dir=self.tmp / "cache", state_dir=self.tmp / "state",
                               config_dir=self.tmp / "config", dry_run=dry_run)
        settings = Settings.from_args(args)
        settings.ensure_dirs()
        return pipeline.Pipeline(settings)

    def test_plugins_by_name_later_directory_wins(self):
        first = touch(self.tmp / "a/Same.esp")
        second = touch(self.tmp / "b/same.ESP")
        touch(self.tmp / "a/readme.txt")
        found = pipeline._plugins_by_name([self.tmp / "a", self.tmp / "b", self.tmp / "missing"])
        self.assertEqual(found, {"same.esp": second})
        self.assertNotEqual(found["same.esp"], first)

    def test_strip_generated_removes_only_generated_plugins(self):
        pipe = self.make_pipeline()
        cfg = OpenMWConfig()
        cfg.content = ["Morrowind.esm", tools.DELTA_OUTPUT.upper(), tools.LIGHTS_OUTPUT, "Mod.esp"]
        cfg.groundcover = [tools.GROUNDCOVER_PLUGIN, "Mine.esp"]
        pipe.save_cfg(cfg)
        pipe.strip_generated()
        result = pipe.load_cfg()
        self.assertEqual(result.content, ["Morrowind.esm", "Mod.esp"])
        self.assertEqual(result.groundcover, ["Mine.esp"])
        self.assertIn(pipe.s.generated_dir, result.data)

    def test_clean_skips_lua_plugins_and_files_outside_moddata(self):
        pipe = self.make_pipeline()
        mod = pipe.s.mods_dir / "coll/mod"
        plain = touch(mod / "Plain.esp")
        lua = touch(mod / "Lua.esp")
        esm = touch(mod / "Big.esm")
        outside = touch(self.tmp / "base/Base.esp")
        cfg = OpenMWConfig()
        cfg.data = [mod, outside.parent]
        cfg.content = ["Plain.esp", "Lua.esp", "Big.esm", "Base.esp", "Unknown.esp"]
        pipe.save_cfg(cfg)
        with mock.patch.object(pipeline, "_needs_lua_records", side_effect=lambda p, a: p == lua), \
                mock.patch.object(pipeline.tools, "tes3cmd_clean", return_value=[plain]) as clean:
            with self.assertLogs("mwpipe", "INFO") as logs:
                pipe.clean()
        self.assertEqual(clean.call_args[0][0], [plain])
        self.assertEqual(clean.call_args[0][1], [plain, lua, esm, outside])
        self.assertTrue(any("not cleaning Lua.esp" in line for line in logs.output))
        self.assertTrue(any("cleaned 1 of 1" in line for line in logs.output))

    def test_register_requires_existing_generated_files(self):
        pipe = self.make_pipeline()
        with self.assertRaises(PipelineError):
            pipe._register(["Missing.omwaddon"])
        touch(pipe.s.generated_dir / "Made.omwaddon")
        pipe._register(["Made.omwaddon"])
        self.assertEqual(pipe.load_cfg().content, ["Made.omwaddon"])

    def test_register_in_dry_run_skips_the_file_check(self):
        pipe = self.make_pipeline(dry_run=True)
        with self.assertLogs("mwpipe", "INFO"):
            pipe._register(["Missing.omwaddon"])
        self.assertFalse(pipe.s.openmw_cfg.exists())


class TestTextureReport(TempDir):
    def test_nothing_missing(self):
        with self.assertLogs("mwpipe", "INFO") as logs:
            report(MissingTextures(meshes=4))
        self.assertIn("4 scanned meshes", logs.output[0])

    def test_missing_textures_are_grouped_by_mod_and_written_to_a_file(self):
        result = MissingTextures(meshes=2, missing={
            "tx_a": [("Mod A", "m/a.nif")], "tx_b": [("Mod A", "m/b.nif")],
            "tx_c": [("Mod B", "m/c.nif")], "tx_d": [("Mod A", "m/d.nif")], "tx_e": [("Mod A", "m/e.nif")]})
        out = self.tmp / "state/missing.txt"
        with self.assertLogs("mwpipe", "WARNING") as logs:
            report(result, out, examples=2)
        text = "\n".join(logs.output)
        self.assertIn("Mod A: 4 missing", text)
        self.assertIn(", ...", text)
        self.assertLess(text.index("Mod A"), text.index("Mod B"))
        lines = out.read_text().splitlines()
        self.assertEqual(lines[0], "texture\tmod\tmesh")
        self.assertIn("tx_c\tMod B\tm/c.nif", lines)


if __name__ == "__main__":
    unittest.main()
