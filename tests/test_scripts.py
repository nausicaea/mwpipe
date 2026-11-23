"""Tests for the shell scripts and for the security settings in compose.yaml and the Dockerfile."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE_DOCKER = '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_LOG"\nprintf "PUID=%s PGID=%s\\n" "$PUID" "$PGID" >> "$FAKE_LOG"\n'


class TestWrapperScript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        shutil.copy2(ROOT / "mwpipe.sh", self.project / "mwpipe.sh")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "docker.log"

    def run_script(self, *args, env=None):
        full_env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log), "HOME": str(self.tmp)}
        full_env.update(env or {})
        return subprocess.run(["sh", str(self.project / "mwpipe.sh"), *args], env=full_env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if not line.startswith("PUID=")]

    def test_scripts_have_valid_syntax(self):
        for script in ("mwpipe.sh", "docker/fetch-gitlab-tool.sh"):
            result = subprocess.run(["sh", "-n", str(ROOT / script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_env_file_is_an_error(self):
        result = self.run_script("run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cp .env.example .env", result.stderr)
        self.assertFalse(self.log.exists())

    def test_secret_file_is_created_private_and_empty(self):
        (self.project / ".env").write_text("")
        self.assertEqual(self.run_script("run").returncode, 0)
        secret = self.project / "secrets/nexus_api_key"
        self.assertEqual(secret.read_text(), "")
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)

    def test_existing_secret_is_not_changed_and_path_can_be_set(self):
        (self.project / ".env").write_text("")
        secret = self.project / "mykey"
        secret.write_text("KEY")
        self.assertEqual(self.run_script("run", env={"NEXUS_API_KEY_SECRET": str(secret)}).returncode, 0)
        self.assertEqual(secret.read_text(), "KEY")
        self.assertFalse((self.project / "secrets").exists())

    def test_default_builds_locally_and_passes_arguments_and_ids(self):
        (self.project / ".env").write_text("")
        self.assertEqual(self.run_script("run", "--dry-run", "a b").returncode, 0)
        self.assertEqual(self.calls(), ["compose run --rm -T --build mwpipe run --dry-run a b"])
        self.assertIn(f"PUID={os.getuid()} PGID={os.getgid()}", self.log.read_text())

    def test_published_image_is_pulled_and_not_built(self):
        (self.project / ".env").write_text("")
        self.assertEqual(self.run_script("postprocess", env={"MWPIPE_IMAGE": "ghcr.io/x/mwpipe:latest"}).returncode, 0)
        self.assertEqual(self.calls(), ["compose pull --quiet mwpipe", "compose run --rm -T mwpipe postprocess"])


class TestFetchScript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bin = Path(self._tmp.name)
        self.addCleanup(lambda: Path("/tmp/tool-linux-amd64.zip").unlink(missing_ok=True))  # the script's download

    def fake_curl(self, body: str) -> None:
        curl = self.bin / "curl"
        curl.write_text("#!/bin/sh\n" + body)
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)

    def run_script(self, *args, arch="amd64"):
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": self._tmp.name}
        if arch:
            env["TARGETARCH"] = arch
        return subprocess.run(["sh", str(ROOT / "docker/fetch-gitlab-tool.sh"), *args], env=env,
                              capture_output=True, text=True)

    def test_build_without_buildkit_is_an_error(self):
        result = self.run_script("tool", "1.0", arch="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TARGETARCH", result.stderr)

    def test_missing_ci_artifact_is_reported(self):
        self.fake_curl("exit 22\n")
        result = self.run_script("tool", "1.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No CI artifact for tool 1.0", result.stderr)

    def test_checksum_mismatch_stops_the_install(self):
        self.fake_curl('while [ $# -gt 0 ]; do [ "$1" = -o ] && printf data > "$2"; shift; done\n')
        result = self.run_script("tool", "1.0", "0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(Path("/out/tool").exists())


class TestContainerHardening(unittest.TestCase):
    """The README promises these limits, so a change to them must break a test."""

    def test_compose_limits(self):
        compose = (ROOT / "compose.yaml").read_text()
        for setting in ("read_only: true", "cap_drop: [ALL]", 'security_opt: ["no-new-privileges:true"]'):
            self.assertIn(setting, compose)

    def test_compose_config_files_are_mounted_read_only_by_the_compose_configs_feature(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("MWPIPE_OVERRIDES: /config/overrides.json", compose)
        self.assertNotIn("privileged", compose)

    def test_image_runs_as_a_normal_user(self):
        lines = (ROOT / "Dockerfile").read_text().splitlines()
        users = [line for line in lines if line.startswith("USER ")]
        self.assertEqual(users, ["USER modder"])
        self.assertTrue(any(re.match(r"RUN adduser -D -u 1000 modder", line) for line in lines))

    def test_base_images_are_pinned_by_digest(self):
        for line in (ROOT / "Dockerfile").read_text().splitlines():
            if line.startswith("FROM ") and "scratch" not in line:
                self.assertRegex(line, r"@sha256:[0-9a-f]{64}", line)


if __name__ == "__main__":
    unittest.main()
