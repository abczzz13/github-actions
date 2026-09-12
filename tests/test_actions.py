import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ActionTests(unittest.TestCase):
    def test_actions_are_self_contained_and_sha_pinned(self):
        actions = sorted(ROOT.glob("*/action.yml"))
        self.assertEqual(len(actions), 6)
        for path in actions:
            with self.subTest(action=path.parent.name):
                action = yaml.safe_load(path.read_text())
                self.assertEqual(action["runs"]["using"], "composite")
                for step in action["runs"]["steps"]:
                    if "uses" in step:
                        self.assertRegex(step["uses"], r"^[\w/-]+@[0-9a-f]{40}$")
                    if "run" in step:
                        script = step["run"]
                        self.assertNotIn("${{", script)
                        self.assertLessEqual(len(script.splitlines()), 3)
                        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh") as shell:
                            shell.write(script)
                            shell.flush()
                            subprocess.run(["shellcheck", "--shell=bash", shell.name], check=True)

    def test_format_diff_fails_even_when_formatter_returns_zero(self):
        for output, exit_code, expected in [("", 0, 0), ("formatting diff", 0, 1), ("", 2, 1)]:
            with self.subTest(output=output, exit_code=exit_code):
                with tempfile.TemporaryDirectory() as directory:
                    executable = Path(directory) / "golangci-lint"
                    executable.write_text(
                        '#!/bin/sh\nif [ "$1" = config ]; then exit 0; fi\n'
                        f"printf '%s' '{output}'\nexit {exit_code}\n"
                    )
                    executable.chmod(0o755)
                    env = os.environ | {"PATH": f"{directory}:{os.environ['PATH']}"}
                    result = subprocess.run([str(ROOT / "scripts/check-go-format")], env=env)
                    self.assertEqual(result.returncode, expected)

    def test_commitizen_has_one_shared_pin(self):
        requirements = (ROOT / "scripts/commitizen-requirements.txt").read_text()
        self.assertRegex(requirements, r"\Acommitizen==[0-9]+\.[0-9]+\.[0-9]+\n\Z")
        for action in ["commit-policy", "commitizen-bump"]:
            with self.subTest(action=action):
                text = (ROOT / action / "action.yml").read_text()
                self.assertIn('/scripts/with-commitizen"', text)
                self.assertNotIn("commitizen_version", text)

    def test_check_treats_message_as_data(self):
        for message, expected in [
            ("feat: add sharing", 0),
            ("fix: reject $(touch /tmp/commit-policy-injection)", 0),
            ("invalid title", 14),
        ]:
            with self.subTest(message=message):
                result = subprocess.run(
                    [os.sys.executable, str(ROOT / "scripts/commitizen_release.py"), "check"],
                    env=os.environ | {"COMMIT_MESSAGE": message}, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
