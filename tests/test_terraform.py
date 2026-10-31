import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from helpers import ROOT, write_executable


class TerraformTests(unittest.TestCase):
    def fake_terraform_tools(self, directory, fmt=0, plan=0, apply=0):
        # Fake terraform and tflint record their arguments and return the given statuses.
        tools = Path(directory) / "tools"
        tools.mkdir()
        log = Path(directory) / "commands.log"
        caches = Path(directory) / "plugin-caches.log"
        write_executable(
            tools / "terraform",
            f'echo "terraform $*" >> "{log}"\n'
            'case " $* " in\n'
            f'  " fmt "*) exit {fmt} ;;\n'
            f'  *" init "*) echo "$TF_PLUGIN_CACHE_DIR" >> "{caches}" ;;\n'
            '  *" plan "*)\n'
            '    for arg in "$@"; do\n'
            '      case "$arg" in -out=*) printf "synthetic plan\\n" > "${arg#-out=}" ;; esac\n'
            '    done\n'
            f'    exit {plan} ;;\n'
            f'  *" apply "*) exit {apply} ;;\n'
            'esac',
        )
        write_executable(tools / "tflint", f'echo "tflint $*" >> "{log}"')
        return tools, log

    def run_terraform_quality(self, repository, tools, roots, run_tests=None, extra_env=None):
        env = os.environ | {"PATH": f"{tools}:{os.environ['PATH']}"}
        for name in ["TERRAFORM_ROOTS", "TERRAFORM_RUN_TESTS", "TF_PLUGIN_CACHE_DIR"]:
            env.pop(name, None)
        if roots is not None:
            env["TERRAFORM_ROOTS"] = roots
        if run_tests is not None:
            env["TERRAFORM_RUN_TESTS"] = run_tests
        env.update(extra_env or {})
        return subprocess.run(
            [str(ROOT / "scripts/terraform-quality")], cwd=repository, env=env, capture_output=True, text=True,
        )

    def make_terraform_repository(self, directory):
        repository = Path(directory) / "repository"
        for root in ["environments/prod", "environments/test", "modules/network"]:
            (repository / root).mkdir(parents=True)
        (repository / "environments/README.md").write_text("# not a root\n")
        (repository / ".tflint.hcl").write_text("")
        return repository.resolve()

    def recovery_outputs(self, output, root="environments/test", artifact=True):
        values = dict(line.split("=", 1) for line in output)
        self.assertEqual(values["errored-state-path"], f"{root}/errored.tfstate")
        if not artifact:
            self.assertNotIn("errored-state-artifact", values)
            return values
        prefix = f"errored-tfstate-{root.replace('/', '-')}-42-2-"
        name = values["errored-state-artifact"]
        self.assertTrue(name.startswith(prefix), name)
        self.assertRegex(name.removeprefix(prefix), r"\A[A-Za-z0-9]{16}\Z")
        return values

    def test_terraform_quality_checks_each_selected_root_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            tools, log = self.fake_terraform_tools(directory)
            result = self.run_terraform_quality(repository, tools, "environments/*\n\nmodules/network\n", run_tests="true")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = f"--config={repository}/.tflint.hcl"
            expected = ["terraform fmt -check -recursive -diff .", f"tflint --init {config}"]
            for root in ["environments/prod", "environments/test", "modules/network"]:
                expected += [
                    f"terraform -chdir={root} init -backend=false -lockfile=readonly",
                    f"terraform -chdir={root} validate",
                    f"tflint --chdir={root} {config} --format=compact",
                    f"terraform -chdir={root} test",
                ]
            self.assertEqual(log.read_text().splitlines(), expected)

    def test_terraform_quality_formats_hidden_directories_holding_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            for root in [".infra/prod", ".infra/test", "environments/.nested/root"]:
                (repository / root).mkdir(parents=True)
            tools, log = self.fake_terraform_tools(directory)
            result = self.run_terraform_quality(
                repository, tools, ".infra/*\nenvironments/.nested/root\nenvironments/test",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                log.read_text().splitlines()[0],
                "terraform fmt -check -recursive -diff . .infra environments/.nested",
            )

    def test_terraform_quality_shares_a_temporary_plugin_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            tools, _ = self.fake_terraform_tools(directory)
            caches = Path(directory) / "plugin-caches.log"
            runner_temp = Path(directory) / "runner temp"
            runner_temp.mkdir()
            result = self.run_terraform_quality(
                repository, tools, "environments/*", extra_env={"RUNNER_TEMP": str(runner_temp)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            (cache,) = set(caches.read_text().splitlines())
            self.assertEqual(Path(cache).parent, runner_temp)
            self.assertFalse(Path(cache).exists(), "the temporary plugin cache must be removed")

            caches.unlink()
            caller = Path(directory) / "caller cache"
            caller.mkdir()
            result = self.run_terraform_quality(
                repository, tools, "environments/*", extra_env={"TF_PLUGIN_CACHE_DIR": str(caller)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(caches.read_text().splitlines()), {str(caller)})
            self.assertTrue(caller.exists(), "a caller-owned plugin cache must be kept")

    def test_terraform_quality_tests_are_opt_in(self):
        action = yaml.safe_load((ROOT / "terraform-quality/action.yml").read_text())
        self.assertEqual(action["inputs"]["run-tests"]["default"], "false")
        for run_tests in [None, "false", "true", "TRUE", "yes", "$(id)"]:
            with self.subTest(run_tests=run_tests), tempfile.TemporaryDirectory() as directory:
                repository = self.make_terraform_repository(directory)
                tools, log = self.fake_terraform_tools(directory)
                result = self.run_terraform_quality(repository, tools, "environments/test", run_tests)
                if run_tests in [None, "false", "true"]:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual("-chdir=environments/test test" in log.read_text(), run_tests == "true")
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertFalse(log.exists())

    def test_terraform_quality_deduplicates_canonical_roots_in_selection_order(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            (repository / "environments/test-alias").symlink_to("test", target_is_directory=True)
            tools, log = self.fake_terraform_tools(directory)
            result = self.run_terraform_quality(
                repository, tools, "environments/prod\nenvironments/*\n./environments/test/\nmodules/network",
                run_tests="true",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([line for line in log.read_text().splitlines() if line.endswith(" validate")], [
                "terraform -chdir=environments/prod validate",
                "terraform -chdir=environments/test validate",
                "terraform -chdir=modules/network validate",
            ])
            self.assertEqual(log.read_text().count("-chdir=environments/test test"), 1)

    def test_terraform_roots_reject_external_symlinks_before_running(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            outside = Path(directory) / "repository-sibling"
            outside.mkdir()
            (repository / "environments/escape").symlink_to(outside, target_is_directory=True)
            tools, log = self.fake_terraform_tools(directory)
            for roots in ["environments/escape", "environments/escape/.", "environments/*"]:
                result = self.run_terraform_quality(repository, tools, roots)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("outside the repository", result.stderr)
                self.assertFalse(log.exists())
            for mode in ["plan", "apply"]:
                result, output = self.run_terraform_run(directory, tools, mode, "environments/escape")
                self.assertEqual(result.returncode, 1)
                self.assertIn("outside the repository", result.stderr)
                self.assertEqual(output, [])
                self.assertFalse(log.exists())

    def test_terraform_quality_validates_expanded_directory_names(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            tools, log = self.fake_terraform_tools(directory)
            for name in ["with space", "with\nnewline", "with;semicolon"]:
                with self.subTest(name=name):
                    invalid = repository / "environments" / name
                    invalid.mkdir()
                    result = self.run_terraform_quality(repository, tools, "environments/*")
                    self.assertEqual(result.returncode, 1)
                    self.assertFalse(log.exists())
                    invalid.rmdir()

    def test_terraform_quality_rejects_unsafe_or_missing_roots_before_running(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            tools, log = self.fake_terraform_tools(directory)
            for roots in [
                None, "", " \n", "/etc", "../outside", "environments/../..", "env;id", "env ironments",
                "$HOME", "missing/*", "environments/README.md",
            ]:
                with self.subTest(roots=roots):
                    result = self.run_terraform_quality(repository, tools, roots)
                    self.assertEqual(result.returncode, 1)
                    self.assertFalse(log.exists(), "no tool may run for rejected roots")
            (repository / ".tflint.hcl").unlink()
            result = self.run_terraform_quality(repository, tools, "environments/*")
            self.assertEqual(result.returncode, 1)
            self.assertIn(".tflint.hcl", result.stderr)
            self.assertFalse(log.exists())

    def test_terraform_quality_stops_when_formatting_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            tools, log = self.fake_terraform_tools(directory, fmt=3)
            result = self.run_terraform_quality(repository, tools, "environments/*")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(log.read_text().splitlines(), ["terraform fmt -check -recursive -diff ."])

    def run_terraform_version(self, directory, version=None, installed=None, wrapper=False):
        # PATH holds only the fake tools, so a real terraform cannot leak into the result.
        tools = Path(directory) / "version-tools"
        tools.mkdir()
        if installed is not None:
            write_executable(tools / "terraform", f'echo \'{{"terraform_version": "{installed}"}}\'')
        if wrapper:
            write_executable(tools / "terraform-bin", "exit 0")
        output = Path(directory) / "outputs"
        output.write_text("")
        env = {"PATH": str(tools), "GITHUB_OUTPUT": str(output)}
        if version is not None:
            env["TERRAFORM_VERSION"] = version
        result = subprocess.run(
            [shutil.which("bash"), str(ROOT / "scripts/terraform-version")], env=env, capture_output=True, text=True,
        )
        return result, dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_terraform_version_reuses_only_a_matching_unwrapped_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            _, outputs = self.run_terraform_version(directory)
            pinned = outputs["version"]
        self.assertRegex(pinned, r"\A[0-9]+\.[0-9]+\.[0-9]+\Z")
        cases = [
            (None, None, False, pinned, "true"),
            (None, pinned, False, pinned, "false"),
            ("", pinned, False, pinned, "false"),
            (None, "0.0.1", False, pinned, "true"),
            ("1.2.3-rc1", "1.2.3-rc1", False, "1.2.3-rc1", "false"),
            ("1.2.3", "1.2.30", False, "1.2.3", "true"),
            (None, pinned, True, pinned, "true"),
        ]
        for version, installed, wrapper, expected_version, expected_install in cases:
            with self.subTest(version=version, installed=installed, wrapper=wrapper):
                with tempfile.TemporaryDirectory() as directory:
                    result, outputs = self.run_terraform_version(directory, version, installed, wrapper)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(outputs, {"version": expected_version, "install": expected_install})

    def test_terraform_version_rejects_inexact_versions(self):
        for version in ["latest", "~> 1.15", "1.15", "v1.15.9", "1.15.9\ninstall=false", "$(id)"]:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                result, outputs = self.run_terraform_version(directory, version)
                self.assertEqual(result.returncode, 1)
                self.assertIn("exact version", result.stderr)
                self.assertEqual(outputs, {})

    def run_terraform_run(
        self, directory, tools, mode, root="environments/test", state_recovery="github-artifact", extra_env=None,
    ):
        runner_temp = Path(directory) / "runner temp"
        runner_temp.mkdir(exist_ok=True)
        output = Path(directory) / "outputs"
        output.write_text("")
        env = os.environ | {
            "PATH": f"{tools}:{os.environ['PATH']}", "TERRAFORM_ROOT": root, "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(runner_temp), "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2",
        }
        env.pop("TERRAFORM_STATE_RECOVERY", None)
        if state_recovery is not None:
            env["TERRAFORM_STATE_RECOVERY"] = state_recovery
        env.update(extra_env or {})
        result = subprocess.run(
            [str(ROOT / "scripts/terraform-run"), mode], cwd=Path(directory) / "repository",
            env=env, capture_output=True, text=True,
        )
        return result, output.read_text().splitlines()

    def test_terraform_plan_reports_changes_from_the_detailed_exit_code(self):
        for status, expected_code, expected_output in [
            (0, 0, ["changes=false"]), (2, 0, ["changes=true"]), (1, 1, []),
        ]:
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as directory:
                    self.make_terraform_repository(directory)
                    tools, log = self.fake_terraform_tools(directory, plan=status)
                    result, output = self.run_terraform_run(directory, tools, "plan", state_recovery=None)
                    self.assertEqual(result.returncode, expected_code, result.stderr)
                    self.assertEqual(output, expected_output)
                    self.assertEqual(log.read_text().splitlines(), [
                        "terraform -chdir=environments/test init -lockfile=readonly",
                        "terraform -chdir=environments/test plan -lock-timeout=5m -detailed-exitcode",
                    ])

    def test_terraform_apply_applies_exactly_the_saved_plan(self):
        for status, changes in [(0, "false"), (2, "true")]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                self.make_terraform_repository(directory)
                tools, log = self.fake_terraform_tools(directory, plan=status)
                result, output = self.run_terraform_run(directory, tools, "apply")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.recovery_outputs(output)["changes"], changes)
                init, plan, apply = log.read_text().splitlines()
                self.assertEqual(init, "terraform -chdir=environments/test init -lockfile=readonly")
                prefix = "terraform -chdir=environments/test plan -lock-timeout=5m -detailed-exitcode -out="
                self.assertTrue(plan.startswith(prefix), plan)
                saved = plan.removeprefix(prefix)
                self.assertTrue(saved.startswith(f"{directory}/runner temp/terraform-apply."), saved)
                self.assertEqual(apply, f"terraform -chdir=environments/test apply -lock-timeout=5m {saved}")
                self.assertFalse(Path(saved).parent.exists(), "the sensitive saved plan must be removed")

    def test_terraform_recovery_names_are_unique_per_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            self.make_terraform_repository(directory)
            tools, _ = self.fake_terraform_tools(directory, apply=1)
            names = set()
            for _ in range(2):
                result, output = self.run_terraform_run(directory, tools, "apply")
                self.assertEqual(result.returncode, 1, result.stderr)
                names.add(self.recovery_outputs(output)["errored-state-artifact"])
            self.assertEqual(len(names), 2, "each failed apply needs its own recovery artifact")
            self.assertEqual(list((Path(directory) / "runner temp").glob("terraform-apply.*")), [])

    def test_terraform_apply_without_upload_still_reports_the_emergency_state_path(self):
        with tempfile.TemporaryDirectory() as directory:
            self.make_terraform_repository(directory)
            tools, log = self.fake_terraform_tools(directory, apply=1)
            result, output = self.run_terraform_run(directory, tools, "apply", state_recovery="none")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.recovery_outputs(output, artifact=False)
            self.assertIn(" apply ", log.read_text())

    def test_terraform_apply_does_not_apply_after_a_failed_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            self.make_terraform_repository(directory)
            tools, log = self.fake_terraform_tools(directory, plan=1)
            result, output = self.run_terraform_run(directory, tools, "apply")
            self.assertEqual(result.returncode, 1)
            self.recovery_outputs(output)
            self.assertNotIn(" apply ", log.read_text())
            self.assertEqual(list((Path(directory) / "runner temp").glob("terraform-apply.*")), [])

    def test_terraform_apply_requires_an_explicit_recovery_policy_before_running(self):
        action = yaml.safe_load((ROOT / "terraform-apply/action.yml").read_text())
        self.assertIs(action["inputs"]["state-recovery"]["required"], True)
        self.assertNotIn("default", action["inputs"]["state-recovery"])
        with tempfile.TemporaryDirectory() as directory:
            self.make_terraform_repository(directory)
            tools, log = self.fake_terraform_tools(directory)
            for policy in [None, "", "false", "NONE", "GITHUB-ARTIFACT", "$(id)"]:
                with self.subTest(policy=policy):
                    result, output = self.run_terraform_run(directory, tools, "apply", state_recovery=policy)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("state-recovery", result.stderr)
                    self.assertEqual(output, [])
                    self.assertFalse(log.exists(), "no Terraform command may run without a recovery policy")

    def test_terraform_apply_canonicalizes_internal_symlinks_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            (repository / "alias").symlink_to("environments/test", target_is_directory=True)
            tools, log = self.fake_terraform_tools(directory)
            result, output = self.run_terraform_run(directory, tools, "apply", "./alias/.")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.recovery_outputs(output)
            self.assertNotIn("-chdir=./alias", log.read_text())

    def test_terraform_recovery_fixture_preserves_only_emergency_state_on_failure(self):
        for failure in ["state", "ordinary"]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                repository = self.make_terraform_repository(directory)
                tools, _ = self.fake_terraform_tools(directory)
                (tools / "terraform").write_bytes((ROOT / "tests/fixtures/terraform-recovery/terraform").read_bytes())
                result, output = self.run_terraform_run(
                    directory, tools, "apply", extra_env={"RECOVERY_FIXTURE_FAILURE": failure},
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.recovery_outputs(output)
                emergency = repository / "environments/test/errored.tfstate"
                self.assertEqual(emergency.exists(), failure == "state")
                if emergency.exists():
                    self.assertEqual(emergency.read_text(), '{"fixture":"not-real-state"}\n')
                self.assertEqual(list((Path(directory) / "runner temp").glob("terraform-apply.*")), [])

    def test_terraform_run_rejects_unsafe_requests_before_running(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self.make_terraform_repository(directory)
            tools, log = self.fake_terraform_tools(directory)
            cases = [
                (mode, root)
                for mode in ["plan", "apply"]
                for root in ["", "/etc", "../outside", "environments/test/../..", "missing", "env;id", "env test"]
            ] + [("destroy", "environments/test"), ("", "environments/test")]
            for mode, root in cases:
                with self.subTest(mode=mode, root=root):
                    result, output = self.run_terraform_run(directory, tools, mode, root)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(output, [])
                    self.assertFalse(log.exists(), "terraform must not run for rejected requests")
            (repository / "environments/test/errored.tfstate").write_text("{}")
            result, output = self.run_terraform_run(directory, tools, "apply")
            self.assertEqual(result.returncode, 1)
            self.assertIn("errored.tfstate", result.stderr)
            self.assertEqual(output, [])
            self.assertFalse(log.exists())
            (repository / "environments/test/errored.tfstate").unlink()
            (repository / "environments/test/errored.tfstate").symlink_to("missing-state")
            result, output = self.run_terraform_run(directory, tools, "apply")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(output, [])
            self.assertFalse(log.exists())

    def test_terraform_apply_limits_recovery_artifact_exposure(self):
        upload = yaml.safe_load((ROOT / "terraform-apply/action.yml").read_text())["runs"]["steps"][-1]
        self.assertTrue(upload["uses"].startswith("actions/upload-artifact@"))
        self.assertIn("steps.apply.outputs.errored-state-artifact != ''", upload["if"])
        self.assertEqual(upload["with"]["retention-days"], 3)
        self.assertIs(upload["with"]["overwrite"], False)

    def test_release_waits_for_all_consumer_jobs(self):
        workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(
            set(workflow["jobs"]["release"]["needs"]), {"automation", "consumers", "terraform", "terraform-recovery"},
        )


if __name__ == "__main__":
    unittest.main()
