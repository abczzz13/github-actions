import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from helpers import ROOT, write_executable

SCRIPT = ROOT / "scripts/helm-deploy"

# The release JSON holds the rendered manifest, including Secrets. Only the
# revision and status may reach the log, outputs or summary.
RELEASE = json.dumps({
    "version": 3,
    "info": {"status": "deployed", "notes": "private-release-notes"},
    "manifest": "kind: Secret\nstringData:\n  password: private-secret-value\n",
})
# After the upgrade, helm history prints HELM_HISTORY_AFTER, or fails like
# Helm for a missing release when it is empty.
HELM = """printf '%s\\n' "$*" >> "$HELM_LOG"
case "$1" in
version) echo "$HELM_VERSION" ;;
list) printf '%s' "$HELM_LIST" ;;
history)
    if ! grep -q '^upgrade' "$HELM_LOG"; then printf '%s' "$HELM_HISTORY"
    elif [ -n "$HELM_HISTORY_AFTER" ]; then printf '%s' "$HELM_HISTORY_AFTER"
    else echo 'Error: release: not found' >&2; exit 1; fi ;;
upgrade)
    printf '%s' "$HELM_RESULT"
    if [ "$HELM_STATUS" -ne 0 ]; then printf '%s\\n' "$HELM_ERROR" >&2; fi
    exit "$HELM_STATUS" ;;
*) echo 'Unexpected Helm command' >&2; exit 99 ;;
esac"""
EXISTS = '[{"name":"app","namespace":"app-test"}]'


def history(*statuses):
    # Helm puts error text in the description, which can include rendered
    # objects. It must never reach the log or summary.
    return json.dumps([{"revision": index, "status": status, "description": "private-secret-value"}
                       for index, status in enumerate(statuses, 1)])


