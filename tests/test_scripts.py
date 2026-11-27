"""Tests for the shell scripts and for the security settings in compose.yaml and the Dockerfile."""

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# `docker info` writes $FAKE_INFO (the runtime type); other commands are logged with the IDs.
FAKE_DOCKER = ('#!/bin/sh\n[ "$1" = info ] && { printf "%s\\n" "${FAKE_INFO:-}"; exit 0; }\n'
               'printf "%s\\n" "$*" >> "$FAKE_LOG"\nprintf "PUID=%s PGID=%s\\n" "$PUID" "$PGID" >> "$FAKE_LOG"\n')


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
        for script in ("mwpipe.sh", "docker/fetch-gitlab-tool.sh", "docker/update-tool-hashes.sh"):
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

    def test_rootless_docker_uses_container_root(self):
        (self.project / ".env").write_text("")
        info = '["name=seccomp,profile=builtin","name=rootless","name=cgroupns"]'
        self.assertEqual(self.run_script("run", env={"FAKE_INFO": info}).returncode, 0)
        self.assertIn("PUID=0 PGID=0", self.log.read_text())

    def test_rootless_podman_uses_container_root(self):
        (self.project / ".env").write_text("")
        self.assertEqual(self.run_script("run", env={"FAKE_INFO": "true"}).returncode, 0)
        self.assertIn("PUID=0 PGID=0", self.log.read_text())

    def test_ids_from_the_environment_are_kept(self):
        (self.project / ".env").write_text("")
        env = {"FAKE_INFO": "true", "PUID": "1234", "PGID": "5678"}
        self.assertEqual(self.run_script("run", env=env).returncode, 0)
        self.assertIn("PUID=1234 PGID=5678", self.log.read_text())

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


    def test_print_sha256_writes_dockerfile_args(self):
        self.fake_curl('case "$*" in *sha256sum*) exit 22;; esac\n'
                       'while [ $# -gt 0 ]; do [ "$1" = -o ] && printf data > "$2"; shift; done\n')
        result = self.run_script("--print-sha256", "tool", "1.0", arch="")
        self.assertEqual(result.returncode, 0, result.stderr)
        digest = hashlib.sha256(b"data").hexdigest()
        self.assertEqual(result.stdout.splitlines(),
                         [f"ARG TOOL_SHA256_AMD64={digest}", f"ARG TOOL_SHA256_ARM64={digest}"])

    def test_print_sha256_stops_if_the_published_checksum_differs(self):
        self.fake_curl('case "$*" in *sha256sum*) echo "%s  x"; exit 0;; esac\n'
                       'while [ $# -gt 0 ]; do [ "$1" = -o ] && printf data > "$2"; shift; done\n' % ("0" * 64))
        result = self.run_script("--print-sha256", "tool", "1.0", arch="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not the same", result.stderr)


class TestUpdateToolHashes(unittest.TestCase):
    """docker/update-tool-hashes.sh: the renovate-hashes workflow uses it on Renovate branches."""

    DOCKERFILE = ("ARG GOCOVERIFY_VERSION={g}\nARG GOCOVERIFY_SHA256_AMD64={a}\nARG GOCOVERIFY_SHA256_ARM64={a}\n"
                  "ARG LUMENAIRE_VERSION=1.0\nARG LUMENAIRE_SHA256_AMD64={a}\nARG LUMENAIRE_SHA256_ARM64={a}\n")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name, "repo")
        self.bin = Path(self._tmp.name, "bin")
        self.repo.mkdir()
        self.bin.mkdir()
        curl = self.bin / "curl"  # every zip is "data"; there are no published checksum files
        curl.write_text('#!/bin/sh\ncase "$*" in *sha256sum*) exit 22;; esac\n'
                        'while [ $# -gt 0 ]; do [ "$1" = -o ] && printf data > "$2"; shift; done\n')
        curl.chmod(0o755)
        self.git("init", "-q")
        self.dockerfile = self.repo / "Dockerfile"
        self.dockerfile.write_text(self.DOCKERFILE.format(g="1.0", a="0" * 64))
        self.git("add", "Dockerfile")
        self.git("commit", "-qm", "base")

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.repo, check=True)

    def run_script(self):
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": self._tmp.name}
        return subprocess.run(["sh", str(ROOT / "docker/update-tool-hashes.sh"), "HEAD"], cwd=self.repo,
                              env=env, capture_output=True, text=True)

    def test_only_the_tool_with_a_new_version_gets_new_values(self):
        self.dockerfile.write_text(self.DOCKERFILE.format(g="1.1", a="0" * 64))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        digest = hashlib.sha256(b"data").hexdigest()
        lines = self.dockerfile.read_text().splitlines()
        self.assertIn(f"ARG GOCOVERIFY_SHA256_AMD64={digest}", lines)
        self.assertIn(f"ARG GOCOVERIFY_SHA256_ARM64={digest}", lines)
        self.assertIn(f"ARG LUMENAIRE_SHA256_AMD64={'0' * 64}", lines)  # the version did not change

    def test_no_version_change_changes_nothing(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dockerfile.read_text(), self.DOCKERFILE.format(g="1.0", a="0" * 64))

    def test_a_missing_sha256_arg_is_an_error(self):
        self.dockerfile.write_text(self.DOCKERFILE.format(g="1.1", a="0" * 64).replace("GOCOVERIFY_SHA256_ARM64", "X"))
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no ARG GOCOVERIFY_SHA256_ARM64", result.stderr)


class TestWorkflows(unittest.TestCase):
    def test_a_run_on_a_renovate_branch_does_not_publish(self):
        docker = (ROOT / ".github/workflows/docker.yml").read_text()
        publish = ("github.event_name == 'push' || (github.event_name == 'workflow_dispatch' "
                   "&& github.ref_name == github.event.repository.default_branch)")
        self.assertEqual(docker.count(publish), 2)  # env.PUBLISH and the merge job
        self.assertNotIn("github.event_name != 'pull_request'", docker)

    def test_renovate_ignores_the_commits_of_the_hash_workflow(self):
        workflow = (ROOT / ".github/workflows/renovate-hashes.yml").read_text()
        email = "41898282+github-actions[bot]@users.noreply.github.com"
        self.assertIn(email, workflow)
        self.assertIn(email, (ROOT / "renovate.json5").read_text())
        self.assertIn("startsWith(github.head_ref, 'renovate/')", workflow)
        self.assertIn("gh workflow run docker.yml", workflow)


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

    def test_tools_are_pinned_by_commit_and_sha256(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("--tag", dockerfile)  # a tag can move; cargo install uses --rev
        for tool in ("DELTA_PLUGIN", "PLOX"):
            self.assertRegex(dockerfile, rf"ARG {tool}_COMMIT=[0-9a-f]{{40}}\n")
            self.assertIn(f'--rev "${tool}_COMMIT"', dockerfile)
        for tool in ("GOCOVERIFY", "LUMENAIRE"):
            for arch in ("AMD64", "ARM64"):
                self.assertRegex(dockerfile, rf"ARG {tool}_SHA256_{arch}=[0-9a-f]{{64}}\n")


if __name__ == "__main__":
    unittest.main()
