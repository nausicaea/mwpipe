"""Offline tests for `mwpipe build-collection`: mod list parsing, manifests and the whole flow."""

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run from a checkout without installing

from mwpipe import publish, tools  # noqa: E402
from mwpipe.collection import Collection  # noqa: E402
from mwpipe.modlist import parse_mod_list  # noqa: E402
from mwpipe.nexus import NexusClient  # noqa: E402
from mwpipe.settings import Settings  # noqa: E402
from mwpipe.util import PipelineError  # noqa: E402

LIST = """\
# my list
https://www.nexusmods.com/morrowind/mods/1            # primary file
https://www.nexusmods.com/morrowind/mods/2?tab=files&file_id=22
https://www.nexusmods.com/morrowind/mods/1
nxm://morrowind/mods/3/files/33?key=abc&expires=1
https://gitlab.com/modding-openmw/some-tool/-/package_files/99/download
"""


class TestModList(unittest.TestCase):
    def test_parse(self):
        entries = parse_mod_list(LIST).entries  # the repeated mod 1 line is dropped
        self.assertEqual([(e.game, e.mod_id, e.file_id) for e in entries],
                         [("morrowind", 1, None), ("morrowind", 2, 22), ("morrowind", 3, 33), (None, None, None)])
        self.assertFalse(entries[-1].is_nexus)

    def test_disable_lines(self):
        parsed = parse_mod_list("https://www.nexusmods.com/morrowind/mods/1\n- k_weather (louder sounds).esp  # either/or\n")
        self.assertEqual(parsed.disabled, ["k_weather (louder sounds).esp"])
        self.assertEqual(len(parsed.entries), 1)
        with self.assertRaises(PipelineError):
            parse_mod_list("https://www.nexusmods.com/morrowind/mods/1\n-\n")

    def test_order_rule_lines(self):
        parsed = parse_mod_list("https://www.nexusmods.com/morrowind/mods/1\n! Distant_Seafloor_2.00.esm < Bloodmoon.esm\n")
        self.assertEqual(parsed.rules, ["[Order]\nDistant_Seafloor_2.00.esm\nBloodmoon.esm"])
        with self.assertRaises(PipelineError):
            parse_mod_list("https://www.nexusmods.com/morrowind/mods/1\n! only-one.esp\n")

    def test_all_errors_reported(self):
        with self.assertRaises(PipelineError) as ctx:
            parse_mod_list("ftp://x/y\nhttps://www.nexusmods.com/morrowind/collections/abc\n")
        self.assertIn("line 1", str(ctx.exception))
        self.assertIn("line 2", str(ctx.exception))

    def test_one_game_only(self):
        with self.assertRaises(PipelineError):
            parse_mod_list("https://www.nexusmods.com/morrowind/mods/1\nhttps://www.nexusmods.com/skyrim/mods/2")

    def test_direct_name(self):
        self.assertEqual(publish.direct_name("https://gitlab.com/g/harvest-lights/-/package_files/5/download"),
                         "harvest-lights")
        self.assertEqual(publish.direct_name("https://example.org/files/Thing-1.2.zip"), "Thing-1.2")


class FakeClient(NexusClient):
    """No network: answers lookups from tables and records what would have been sent."""

    def __init__(self):
        super().__init__("key")
        self.sent = []
        self._user = {"name": "Tester", "user_id": 7, "is_premium": True}

    def mod(self, game, mod_id):
        return {"name": f"Mod {mod_id}", "version": "1.0", "author": "Someone", "adult_content": mod_id == 2,
                "main_file_version": {"game_scoped_id": str(mod_id * 10)} if mod_id != 4 else None}

    def file_version(self, game, file_id):
        return {"version": f"v{file_id}", "size_in_bytes": len(zip_bytes(f"{file_id // 10}-{file_id}"))}

    def file_url(self, game, mod_id, file_id):
        return f"https://cdn.example/{mod_id}-{file_id}.zip"

    def collection_name(self, game, collection_id):
        return None  # like a v3 API without a read endpoint

    def upload_file(self, path, filename):
        self.sent.append(("upload", filename, path.is_file()))
        return "11111111-1111-1111-1111-111111111111"

    def create_collection(self, upload_id, collection_data, collection_id=None):
        self.sent.append(("create", collection_id, collection_data))
        return {"id": "42", "slug": "abc123", "revision_number": 1, "revision_status": "draft"}


def zip_bytes(stem):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"Data Files/{stem}.esp", stem)
    return buf.getvalue()


def fake_download(url, dest, md5=None):
    """Every 'archive' is a zip with one plugin named after the URL."""
    dest.write_bytes(zip_bytes(Path(url).stem))
    return dest


