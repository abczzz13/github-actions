import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/commitizen_release.py"
SPEC = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.original = Path.cwd()
        self.addCleanup(os.chdir, self.original)
        self.new_repository("default")

    def new_repository(self, name):
        directory = self.root / name
        directory.mkdir()
        self.remote = directory / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.checkout = directory / "source"
        self.checkout.mkdir()
        os.chdir(self.checkout)
        release.git("init", "--initial-branch=main")
        release.git("config", "user.name", "Test")
        release.git("config", "user.email", "test@example.invalid")
        release.git("remote", "add", "origin", str(self.remote))

    def baseline(self, version="1.2.3", tag=True, extra_config=""):
        Path(".cz.toml").write_text(
            '[tool.commitizen]\nname = "cz_conventional_commits"\n'
            'tag_format = "$version"\nversion_scheme = "semver"\n'
            f'version = "{version}"\nupdate_changelog_on_bump = true\n{extra_config}'
        )
        self.commit("feat!: historical breaking change")
        if tag:
            release.git("tag", version)
        release.git("push", "--tags", "origin", "HEAD:main")

    def commit(self, message):
        release.git("add", ".")
        release.git("commit", "--allow-empty", "-m", message)
        return release.git("rev-parse", "HEAD")

    def test_version_calculation(self):
        cases = [
            ("fix: repair request validation", "1.2.4"),
            ("feat: add recipes", "1.3.0"),
            ("feat!: remove legacy endpoint", "2.0.0"),
            ("docs: explain configuration", None),
        ]
        # Separate repositories keep tags and changelog state isolated per case.
        for index, (message, expected) in enumerate(cases):
            with self.subTest(message=message):
                if index:
                    self.new_repository(f"case-{index}")
                self.baseline()
                sha = self.commit(message)
                release.git("push", "origin", "HEAD:main")
                result = release.create_release(sha, "main")
                if expected is None:
                    self.assertIsNone(result)
                    self.assertEqual(release.git("rev-parse", "HEAD"), sha)
                else:
                    head = release.git("rev-parse", "HEAD")
                    self.assertEqual(result, {
                        "created": "true", "version": expected, "tag": expected, "sha": head,
                    })
                    self.assertEqual(release.configured_version(), expected)
                    self.assertEqual(release.git("rev-parse", f"{expected}^{{commit}}"), head)
                    self.assertEqual(release.git("status", "--porcelain"), "")
                    remote = release.git("ls-remote", "origin", f"refs/tags/{expected}")
                    self.assertEqual(remote, f"{head}\trefs/tags/{expected}")
                    self.assertIn(expected, Path("CHANGELOG.md").read_text())

    def test_bootstrap_uses_explicit_version_not_historical_increment(self):
        self.baseline("0.1.0", tag=False)
        sha = release.git("rev-parse", "HEAD")
        result = release.create_release(sha, "main", "0.1.0")
        self.assertEqual(result["version"], "0.1.0")
        self.assertEqual(release.configured_version(), "0.1.0")
        self.assertEqual(release.git("tag", "--list"), "0.1.0")

    def test_bootstrap_honours_configured_changelog_file(self):
        self.baseline("0.1.0", tag=False, extra_config='changelog_file = "HISTORY.md"\n')
        sha = release.git("rev-parse", "HEAD")
        self.assertEqual(release.create_release(sha, "main", "0.1.0")["version"], "0.1.0")
        self.assertIn("0.1.0", Path("HISTORY.md").read_text())
        self.assertFalse(Path("CHANGELOG.md").exists())
        self.assertEqual(release.git("status", "--porcelain", "--untracked-files=all"), "")

    def test_missing_tag_requires_explicit_matching_bootstrap(self):
        self.baseline("0.1.0", tag=False)
        sha = release.git("rev-parse", "HEAD")
        for initial_version in ["", "0.2.0"]:
            with self.subTest(initial_version=initial_version):
                with self.assertRaisesRegex(ValueError, "bootstrap"):
                    release.create_release(sha, "main", initial_version)
        self.assertEqual(release.git("tag", "--list"), "")

    def test_initial_version_is_ignored_once_the_tag_exists(self):
        self.baseline()
        sha = self.commit("fix: candidate")
        release.git("push", "origin", "HEAD:main")
        self.assertEqual(release.create_release(sha, "main", "1.2.3")["version"], "1.2.4")

    def test_configured_version_rejects_unsupported_configuration(self):
        cases = [
            ('tag_format = "v$version"\nversion_scheme = "semver"\nversion = "1.2.3"\n', "unprefixed"),
            ('tag_format = "$version"\nversion_scheme = "pep440"\nversion = "1.2.3"\n', "semver"),
            ('tag_format = "$version"\nversion_scheme = "semver"\nversion = "1.2.3-rc.1"\n', "stable"),
            ('tag_format = "$version"\nversion_scheme = "semver"\nversion = "v1.2.3"\n', "stable"),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                Path(".cz.toml").write_text(f"[tool.commitizen]\n{config}")
                with self.assertRaisesRegex(ValueError, expected):
                    release.configured_version()

    def test_refuses_dirty_or_unvalidated_source(self):
        self.baseline()
        sha = release.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "validated SHA"):
            release.create_release("a" * 40, "main")
        Path("unexpected").write_text("untracked")
        with self.assertRaisesRegex(ValueError, "clean"):
            release.create_release(sha, "main")

    def advance_remote(self):
        other = self.root / "other"
        subprocess.run(
            ["git", "clone", "--branch", "main", str(self.remote), str(other)],
            check=True, capture_output=True,
        )
        for args in [
            ["config", "user.name", "Other"],
            ["config", "user.email", "other@example.invalid"],
            ["commit", "--allow-empty", "-m", "feat: concurrent change"],
            ["push", "origin", "HEAD:main"],
        ]:
            subprocess.run(["git", "-C", str(other), *args], check=True, capture_output=True)

    def test_stale_main_is_noop(self):
        self.baseline()
        sha = self.commit("feat: candidate")
        release.git("push", "origin", "HEAD:main")
        self.advance_remote()
        self.assertIsNone(release.create_release(sha, "main"))
        self.assertEqual(release.git("tag", "--list"), "1.2.3")
        self.assertEqual(release.git("rev-parse", "HEAD"), sha)

    def test_atomic_push_rejects_race_without_publishing_tag(self):
        self.baseline()
        sha = self.commit("feat: candidate")
        release.git("push", "origin", "HEAD:main")
        real_cz = release.cz

        def bump_then_race(*args):
            result = real_cz(*args)
            self.advance_remote()
            return result

        with mock.patch.object(release, "cz", side_effect=bump_then_race):
            with self.assertRaises(subprocess.CalledProcessError):
                release.create_release(sha, "main")
        self.assertEqual(release.git("ls-remote", "origin", "refs/tags/1.3.0"), "")

    def test_main_limits_token_to_transport_environment(self):
        environment = {
            "RELEASE_TOKEN": "test-only-token",
            "VALIDATED_SHA": "a" * 40,
            "RELEASE_BRANCH": "main",
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
        }
        with mock.patch.dict(os.environ, environment):
            with mock.patch.object(release.sys, "argv", ["script", "bump"]):
                with mock.patch.object(release, "create_release", return_value=None) as create:
                    self.assertEqual(release.main(), 0)
                    self.assertNotIn("RELEASE_TOKEN", os.environ)
                    transport = create.call_args.args[3]
                    self.assertNotIn("RELEASE_TOKEN", transport)
                    self.assertEqual(transport["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
                    self.assertTrue(transport["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic "))
        self.assertEqual((self.root / "output").read_text(), "created=false\nversion=\ntag=\nsha=\n")

    def test_rerun_of_validated_revision_does_not_bump_twice(self):
        self.baseline()
        sha = self.commit("fix: candidate")
        release.git("push", "origin", "HEAD:main")
        first = release.create_release(sha, "main")
        release.git("checkout", "--detach", sha)
        self.assertIsNone(release.create_release(sha, "main"))
        self.assertEqual(release.git("tag", "--list"), "1.2.3\n1.2.4")
        self.assertEqual(first["version"], "1.2.4")


if __name__ == "__main__":
    unittest.main()
