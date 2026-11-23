"""Tests for the code that handles untrusted archives, the API key and the command line."""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run from a checkout without installing

from mwpipe import archives, cli, fomod, install, nexus, openmw_cfg, pipeline, settings, util  # noqa: E402
from mwpipe.util import PipelineError  # noqa: E402


class TestSanitise(unittest.TestCase):
    def test_symlinks_are_removed_and_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "x")
            outside = Path(tmp, "secret.txt")
            outside.write_text("secret")
            root.mkdir()
            (root / "ok.txt").write_text("ok")
            (root / "link").symlink_to(outside)
            (root / "dirlink").symlink_to(tmp, target_is_directory=True)
            with self.assertLogs("mwpipe", "WARNING"):
                archives.sanitise(root)
            self.assertFalse((root / "link").is_symlink() or (root / "link").exists())
            self.assertFalse((root / "dirlink").is_symlink() or (root / "dirlink").exists())
            self.assertEqual(outside.read_text(), "secret")
            self.assertTrue((root / "ok.txt").exists())

    def test_permissions_are_normalised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "d").mkdir()
            f = root / "d" / "f.esp"
            f.write_text("x")
            f.chmod(0o777)
            archives.sanitise(root)
            self.assertEqual(f.stat().st_mode & 0o777, 0o644)
            self.assertEqual((root / "d").stat().st_mode & 0o777, 0o755)


class TestExtract(unittest.TestCase):
    def _zip(self, path: Path, members: dict[str, str]) -> None:
        with zipfile.ZipFile(path, "w") as zf:
            for name, text in members.items():
                zf.writestr(name, text)

    def test_zip_fallback_extracts_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp, "a.zip")
            self._zip(archive, {"Data Files/a.esp": "x"})
            with mock.patch.object(archives.shutil, "which", return_value=None):
                dest = archives.extract(archive, Path(tmp, "out"))
            self.assertEqual((dest / "Data Files" / "a.esp").read_text(), "x")

    def test_zip_entry_that_leaves_the_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp, "evil.zip")
            self._zip(archive, {"../escaped.txt": "x"})
            with mock.patch.object(archives.shutil, "which", return_value=None):
                with self.assertRaises(PipelineError) as ctx:
                    archives.extract(archive, Path(tmp, "out"))
            self.assertIn("leaves the extraction directory", str(ctx.exception))
            self.assertFalse(Path(tmp, "escaped.txt").exists())

    def test_unknown_format_without_extractor_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp, "a.7z")
            archive.write_bytes(b"not an archive")
            with mock.patch.object(archives.shutil, "which", return_value=None):
                with self.assertRaises(PipelineError) as ctx:
                    archives.extract(archive, Path(tmp, "out"))
            self.assertIn("no extractor available", str(ctx.exception))


class TestReadApiKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        secret = mock.patch.object(settings, "SECRET_FILE", self.dir / "no-secret")
        secret.start()
        self.addCleanup(secret.stop)

    def _file(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path

    def test_flag_file_has_priority_over_environment(self):
        key_file = self._file("k", "from-file\n")
        os.environ["NEXUS_API_KEY"] = "from-env"
        self.assertEqual(settings.read_api_key(key_file, False), "from-file")

    def test_env_file_then_docker_secret_then_env_key(self):
        os.environ["NEXUS_API_KEY"] = "from-env"
        self.assertEqual(settings.read_api_key(None, False), "from-env")
        with mock.patch.object(settings, "SECRET_FILE", self._file("secret", "from-secret")):
            self.assertEqual(settings.read_api_key(None, False), "from-secret")
            os.environ["NEXUS_API_KEY_FILE"] = str(self._file("envfile", "from-env-file"))
            self.assertEqual(settings.read_api_key(None, False), "from-env-file")

    def test_empty_file_is_skipped(self):
        os.environ["NEXUS_API_KEY"] = "from-env"
        self.assertEqual(settings.read_api_key(self._file("empty", "\n"), False), "from-env")

    def test_stdin(self):
        with mock.patch.object(sys, "stdin", io.StringIO("from-stdin\nsecond line\n")):
            self.assertEqual(settings.read_api_key(None, True), "from-stdin")

    def test_key_with_whitespace_is_rejected(self):
        with self.assertRaises(PipelineError):
            settings.read_api_key(self._file("bad", "two words"), False)

    def test_no_key_and_no_terminal_is_an_error(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = False
        with mock.patch.object(sys, "stdin", stdin):
            with self.assertRaises(PipelineError) as ctx:
                settings.read_api_key(None, False)
        self.assertIn("No Nexus API key", str(ctx.exception))


class TestApiErrors(unittest.TestCase):
    URL = f"{nexus.API_V1}/users/validate.json"

    def _http_error(self, code: int, body: bytes = b"detail"):
        return urllib.error.HTTPError(self.URL, code, "x", {}, io.BytesIO(body))

    def _call(self, error, **kwargs):
        client = nexus.NexusClient("key")
        with mock.patch("mwpipe.nexus._open", side_effect=error), mock.patch("time.sleep") as sleep:
            try:
                client._api(self.URL, **kwargs)
            finally:
                self.sleep = sleep

    def test_401_and_403_have_their_own_status(self):
        for code in (401, 403):
            with self.assertRaises(nexus.ApiError) as ctx:
                self._call(self._http_error(code))
            self.assertEqual(ctx.exception.status, code)

    def test_other_status_is_reported_with_detail(self):
        with self.assertRaises(nexus.ApiError) as ctx:
            self._call(self._http_error(500, b"server broke"))
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("server broke", str(ctx.exception))

    def test_429_is_retried_and_then_reported(self):
        with self.assertRaises(nexus.ApiError) as ctx:
            self._call([self._http_error(429)] * 3, retries=3)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(self.sleep.call_count, 2)

    def test_the_key_goes_to_the_api_host_only(self):
        client = nexus.NexusClient("key")
        with mock.patch("mwpipe.nexus._open") as urlopen:
            for url in ("https://cdn.example/x", "https://api.nexusmods.com.evil.example/x"):
                with self.assertRaises(PipelineError):
                    client._api(url)
            urlopen.assert_not_called()

    def test_presigned_upload_needs_https_and_sends_no_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "f.7z")
            path.write_bytes(b"data")
            with self.assertRaises(PipelineError):
                nexus.put_presigned("http://storage.example/x", path, "f.7z", "0" * 32)
            with mock.patch("mwpipe.nexus._open") as urlopen:
                nexus.put_presigned("https://storage.example/x", path, "f.7z", "0" * 32)
            request = urlopen.call_args[0][0]
            self.assertNotIn("Apikey", request.headers)

    def test_download_over_http_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(PipelineError):
            nexus.download("http://cdn.example/x.7z", Path(tmp, "x.7z"))


class TestCli(unittest.TestCase):
    def test_steps_are_split_and_checked(self):
        self.assertEqual(cli._steps("fetch, download,,sort"), ["fetch", "download", "sort"])
        with self.assertRaises(argparse.ArgumentTypeError) as ctx:
            cli._steps("fetch,bogus,other")
        self.assertIn("bogus, other", str(ctx.exception))

    def test_parser_rejects_unknown_step_and_missing_command(self):
        parser = cli.build_parser()
        for argv in (["run", "--steps", "bogus"], []):
            with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit) as ctx:
                parser.parse_args(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_parser_defaults(self):
        args = cli.build_parser().parse_args(["run"])
        self.assertEqual((args.sort, args.skip, args.dry_run), ("auto", [], False))

    def test_pipeline_error_gives_exit_code_1(self):
        with mock.patch.object(cli.Settings, "from_args", side_effect=PipelineError("broken")):
            with self.assertLogs("mwpipe", "ERROR") as logs:
                self.assertEqual(cli.main(["run"]), 1)
        self.assertIn("broken", logs.output[0])

    def test_keyboard_interrupt_gives_exit_code_130(self):
        with mock.patch.object(cli.Settings, "from_args", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["run"]), 130)

    def test_stdin_cannot_carry_key_and_mod_list(self):
        with mock.patch.object(cli.Settings, "from_args"), \
                mock.patch.object(cli, "read_api_key", return_value="key"):
            with self.assertLogs("mwpipe", "ERROR") as logs:
                self.assertEqual(cli.main(["--api-key-stdin", "build-collection", "-"]), 1)
        self.assertIn("both read stdin", logs.output[0])