class HelmDeployTests(unittest.TestCase):
    def run_script(self, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            write_executable(directory / "helm", HELM)
            chart = directory / "chart"
            chart.mkdir()
            (chart / "Chart.yaml").write_text("name: app\n")
            (chart / "values-test.yaml").write_text("{}\n")
            work = directory / "work"
            work.mkdir()
            files = {name: directory / name for name in ("summary", "output", "helm.log")}
            for path in files.values():
                path.touch()
            env = os.environ | {
                "PATH": f"{directory}:{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(files["summary"]),
                "GITHUB_OUTPUT": str(files["output"]), "HELM_LOG": str(files["helm.log"]),
                "HELM_VERSION": "v4.2.4", "HELM_LIST": "[]", "HELM_HISTORY": "[]", "HELM_HISTORY_AFTER": "",
                "HELM_RESULT": RELEASE, "HELM_STATUS": "0", "HELM_ERROR": "Error: upgrade failed", "TMPDIR": str(work),
                "RELEASE": "app", "CHART": str(chart), "NAMESPACE": "app-test",
                "VALUES": f"{chart}/values-test.yaml\n", "SET_STRING": "image.digest=sha256:abc\nimage.version=1.2.3",
                "TIMEOUT": "15m",
            } | overrides
            result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(list(work.iterdir()), [], "private Helm files must be removed on success and failure")
            read = {name: path.read_text() for name, path in files.items()}
            for private in ("private-secret-value", "private-release-notes"):
                self.assertNotIn(private, result.stdout + result.stderr + read["summary"] + read["output"])
            return result, read, str(chart)

    def upgrade_command(self, read):
        (command,) = [line for line in read["helm.log"].splitlines() if line.startswith("upgrade")]
        return command

    def test_first_install_adopts_without_rollback(self):
        result, read, chart = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read["helm.log"].splitlines(), [
            "version --template {{ .Version }}",
            "list --namespace app-test --filter ^app$ --output json",
            f"upgrade --install app {chart} --namespace app-test --values {chart}/values-test.yaml "
            "--set-string image.digest=sha256:abc --set-string image.version=1.2.3 "
            "--server-side=true --force-conflicts --wait --timeout 15m --take-ownership --output json",
        ])
        self.assertEqual(read["output"], "revision=3\nmode=adopt\n")
        self.assertIn("as revision 3 with `--take-ownership`", read["summary"])

    def test_release_without_a_successful_revision_keeps_adopting(self):
        result, read, _ = self.run_script(HELM_LIST=EXISTS, HELM_HISTORY=history("failed", "failed"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("history app --namespace app-test --output json", read["helm.log"])
        command = self.upgrade_command(read)
        self.assertIn("--take-ownership", command)
        self.assertNotIn("--rollback-on-failure", command)
        self.assertEqual(read["output"], "revision=3\nmode=adopt\n")

    def test_rejects_retained_uninstalled_releases_before_deploying(self):
        histories = (
            history("uninstalled"),
            history("superseded", "uninstalled"),
            json.dumps(list(reversed(json.loads(history("superseded", "uninstalled"))))),
        )
        for retained in histories:
            with self.subTest(history=retained):
                result, read, _ = self.run_script(HELM_LIST=EXISTS, HELM_HISTORY=retained)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("uninstalled with retained history", result.stderr)
                self.assertNotIn("upgrade", read["helm.log"])
                self.assertEqual(read["output"] + read["summary"], "")

    def test_upgrade_after_a_successful_revision_rolls_back_without_adopting(self):
        for statuses in (("deployed",), ("failed", "superseded", "failed"), ("uninstalled", "deployed")):
            with self.subTest(statuses=statuses):
                result, read, _ = self.run_script(HELM_LIST=EXISTS, HELM_HISTORY=history(*statuses))
                self.assertEqual(result.returncode, 0, result.stderr)
                command = self.upgrade_command(read)
                self.assertIn("--rollback-on-failure", command)
                self.assertNotIn("--take-ownership", command)
                self.assertEqual(read["output"], "revision=3\nmode=upgrade\n")
                self.assertIn("Upgraded `app` in `app-test` to revision 3", read["summary"])

    def test_failure_reports_the_state_that_helm_left(self):
        cases = (
            ("adopt without a release", {}, "",
             "No release exists after the failure, so Helm applied no objects."),
            ("adopt that recorded a failed revision", {}, history("failed"),
             "Helm did not roll back or uninstall. Objects that Helm already applied stay in place."),
            ("upgrade that failed before it changed the release", {"HELM_HISTORY": history("deployed")},
             history("deployed"), "Helm failed before it changed the release. Revision 1 still has status `deployed`."),
            ("upgrade that rolled back", {"HELM_HISTORY": history("deployed")},
             history("superseded", "failed", "deployed"),
             "Helm rolled back to the last successful revision. A hook that already ran"),
            ("upgrade whose rollback failed", {"HELM_HISTORY": history("deployed")},
             history("deployed", "failed"), "Helm did not complete the rollback."),
            ("upgrade left pending", {"HELM_HISTORY": history("deployed")},
             history("deployed", "pending-upgrade"), 'See "Pending releases" in the Helm deploy contract'),
            ("history unreadable after the failure", {"HELM_HISTORY": history("deployed")}, "not-json",
             "The release history could not be read after the failure."),
        )
        for name, before, after, message in cases:
            with self.subTest(name):
                result, read, _ = self.run_script(HELM_LIST=EXISTS if before else "[]", HELM_STATUS="1",
                                                  HELM_ERROR="Error: upgrade failed", HELM_HISTORY_AFTER=after,
                                                  **before)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Error: upgrade failed", result.stderr)
                self.assertEqual(read["output"], "")
                self.assertIn("failed.** Read the Helm error in the step log.", read["summary"])
                self.assertIn(message, read["summary"])
                if "rolled back" not in name:
                    self.assertNotIn("Helm rolled back", read["summary"])

    def test_rejects_a_release_that_is_not_deployed(self):
        pending = json.dumps({"version": 3, "info": {"status": "pending-upgrade"}})
        result, read, _ = self.run_script(HELM_RESULT=pending)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pending-upgrade", result.stderr)
        self.assertEqual(read["output"], "")

    def test_rejects_invalid_release_json(self):
        for release in ("not-json", "{}", '{"version":"3","info":{"status":"deployed"}}'):
            with self.subTest(release=release):
                result, read, _ = self.run_script(HELM_RESULT=release)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(read["output"], "")

    def test_fails_when_the_release_state_cannot_be_read(self):
        for overrides in ({"HELM_LIST": "not-json"}, {"HELM_LIST": EXISTS, "HELM_HISTORY": "not-json"},
                          {"HELM_LIST": EXISTS, "HELM_HISTORY": "[]"},
                          {"HELM_LIST": EXISTS, "HELM_HISTORY": '[{"revision":1}]'}):
            with self.subTest(**overrides):
                result, read, _ = self.run_script(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("upgrade", read["helm.log"])

    def test_requires_helm_4(self):
        result, read, _ = self.run_script(HELM_VERSION="v3.20.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Helm 4 is required", result.stderr)
        self.assertNotIn("upgrade", read["helm.log"])

    def test_rejects_invalid_inputs_before_changing_anything(self):
        for overrides in ({"RELEASE": "App"}, {"NAMESPACE": ""}, {"CHART": "/nonexistent"},
                          {"VALUES": "missing.yaml"}, {"SET_STRING": "a=b,c=d"}, {"TIMEOUT": ""},
                          {"TIMEOUT": "15"}, {"TIMEOUT": "0m"}, {"TIMEOUT": "1m;id"}):
            with self.subTest(**overrides):
                result, read, _ = self.run_script(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("upgrade", read["helm.log"])


if __name__ == "__main__":
    unittest.main()