class TestBuildCollection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        args = SimpleNamespace(data_dir=self.tmp / "data", openmw_cfg=self.tmp / "cfg/openmw.cfg",
                               morrowind_data=None, mods_dir=None, downloads_dir=None,
                               generated_dir=None, cache_dir=self.tmp / "cache", state_dir=self.tmp / "state",
                               config_dir=self.tmp / "config", dry_run=False)
        self.s = Settings.from_args(args)
        (self.s.morrowind_data).mkdir(parents=True)
        (self.s.morrowind_data / "Morrowind.esm").write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def run_build(self, text, downloads=None, **kwargs):
        def fake_plox(cmd, cfg_path, cwd, dry_run=False):  # reverse the plugin order, like a real re-sort
            lines = cfg_path.read_text().splitlines()
            content = [ln for ln in lines if ln.startswith("content=")]
            rest = [ln for ln in lines if not ln.startswith("content=")]
            base = [ln for ln in content if ln.lower().endswith(("morrowind.esm", "tribunal.esm", "bloodmoon.esm"))]
            cfg_path.write_text("\n".join(rest + base + [ln for ln in content if ln not in base][::-1]) + "\n")
        client = FakeClient()
        with mock.patch.object(publish, "download", side_effect=fake_download) as dl, \
                mock.patch.object(tools, "run", side_effect=fake_plox):
            result = publish.build_collection(self.s, client, text, **kwargs)
        if downloads is not None:
            downloads.extend(c.args[0] for c in dl.call_args_list)
        return client, result

    def test_collection_id_without_name_uses_recorded_name(self):
        text = "https://www.nexusmods.com/morrowind/mods/1\n"
        _, first = self.run_build(text, name="Vanilla Cut", summary="s")
        self.assertEqual(json.loads((self.s.state_dir / "collections.json").read_text())["42"]["name"], "Vanilla Cut")
        client, second = self.run_build(text, name=None, collection_id="42")
        self.assertEqual(client.sent[1][1], "42")
        self.assertIn("vanilla-cut", second["output_dir"])

    def test_collection_id_unknown_needs_name(self):
        with self.assertRaises(PipelineError):
            self.run_build("https://www.nexusmods.com/morrowind/mods/1\n", name=None, collection_id="999")

    def test_name_is_not_clobbered_by_disabled_plugins(self):
        text = "https://www.nexusmods.com/morrowind/mods/1\n- some.esp\n"
        client, _ = self.run_build(text, name="Real Name")
        self.assertEqual(client.sent[1][2]["collection_manifest"]["info"]["name"], "Real Name")

    def test_rerun_under_another_name_downloads_nothing(self):
        text = "https://www.nexusmods.com/morrowind/mods/1\n"
        first, second = [], []
        self.run_build(text, downloads=first, name="First Name", upload=False)
        self.run_build(text, downloads=second, name="Second Name", upload=False)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(list(self.s.downloads_dir.iterdir())), 1)

    def test_lost_index_adopts_files_by_name_and_size(self):
        text = "https://www.nexusmods.com/morrowind/mods/1\n"
        self.run_build(text, name="Some List", upload=False)
        (self.s.cache_dir / "archives.json").unlink()
        again = []
        self.run_build(text, downloads=again, name="Some List", upload=False)
        self.assertEqual(again, [])
        self.assertEqual(len(list(self.s.downloads_dir.iterdir())), 1)

    def test_direct_url_is_not_downloaded_twice(self):
        text = "https://gitlab.example/g/proj/-/package_files/77/download\n"
        first, second = [], []
        self.run_build(text, downloads=first, name="A List", upload=False)
        (self.s.cache_dir / "archives.json").unlink()
        self.run_build(text, downloads=second, name="B List", upload=False)
        self.assertEqual((len(first), second), (1, []))

    def test_dedupe_downloads(self):
        from mwpipe.pipeline import Pipeline
        self.s.ensure_dirs()
        d = self.s.downloads_dir
        (d / "Mod-1-0.7z").write_bytes(b"same")
        (d / "5-50-Mod-1-0.7z").write_bytes(b"same")
        (d / "other.7z").write_bytes(b"different")
        pipe = Pipeline(self.s)
        merged = pipe.dedupe_downloads()
        self.assertEqual([(a.name, b.name) for a, b in merged], [("5-50-Mod-1-0.7z", "Mod-1-0.7z")])
        self.assertEqual(sorted(f.name for f in d.iterdir()), ["Mod-1-0.7z", "other.7z"])

    def test_dedupe_dry_run_keeps_everything(self):
        import dataclasses
        from mwpipe.pipeline import Pipeline
        self.s.ensure_dirs()
        d = self.s.downloads_dir
        (d / "a.7z").write_bytes(b"same")
        (d / "bb.7z").write_bytes(b"same")
        Pipeline(dataclasses.replace(self.s, dry_run=True)).dedupe_downloads()
        self.assertEqual(len(list(d.iterdir())), 2)

    def test_fresh_download_identical_to_existing_is_merged(self):
        from mwpipe.pipeline import Pipeline
        self.s.ensure_dirs()
        d = self.s.downloads_dir
        (d / "Mod-1.7z").write_bytes(b"same")
        (d / "Mod-1-copy.7z").write_bytes(b"same")
        kept = Pipeline(self.s).dedupe_downloads(only=d / "Mod-1-copy.7z")
        self.assertEqual([k.name for _, k in kept], ["Mod-1.7z"])
        self.assertFalse((d / "Mod-1-copy.7z").exists())

    def test_end_to_end(self):
        text = "https://www.nexusmods.com/morrowind/mods/1\nhttps://www.nexusmods.com/morrowind/mods/2\n"
        client, result = self.run_build(text, name="My Test List", summary="s")
        self.assertEqual(result["url"], "https://www.nexusmods.com/games/morrowind/collections/abc123")
        self.assertEqual(result["status"], "draft")
        self.assertFalse(self.s.openmw_cfg.exists(), "the user's openmw.cfg must not be touched")

        upload, create = client.sent
        self.assertEqual(upload[0:2], ("upload", Path(result["bundle"]).name))
        data = create[2]
        self.assertEqual(data["collection_schema_id"], 1)
        self.assertTrue(data["adult_content"])  # mod 2 is flagged adult
        manifest = data["collection_manifest"]
        self.assertEqual(manifest["info"]["name"], "My Test List")
        self.assertEqual(manifest["info"]["author"], "Tester")
        self.assertEqual([m["name"] for m in manifest["mods"]], ["Mod 1", "Mod 2"])  # list order is data order
        src = manifest["mods"][0]["source"]
        self.assertEqual((src["type"], src["mod_id"], src["file_id"]), ("nexus", "1", "10"))
        self.assertEqual(len(src["md5"]), 32)

        out = Path(result["output_dir"])
        order = (out / "load-order.txt").read_text().split()
        self.assertEqual(order, ["Morrowind.esm", "2-20.esp", "1-10.esp"])  # the fake plox reversed the plugins
        bundle = json.loads((out / "collection.json").read_text())
        self.assertEqual([e["name"] for e in bundle["loadOrder"]], order)

        # The bundle's collection.json is what `mwpipe run` reads back: ids and load order survive.
        coll = Collection.load(out / "collection.json", {}, include_optional=False)
        self.assertEqual(coll.plugin_order(), order)
        self.assertEqual([m.key for m in coll.mods], ["1-10", "2-20"])

    def test_disabled_plugin_is_left_out_everywhere(self):
        def two_plugins(url, dest, md5=None):  # one mod shipping either/or plugins
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("Data Files/a.esp", "a")
                zf.writestr("Data Files/a (louder).esp", "b")
            return dest
        text = "https://www.nexusmods.com/morrowind/mods/1\n- a (louder).esp\n- typo.esp\n"
        seen = []
        with mock.patch.object(publish, "download", side_effect=two_plugins), \
                mock.patch.object(tools, "run", side_effect=lambda cmd, cfg, cwd, dry=False: seen.append(cfg.read_text())):
            client = FakeClient()
            with self.assertLogs("mwpipe", "WARNING") as logs:
                result = publish.build_collection(self.s, client, text, name="Either Or", upload=False)
        self.assertIn("a.esp", seen[0])
        self.assertNotIn("louder", seen[0])  # plox never saw the alternative, so it reports no conflict
        self.assertTrue(any("typo.esp" in line for line in logs.output))
        out = Path(result["output_dir"])
        self.assertEqual((out / "load-order.txt").read_text().split("\n")[:-1], ["Morrowind.esm", "a.esp"])
        coll = Collection.load(out / "collection.json", {}, include_optional=False)
        self.assertEqual(coll.plugin_order(), ["Morrowind.esm", "a.esp"])
        self.assertIn("a (louder).esp", coll.disabled_plugins())

    def test_no_upload_and_new_revision(self):
        text = "https://www.nexusmods.com/morrowind/mods/1\n"
        client, result = self.run_build(text, name="Another List", upload=False)
        self.assertEqual(client.sent, [])
        self.assertNotIn("url", result)
        client, result = self.run_build(text, name="Another List", collection_id="42")
        self.assertEqual(client.sent[1][1], "42")
        self.assertNotIn("url", result)

    def test_direct_mod_and_cached_rerun(self):
        text = ("https://www.nexusmods.com/morrowind/mods/1\n"
                "https://gitlab.com/modding-openmw/some-tool/-/package_files/99/download\n")
        client, result = self.run_build(text, name="With Direct")
        mods = client.sent[1][2]["collection_manifest"]["mods"]
        self.assertEqual(mods[1]["source"]["type"], "direct")
        self.assertEqual(mods[1]["name"], "some-tool")
        self.assertTrue(mods[1]["source"]["logical_filename"].endswith(".zip"))  # sniffed from content
        with mock.patch.object(publish, "download", side_effect=AssertionError("must reuse the cache")), \
                mock.patch.object(tools, "run"):
            publish.build_collection(self.s, FakeClient(), text, name="With Direct", upload=False)

    def test_unresolvable_mod(self):
        text = "https://www.nexusmods.com/morrowind/mods/1\nhttps://www.nexusmods.com/morrowind/mods/4\n"
        with self.assertRaises(PipelineError) as ctx:
            self.run_build(text, name="Broken List")
        self.assertIn("mods/4", str(ctx.exception))
        client, result = self.run_build(text, name="Broken List", allow_missing=True)
        self.assertEqual(result["mods"], 1)

    def test_name_limits(self):
        for bad in ("ab", "x" * 37):
            with self.assertRaises(PipelineError):
                self.run_build("https://www.nexusmods.com/morrowind/mods/1", name=bad)

    def test_build_keeps_only_plugin_files(self):
        def rich_archive(url, dest, md5=None):
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("Data Files/a.esp", "a")
                zf.writestr("Data Files/readme.txt", "x")
                zf.writestr("Data Files/textures/t.dds", "t")
                zf.writestr("Data Files/meshes/sub/m.nif", "m")
            return dest
        with mock.patch.object(publish, "download", side_effect=rich_archive), \
                mock.patch.object(tools, "run"):
            publish.build_collection(self.s, FakeClient(), "https://www.nexusmods.com/morrowind/mods/1\n",
                                     name="Slim List", upload=False)
            publish.build_collection(self.s, FakeClient(), "https://www.nexusmods.com/morrowind/mods/1\n",
                                     name="Slim List", upload=False)  # rebuild: the skip path must keep it slim too
        # where the build installs depends on whether it has its own ModData; check both places
        files = [p.name for d in (self.s.mods_dir, self.s.state_dir / "builds/slim-list/ModData")
                 for p in d.rglob("*") if p.is_file()]
        self.assertEqual(files, ["a.esp"])