if __name__ == "__main__":
    unittest.main()


class TestPathContainment(unittest.TestCase):
    """Paths from FOMOD files and collection.json must stay in their directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _fomod(self, files_xml: str) -> Path:
        tree = self.tmp / "tree"
        (tree / "fomod").mkdir(parents=True)
        (tree / "a.esp").write_text("a")
        (self.tmp / "secret").write_text("secret")
        config = tree / "fomod" / "ModuleConfig.xml"
        config.write_text(f"<config><requiredInstallFiles>{files_xml}</requiredInstallFiles></config>")
        return config

    def test_safe_join(self):
        base = self.tmp / "base"
        self.assertEqual(util.safe_join(base, "/a\\b"), base / "a" / "b")
        for bad in ("../x", "a/../../x", "..\\x"):
            with self.subTest(bad), self.assertRaises(PipelineError):
                util.safe_join(base, bad)

    def test_fomod_destination_outside_the_mod_folder_is_rejected(self):
        config = self._fomod('<file source="a.esp" destination="../../escaped.esp"/>')
        dest = self.tmp / "ModData" / "coll" / "mod"
        with self.assertRaises(PipelineError):
            fomod.FomodInstaller(config, None).install(dest)
        self.assertFalse((self.tmp / "ModData" / "escaped.esp").exists())

    def test_fomod_source_outside_the_archive_is_rejected(self):
        for xml in ('<file source="../secret" destination="stolen.txt"/>',
                    '<folder source=".." destination="stolen"/>'):
            with self.subTest(xml):
                shutil.rmtree(self.tmp / "tree", ignore_errors=True)
                dest = self.tmp / "mod"
                with self.assertRaises(PipelineError):
                    fomod.FomodInstaller(self._fomod(xml), None).install(dest)
                self.assertFalse((dest / "stolen.txt").exists() or (dest / "stolen").exists())

    def test_fomod_inside_paths_still_work(self):
        config = self._fomod('<file source="a.esp" destination="sub/../b.esp"/>')
        dest = self.tmp / "mod"
        fomod.FomodInstaller(config, None).install(dest)
        self.assertEqual((dest / "b.esp").read_text(), "a")

    def test_replicate_path_outside_the_mod_folder_is_rejected(self):
        tree = self.tmp / "tree"
        (tree / "x..").mkdir(parents=True)
        (tree / "x.." / "evil.esp").write_text("e")
        dest = self.tmp / "mods" / "mod"
        with self.assertRaises(PipelineError):
            install.install_from_hashes(tree, [{"path": "../evil.esp"}], dest)
        self.assertFalse((self.tmp / "mods" / "evil.esp").exists())

    def test_bundled_file_expression_cannot_leave_the_bundle(self):
        args = argparse.Namespace(data_dir=self.tmp / "data", openmw_cfg=self.tmp / "cfg" / "openmw.cfg",
                                  morrowind_data=None, mods_dir=None, downloads_dir=None, generated_dir=None,
                                  cache_dir=self.tmp / "cache", state_dir=self.tmp / "state",
                                  config_dir=self.tmp / "config", dry_run=False)
        s = settings.Settings.from_args(args)
        s.ensure_dirs()
        (s.state_dir / "secret.txt").write_text("secret")
        coll = s.state_dir / "collections" / "c-1"
        (coll / "bundled").mkdir(parents=True)
        (coll / "collection.json").write_text(json.dumps({"mods": [
            {"name": "Evil", "source": {"type": "bundle", "fileExpression": "../../../secret"}}]}))
        (s.state_dir / "current.json").write_text(json.dumps(
            {"slug": "c", "game": "morrowind", "revision": 1, "name": "C", "dir": "collections/c-1"}))
        with self.assertRaises(PipelineError) as ctx:
            pipeline.Pipeline(s).download()
        self.assertIn("path separator", str(ctx.exception))
        self.assertEqual(list(s.downloads_dir.iterdir()), [])


class TestRedirects(unittest.TestCase):
    def _redirect(self, url: str, target: str):
        request = urllib.request.Request(url, headers={"apikey": "KEY"})
        return nexus._SafeRedirects().redirect_request(request, None, 302, "Found", {}, target)

    def test_redirect_to_http_is_refused(self):
        with self.assertRaises(PipelineError):
            self._redirect("https://api.nexusmods.com/v1/x", "http://api.nexusmods.com/v1/x")

    def test_api_key_is_not_sent_to_another_host(self):
        new = self._redirect("https://api.nexusmods.com/v1/x", "https://cdn.example/x")
        self.assertNotIn("Apikey", new.headers)
        self.assertEqual(new.full_url, "https://cdn.example/x")

    def test_api_key_is_kept_on_the_api_host(self):
        new = self._redirect("https://api.nexusmods.com/v1/x", "https://api.nexusmods.com/v1/y")
        self.assertEqual(new.headers.get("Apikey"), "KEY")


class TestConfigLineBreaks(unittest.TestCase):
    def test_managed_value_with_a_line_break_is_rejected(self):
        for cfg in (openmw_cfg.OpenMWConfig(content=['x.esp\ndata="/injected"']),
                    openmw_cfg.OpenMWConfig(archives=["a.bsa\rcontent=b.esp"]),
                    openmw_cfg.OpenMWConfig(data=[Path("/a b")])):
            with self.subTest(cfg), self.assertRaises(PipelineError):
                cfg.render()

    def test_normal_values_render(self):
        text = openmw_cfg.OpenMWConfig(content=["A & B.esp"], data=[Path("/a b")]).render()
        self.assertIn("content=A & B.esp\n", text)
        self.assertIn('data="/a b"\n', text)


class TestSevenZipGuard(unittest.TestCase):
    def setUp(self):
        archives._SEVEN_ZIP_OK.clear()
        self.addCleanup(archives._SEVEN_ZIP_OK.clear)

    def _banner(self, text: str) -> bool:
        done = subprocess.CompletedProcess(["7z"], 0, stdout=text, stderr="")
        with mock.patch.object(archives.subprocess, "run", return_value=done):
            return archives._seven_zip_is_safe("7z")

    def test_p7zip_is_not_used(self):
        with self.assertLogs("mwpipe", "WARNING"):
            self.assertFalse(self._banner("7-Zip [64] 16.02 : Copyright (c) 1999-2016 Igor Pavlov : 2016-05-21\n"
                                          "p7zip Version 16.02 (locale=C.UTF-8,Utf16=on)"))

    def test_7zip_is_used(self):
        self.assertTrue(self._banner("7-Zip (z) 24.09 (x64) : Copyright (c) 1999-2024 Igor Pavlov : 2024-11-29"))

    def test_extract_skips_p7zip_and_uses_bsdtar(self):
        calls = []

        def banner(cmd, **kwargs):
            calls.append(cmd[0])
            return subprocess.CompletedProcess(cmd, 0, stdout="p7zip Version 16.02", stderr="")

        def extractor(cmd, dest, limit):
            calls.append(cmd[0])
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(archives.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.object(archives.subprocess, "run", side_effect=banner), \
                mock.patch.object(archives, "_run_limited", side_effect=extractor), \
                self.assertLogs("mwpipe", "WARNING"):
            archives.extract(Path(tmp, "a.7z"), Path(tmp, "out"))
        self.assertEqual(calls, ["7z", "bsdtar"])


class TestExtractionLimit(unittest.TestCase):
    """An archive must not fill the disk (a "zip bomb")."""

    LIMIT = 1 << 20  # 1 MiB keeps the tests fast

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "out"
        self.dest.mkdir()

    def _run(self, script: str):
        return archives._run_limited(["sh", "-c", script, "sh", str(self.dest)], self.dest, self.LIMIT)

    def test_small_extraction_passes(self):
        code, _ = self._run('head -c 1000 /dev/zero > "$1/a"; echo warn >&2')
        self.assertEqual(code, 0)

    def test_errors_of_the_extractor_are_returned(self):
        code, stderr = self._run("echo broken archive >&2; exit 2")
        self.assertEqual((code, stderr.strip()), (2, "broken archive"))

    def test_one_file_larger_than_the_limit_stops_the_extractor(self):
        code, _ = self._run('exec head -c 3145728 /dev/zero > "$1/big"')
        self.assertIsNone(code)
        self.assertLessEqual((self.dest / "big").stat().st_size, self.LIMIT)

    def test_cut_file_is_found_when_the_extractor_continues(self):
        code, _ = self._run('head -c 3145728 /dev/zero > "$1/big"; exit 0')  # the shell survives `head`
        self.assertIsNone(code)

    def test_many_files_larger_than_the_limit_stop_the_extractor(self):
        with mock.patch.object(archives, "_POLL_SECONDS", 0.05):
            code, _ = self._run('i=0; while [ $i -lt 400 ]; do head -c 65536 /dev/zero > "$1/f$i"; '
                                'i=$((i + 1)); sleep 0.01; done')
        self.assertIsNone(code)
        self.assertLess(len(list(self.dest.iterdir())), 400)

    def test_extract_removes_the_files_and_reports_the_limit(self):
        (self.dest / "partial").write_text("x")
        with mock.patch.object(archives.shutil, "which", side_effect=lambda c: "/usr/bin/bsdtar" if c == "bsdtar" else None), \
                mock.patch.object(archives, "_run_limited", return_value=(None, "")) as run:
            with self.assertRaises(PipelineError) as ctx:
                archives.extract(self.tmp / "a.7z", self.dest, self.LIMIT)
        self.assertEqual(run.call_count, 1)  # no other extractor is tried
        self.assertIn("--extract-limit", str(ctx.exception))
        self.assertFalse(self.dest.exists())

    def test_zip_fallback_checks_the_sizes_in_the_headers(self):
        archive = self.tmp / "bomb.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zeros.bin", b"\0" * (2 * self.LIMIT))
        self.assertLess(archive.stat().st_size, self.LIMIT)
        with mock.patch.object(archives.shutil, "which", return_value=None), self.assertRaises(PipelineError):
            archives.extract(archive, self.dest, self.LIMIT)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_fomod_copies_count_against_the_limit(self):
        tree = self.tmp / "tree"
        (tree / "fomod").mkdir(parents=True)
        (tree / "big.bsa").write_bytes(b"\0" * (self.LIMIT // 3))
        copies = "".join(f'<file source="big.bsa" destination="c{i}.bsa"/>' for i in range(5))
        (tree / "fomod" / "ModuleConfig.xml").write_text(
            f"<config><requiredInstallFiles>{copies}</requiredInstallFiles></config>")
        installer = fomod.FomodInstaller(tree / "fomod" / "ModuleConfig.xml", None, self.LIMIT)
        with self.assertRaises(PipelineError):
            installer.install(self.tmp / "mod")
        self.assertLessEqual(archives.tree_size(self.tmp / "mod"), self.LIMIT)

    def test_limit_from_flag_and_environment(self):
        with mock.patch.dict(os.environ, {"MWPIPE_EXTRACT_LIMIT": ""}):
            self.assertEqual(settings.extract_limit(None), 10 << 30)
            self.assertEqual(settings.extract_limit(0.5), 1 << 29)
        with mock.patch.dict(os.environ, {"MWPIPE_EXTRACT_LIMIT": "25"}):
            self.assertEqual(settings.extract_limit(None), 25 << 30)
            self.assertEqual(settings.extract_limit(2), 2 << 30)  # the flag has priority
        for bad in ("lots", "0", "-1"):
            with mock.patch.dict(os.environ, {"MWPIPE_EXTRACT_LIMIT": bad}), self.assertRaises(PipelineError):
                settings.extract_limit(None)
        self.assertEqual(cli.build_parser().parse_args(["--extract-limit", "3", "postprocess"]).extract_limit, 3.0)
