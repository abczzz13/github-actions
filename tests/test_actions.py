import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from commitizen.exceptions import ExitCode
import yaml


ROOT = Path(__file__).resolve().parents[1]


def write_executable(path, body):
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


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

    def test_ci_separates_events_and_checks_pr_titles(self):
        # BaseLoader preserves GitHub's "on" key rather than YAML 1.1's boolean.
        workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn("${{ github.event_name }}", workflow["concurrency"]["group"])
        self.assertEqual(
            workflow["concurrency"]["cancel-in-progress"],
            "${{ github.event_name == 'pull_request' }}",
        )
        self.assertTrue(
            {"opened", "synchronize", "reopened", "edited"}
            <= set(workflow["on"]["pull_request"]["types"])
        )
        steps = workflow["jobs"]["automation"]["steps"]
        policy = next(step for step in steps if step.get("uses") == "./commit-policy")
        self.assertEqual(policy["if"], "github.event_name == 'pull_request'")
        self.assertEqual(policy["with"]["message"], "${{ github.event.pull_request.title }}")
        self.assertIn("automation", workflow["jobs"]["release"]["needs"])

    def test_go_quality_fails_on_diff_even_when_formatter_returns_zero(self):
        for output, exit_code, expected in [("", 0, 0), ("formatting diff", 0, 1), ("", 2, 1)]:
            with self.subTest(output=output, exit_code=exit_code):
                with tempfile.TemporaryDirectory() as directory:
                    write_executable(
                        Path(directory) / "golangci-lint",
                        f'if [ "$1" != fmt ]; then exit 0; fi\nprintf \'%s\' \'{output}\'\nexit {exit_code}',
                    )
                    env = os.environ | {"PATH": f"{directory}:{os.environ['PATH']}"}
                    result = subprocess.run([str(ROOT / "scripts/go-quality")], env=env)
                    self.assertEqual(result.returncode, expected)

    def test_commitizen_has_one_shared_hash_pinned_definition(self):
        source = (ROOT / "scripts/commitizen-requirements.in").read_text()
        self.assertRegex(source, r"\Acommitizen==[0-9]+\.[0-9]+\.[0-9]+\n\Z")
        lock = (ROOT / "scripts/commitizen-requirements.txt").read_text()
        self.assertIn(source.strip() + " \\\n    --hash=sha256:", lock)
        self.assertIn("--require-hashes", (ROOT / "scripts/with-commitizen").read_text())
        for action in ["commit-policy", "commitizen-bump"]:
            with self.subTest(action=action):
                text = (ROOT / action / "action.yml").read_text()
                self.assertIn('/scripts/with-commitizen"', text)
                self.assertNotIn("commitizen_version", text)

    def test_check_treats_message_as_data(self):
        for message, expected in [
            ("feat: add sharing", 0),
            ("fix: reject $(touch /tmp/commit-policy-injection)", 0),
            ("invalid title", ExitCode.INVALID_COMMIT_MSG),
        ]:
            with self.subTest(message=message):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/commitizen_release.py"), "check"],
                    env=os.environ | {"COMMIT_MESSAGE": message}, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

    def run_semgrep(self, directory, rules):
        # A fake docker records its arguments instead of running the scanner.
        log = Path(directory) / "docker.log"
        log.unlink(missing_ok=True)
        write_executable(Path(directory) / "docker", f'printf \'%s\\n\' "$@" > "{log}"')
        env = os.environ | {"PATH": f"{directory}:{os.environ['PATH']}"}
        if rules is not None:
            env["SEMGREP_RULES"] = rules
        else:
            env.pop("SEMGREP_RULES", None)
        result = subprocess.run([str(ROOT / "scripts/semgrep")], env=env, capture_output=True, text=True)
        return result, log.read_text().splitlines() if log.exists() else []

    def test_semgrep_only_mounts_relative_rule_paths_from_pinned_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            result, arguments = self.run_semgrep(directory, "go/lang/security/injection\n\ngo/jwt-go\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                [arguments[index + 1] for index, argument in enumerate(arguments) if argument == "--config"],
                ["/src/.semgrep-rules/go/lang/security/injection", "/src/.semgrep-rules/go/jwt-go"],
            )
            self.assertIn(f"{Path.cwd()}:/src:ro", arguments)
            for rules in [None, "", " \n", "/etc/passwd", "../outside", "go/lang;id", "go lang", "$HOME"]:
                with self.subTest(rules=rules):
                    result, arguments = self.run_semgrep(directory, rules)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(arguments, [], "docker must not run for rejected rules")

    def test_lint_automation_selects_supported_shell_sources_only(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
            files = {
                "suffix.sh": "echo suffix",
                "bash-shebang": "#!/usr/bin/env bash\necho bash",
                "sh-shebang": "#!/bin/sh\necho sh",
                "dash-shebang": "#!/usr/bin/dash\necho dash",
                "fish-shebang": "#!/usr/bin/fish\necho fish",
                "zsh-shebang": "#!/bin/zsh\necho zsh",
                "python-shebang": "#!/usr/bin/env python3\nprint()",
                "README.md": "# not a script",
                ".semgrep-rules/rule.sh": "echo reserved rule checkout",
                "vendor/module.sh": "echo vendored",
            }
            for name, content in files.items():
                path = repository / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content + "\n")
            tools = Path(directory) / "tools"
            tools.mkdir()
            log = Path(directory) / "shellcheck.log"
            write_executable(tools / "actionlint", "exit 0")
            write_executable(tools / "shellcheck", f'printf \'%s\\n\' "$@" > "{log}"')
            env = os.environ | {"PATH": f"{tools}:{os.environ['PATH']}", "SHELLCHECK_EXCLUDES": "SC2034,SC2154"}
            subprocess.run([str(ROOT / "scripts/lint-automation")], cwd=repository, env=env, check=True)
            self.assertEqual(
                sorted(log.read_text().splitlines()),
                ["--exclude=SC2034,SC2154", "bash-shebang", "dash-shebang", "sh-shebang", "suffix.sh"],
            )


if __name__ == "__main__":
    unittest.main()
