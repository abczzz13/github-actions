import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from helpers import ROOT

SCRIPT = ROOT / "scripts/resolve-release"


class ResolveReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.directory.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("commit", "-q", "--allow-empty", "-m", "release")
        self.release = self.git("rev-parse", "HEAD")
        self.git("tag", "1.2.3")
        self.git("tag", "-a", "1.2.4", "-m", "annotated")
        self.git("commit", "-q", "--allow-empty", "-m", "main")
        self.main = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "-c", "feature")
        self.git("commit", "-q", "--allow-empty", "-m", "unreviewed")
        self.git("tag", "9.0.0")
        # A branch that looks like a release must not stand in for the tag.
        self.git("branch", "2.0.0")
        self.git("switch", "-q", "main")

    def tearDown(self):
        self.directory.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def resolve(self, tag, reviewed=None):
        output = self.repo / ".output"
        output.write_text("")
        env = os.environ | {"TAG": tag, "REVIEWED_SHA": reviewed or self.main, "GITHUB_OUTPUT": str(output)}
        result = subprocess.run([str(SCRIPT)], cwd=self.repo, env=env, capture_output=True, text=True)
        return result, output.read_text()

    def test_outputs_the_release_commit(self):
        for tag in ("1.2.3", "1.2.4"):
            with self.subTest(tag=tag):
                result, output = self.resolve(tag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, f"sha={self.release}\nversion={tag}\n")

    def test_rejects_a_tag_outside_the_reviewed_history(self):
        result, output = self.resolve("9.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not on the history of reviewed commit", result.stderr)
        self.assertEqual(output, "")

    def test_rejects_missing_tags_and_branches(self):
        for tag in ("3.0.0", "2.0.0"):
            with self.subTest(tag=tag):
                result, _ = self.resolve(tag)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("does not exist", result.stderr)

    def test_rejects_invalid_inputs_before_reading_git(self):
        for tag, reviewed in (("v1.2.3", None), ("1.2", None), ("01.2.3", None), ("1.2.3-rc.1", None),
                              ("1.2.3", "main"), ("1.2.3", "a" * 39)):
            with self.subTest(tag=tag, reviewed=reviewed):
                result, output = self.resolve(tag, reviewed)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "")

    def test_rejects_an_unknown_reviewed_commit(self):
        result, _ = self.resolve("1.2.3", "b" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not in the checkout", result.stderr)


if __name__ == "__main__":
    unittest.main()
