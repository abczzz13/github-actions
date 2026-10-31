import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from commitizen.exceptions import ExitCode
import yaml

from helpers import ROOT, write_executable


def go_quality_steps():
    return yaml.safe_load((ROOT / "go-quality/action.yml").read_text())["runs"]["steps"]


class ActionTests(unittest.TestCase):
    def test_actions_are_self_contained_and_sha_pinned(self):
        actions = sorted(ROOT.glob("*/action.yml"))
        self.assertEqual(len(actions), 9)
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

    def test_go_quality_disables_builtin_caching(self):
        steps = go_quality_steps()
        setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-go@"))
        lint = next(step for step in steps if step.get("uses", "").startswith("golangci/golangci-lint-action@"))
        self.assertIs(setup["with"]["cache"], False)
        self.assertIs(lint["with"]["skip-cache"], True)

    def test_go_quality_separates_dependency_and_per_commit_cache_keys(self):
        restores = {
            step["id"]: step["with"] for step in go_quality_steps()
            if step.get("uses", "").startswith("actions/cache/restore@")
        }
        self.assertEqual(set(restores), {"modules-cache", "analysis-cache"})
        platform = "${{ runner.os }}-${{ runner.arch }}-"
        self.assertEqual(
            restores["modules-cache"]["key"],
            "go-quality-modules-" + platform + "${{ hashFiles(inputs.go-version-file, '**/go.sum') }}",
        )
        self.assertEqual(
            restores["analysis-cache"]["key"],
            "go-quality-analysis-" + platform + "${{ hashFiles(inputs.go-version-file) }}-${{ github.sha }}",
        )

    def test_go_quality_orders_cache_restore_fallbacks(self):
        restores = {
            step["id"]: step["with"] for step in go_quality_steps()
            if step.get("uses", "").startswith("actions/cache/restore@")
        }
        platform = "${{ runner.os }}-${{ runner.arch }}-"
        self.assertEqual(
            restores["modules-cache"]["restore-keys"].splitlines(),
            ["go-quality-modules-" + platform],
        )
        prefix = "go-quality-analysis-" + platform
        self.assertEqual(
            restores["analysis-cache"]["restore-keys"].splitlines(),
            [prefix + "${{ hashFiles(inputs.go-version-file) }}-", prefix],
        )

    def test_go_quality_saves_reuse_restore_configuration_after_quality_checks(self):
        steps = go_quality_steps()
        restores = {step["id"]: step for step in steps if step.get("uses", "").startswith("actions/cache/restore@")}
        self.assertEqual(restores["modules-cache"]["with"]["path"], "${{ steps.cache-paths.outputs.go-mod }}")
        self.assertEqual(
            restores["analysis-cache"]["with"]["path"].splitlines(),
            ["${{ steps.cache-paths.outputs.go-build }}", "${{ steps.cache-paths.outputs.golangci-lint }}"],
        )
        saves = [step for step in steps if step.get("uses", "").startswith("actions/cache/save@")]
        self.assertEqual(len(saves), 2)
        quality_index = next(index for index, step in enumerate(steps) if "/scripts/go-quality" in step.get("run", ""))
        for save, cache_id in zip(saves, ["modules-cache", "analysis-cache"], strict=True):
            with self.subTest(cache=cache_id):
                self.assertLess(steps.index(restores[cache_id]), quality_index)
                self.assertGreater(steps.index(save), quality_index)
                self.assertEqual(save["with"]["path"], restores[cache_id]["with"]["path"])
                self.assertEqual(save["with"]["key"], "${{ steps." + cache_id + ".outputs.cache-primary-key }}")
                self.assertEqual(save["uses"].split("@", 1)[1], restores[cache_id]["uses"].split("@", 1)[1])

    def test_go_quality_cache_save_eligibility(self):
        saves = [step for step in go_quality_steps() if step.get("uses", "").startswith("actions/cache/save@")]
        cases = [
            ("default-branch push", {}, True),
            ("failed quality checks", {"succeeded": False}, False),
            ("exact cache hit", {"cache_hit": "true"}, False),
            ("cache miss", {"cache_hit": ""}, True),
            ("feature-branch push", {"ref": "refs/heads/feature"}, False),
            ("tag push", {"ref": "refs/tags/1.0.0"}, False),
            ("PR merge ref", {"event": "pull_request", "ref": "refs/pull/1/merge"}, False),
            ("PR default-branch ref", {"event": "pull_request"}, False),
            ("PR target default-branch base ref", {"event": "pull_request_target"}, False),
            ("PR target feature-branch base ref", {"event": "pull_request_target", "ref": "refs/heads/feature"}, False),
            ("manual default-branch run", {"event": "workflow_dispatch"}, True),
            ("manual feature-branch run", {"event": "workflow_dispatch", "ref": "refs/heads/feature"}, False),
            ("scheduled default-branch run", {"event": "schedule"}, True),
            ("non-main default branch", {"default_branch": "trunk", "ref": "refs/heads/trunk"}, True),
            ("main is not the default branch", {"default_branch": "trunk"}, False),
        ]
        for save, cache_id in zip(saves, ["modules-cache", "analysis-cache"], strict=True):
            for name, overrides, expected in cases:
                with self.subTest(cache=cache_id, case=name):
                    context = {
                        "event": "push", "ref": "refs/heads/main", "default_branch": "main",
                        "succeeded": True, "cache_hit": "false",
                    } | overrides
                    # Evaluate the guard's conjunction, rejecting unsupported clauses.
                    # This is deliberately not a general GitHub expression interpreter.
                    checks = {
                        "success()": context["succeeded"],
                        "github.event_name != 'pull_request'": context["event"] != "pull_request",
                        "github.event_name != 'pull_request_target'": context["event"] != "pull_request_target",
                        "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)":
                            context["ref"] == f"refs/heads/{context['default_branch']}",
                        f"steps.{cache_id}.outputs.cache-hit != 'true'": context["cache_hit"] != "true",
                    }
                    clauses = [clause.strip() for clause in save["if"].split("&&")]
                    self.assertLessEqual(set(clauses), checks.keys())
                    self.assertEqual(all(checks[clause] for clause in clauses), expected)

    def test_go_quality_discovers_cache_paths_with_environment_overrides(self):
        steps = go_quality_steps()
        locate = next(step for step in steps if step.get("id") == "cache-paths")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            write_executable(
                Path(directory) / "go",
                '[ "$1" = env ] || exit 2\n'
                'case "$2" in\n'
                '  GOCACHE) printf "%s\\n" "$HOME/build cache" ;;\n'
                '  GOMODCACHE) printf "%s\\n" "$HOME/module cache" ;;\n'
                '  *) exit 2 ;;\n'
                'esac',
            )
            env = os.environ | {
                "PATH": f"{directory}:{os.environ['PATH']}",
                "HOME": f"{directory}/home with spaces",
                "GITHUB_OUTPUT": str(output),
            }
            env.pop("GOLANGCI_LINT_CACHE", None)
            env.pop("XDG_CACHE_HOME", None)
            for overrides, expected in [
                ({}, f"{env['HOME']}/.cache/golangci-lint"),
                ({"XDG_CACHE_HOME": "/custom cache"}, "/custom cache/golangci-lint"),
                ({"GOLANGCI_LINT_CACHE": "/lint cache", "XDG_CACHE_HOME": "/ignored"}, "/lint cache"),
                ({"GOLANGCI_LINT_CACHE": "", "XDG_CACHE_HOME": "/custom cache"}, "/custom cache/golangci-lint"),
                ({"GOLANGCI_LINT_CACHE": "", "XDG_CACHE_HOME": ""}, f"{env['HOME']}/.cache/golangci-lint"),
            ]:
                with self.subTest(overrides=overrides):
                    output.write_text("")
                    subprocess.run(
                        [locate["shell"], "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", locate["run"]],
                        env=env | overrides, check=True,
                    )
                    self.assertEqual(
                        dict(line.split("=", 1) for line in output.read_text().splitlines()),
                        {
                            "go-build": f"{env['HOME']}/build cache",
                            "go-mod": f"{env['HOME']}/module cache",
                            "golangci-lint": expected,
                        },
                    )

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

    def test_terraform_actions_share_one_install_definition(self):
        def install_steps(action):
            return yaml.safe_load((ROOT / action / "action.yml").read_text())["runs"]["steps"][:2]

        select, setup = install_steps("terraform-quality")
        for action in ["terraform-plan", "terraform-apply"]:
            with self.subTest(action=action):
                self.assertEqual(install_steps(action), [select, setup])
        self.assertEqual(select["id"], "terraform")
        self.assertEqual(setup["if"], "steps.terraform.outputs.install == 'true'")
        self.assertTrue(setup["uses"].startswith("hashicorp/setup-terraform@"))
        self.assertEqual(setup["with"]["terraform_version"], "${{ steps.terraform.outputs.version }}")
        self.assertIs(setup["with"]["terraform_wrapper"], False)


if __name__ == "__main__":
    unittest.main()