class TestBundle(unittest.TestCase):
    def test_zip_fallback_roundtrip(self):
        from mwpipe.archives import extract
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "collection.json").write_text('{"mods": []}')
            with mock.patch.object(publish.shutil, "which", return_value=None):
                bundle = publish.make_bundle(out)
            self.assertEqual(bundle.name, "collection.zip")
            self.assertEqual(json.loads((extract(bundle, out / "x") / "collection.json").read_text()), {"mods": []})

    def test_7z_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "collection.json").write_text("{}")
            done = SimpleNamespace(returncode=0, stderr="")
            with mock.patch.object(publish.shutil, "which", return_value="/usr/bin/7z"), \
                    mock.patch.object(publish.subprocess, "run", return_value=done) as run:
                bundle = publish.make_bundle(out)
            self.assertEqual(bundle.name, "collection.7z")
            self.assertEqual(run.call_args[0][0], ["/usr/bin/7z", "a", "-t7z", "-bso0", "-bsp0",
                                                  "collection.7z", "collection.json"])
            self.assertEqual(run.call_args[1]["cwd"], out)


class TestUploadFlow(unittest.TestCase):
    def test_upload_sequence(self):
        client = NexusClient("key")
        calls = []

        def api(url, body=None, retries=3, method=None):
            calls.append((method or ("POST" if body is not None else "GET"), url.rsplit("/v3", 1)[-1], body))
            if url.endswith("/uploads"):
                return {"data": {"id": "u1", "presigned_url": "https://s3.example/put", "state": "created"}}
            if url.endswith("/finalise"):
                return {"data": {"state": "created"}}
            return {"data": {"state": "available"}}

        client._api = api
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "collection.7z")
            path.write_bytes(b"hello")
            with mock.patch("mwpipe.nexus.put_presigned") as put, mock.patch("mwpipe.nexus.time.sleep"):
                self.assertEqual(client.upload_file(path, "collection.7z"), "u1")
        self.assertEqual(calls[0][2]["md5"], "5d41402abc4b2a76b9719d911017c592")
        self.assertEqual(calls[0][2]["size_bytes"], 5)
        self.assertEqual([c[1] for c in calls], ["/uploads", "/uploads/u1/finalise", "/uploads/u1"])
        self.assertEqual(put.call_args[0][1:], (path, "collection.7z", "5d41402abc4b2a76b9719d911017c592"))


if __name__ == "__main__":
    unittest.main()
