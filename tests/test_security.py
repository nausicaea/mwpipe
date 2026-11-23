"""Tests for the code that handles untrusted archives, the API key and the command line."""

import argparse
import io
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run from a checkout without installing

from mwpipe import archives, cli, nexus, settings  # noqa: E402
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
        with mock.patch("urllib.request.urlopen", side_effect=error), mock.patch("time.sleep") as sleep:
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
        with mock.patch("urllib.request.urlopen") as urlopen:
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
            with mock.patch("urllib.request.urlopen") as urlopen:
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
