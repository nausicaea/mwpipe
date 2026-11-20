"""Offline tests for `mwpipe build-collection`: mod list parsing, manifests and the whole flow."""

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
        entries = parse_mod_list(LIST)  # the repeated mod 1 line is dropped
        self.assertEqual([(e.game, e.mod_id, e.file_id) for e in entries],
                         [("morrowind", 1, None), ("morrowind", 2, 22), ("morrowind", 3, 33), (None, None, None)])
        self.assertFalse(entries[-1].is_nexus)

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
        return {"version": f"v{file_id}"}

    def file_url(self, game, mod_id, file_id):
        return f"https://cdn.example/{mod_id}-{file_id}.zip"

    def upload_file(self, path, filename):
        self.sent.append(("upload", filename, path.is_file()))
        return "11111111-1111-1111-1111-111111111111"

    def create_collection(self, upload_id, collection_data, collection_id=None):
        self.sent.append(("create", collection_id, collection_data))
        return {"id": "42", "slug": "abc123", "revision_number": 1, "revision_status": "draft"}


def fake_download(url, dest, md5=None):
    """Every 'archive' is a zip with one plugin named after the URL."""
    stem = Path(url).stem
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(f"Data Files/{stem}.esp", stem)
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

    def run_build(self, text, **kwargs):
        def fake_plox(cmd, cfg_path, cwd, dry_run=False):  # reverse the plugin order, like a real re-sort
            lines = cfg_path.read_text().splitlines()
            content = [ln for ln in lines if ln.startswith("content=")]
            rest = [ln for ln in lines if not ln.startswith("content=")]
            cfg_path.write_text("\n".join(rest + content[::-1]) + "\n")
        client = FakeClient()
        with mock.patch.object(publish, "download", side_effect=fake_download), \
                mock.patch.object(tools, "run", side_effect=fake_plox):
            result = publish.build_collection(self.s, client, text, **kwargs)
        return client, result

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
        self.assertEqual(order, ["2-20.esp", "1-10.esp"])  # the fake plox reversed it
        bundle = json.loads((out / "collection.json").read_text())
        self.assertEqual([e["name"] for e in bundle["loadOrder"]], order)

        # The bundle's collection.json is what `mwpipe run` reads back: ids and load order survive.
        coll = Collection.load(out / "collection.json", {}, include_optional=False)
        self.assertEqual(coll.plugin_order(), order)
        self.assertEqual([m.key for m in coll.mods], ["1-10", "2-20"])

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
