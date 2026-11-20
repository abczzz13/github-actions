import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from helpers import ROOT, write_executable

SCRIPT = ROOT / "scripts/helm-dry-run"
EXISTS = '[{"name":"app","namespace":"app-test"}]'

# The release contains an upgrade-only Secret and a hook outside the manifest.
# Neither the release JSON nor its notes should reach the log or summary.
MANIFEST = """apiVersion: v1
kind: ConfigMap
metadata:
  name: app
data:
  lookup: from-cluster
  capability: cluster-only
---
apiVersion: v1
kind: Secret
metadata:
  name: upgrade-only
stringData:
  password: private-secret-value
"""
HELM_RESULT = json.dumps({
    "manifest": MANIFEST,
    "hooks": [{"manifest": "kind: Job\nmetadata:\n  name: hook-only\n"}],
    "info": {"notes": "private-release-notes"},
})
HELM = """printf '%s\\n' "$*" >> "$HELM_LOG"
case "$1" in
version) echo "$HELM_VERSION" ;;
list) printf '%s' "$HELM_LIST" ;;
history) printf '%s' "$HELM_HISTORY" ;;
upgrade) printf '%s' "$HELM_RESULT"; exit "$HELM_STATUS" ;;
*) echo 'Unexpected Helm command' >&2; exit 99 ;;
esac"""
KUBECTL = """printf '%s\\n' "$*" >> "$KUBECTL_LOG"
while [ "$#" -gt 0 ]; do
  if [ "$1" = --filename ]; then
    cp "$2" "$MANIFEST_LOG"
    stat -c '%a' "$(dirname "$2")" "$(dirname "$2")/release.json" "$2" > "$MODES_LOG"
    break
  fi
  shift
done
printf '%s' "$DIFF"
exit "$DIFF_STATUS\""""


class HelmDryRunTests(unittest.TestCase):
    def run_script(self, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            write_executable(directory / "helm", HELM)
            write_executable(directory / "kubectl", KUBECTL)
            chart = directory / "chart"
            chart.mkdir()
            (chart / "Chart.yaml").write_text("name: app\n")
            (chart / "values-test.yaml").write_text("{}\n")
            work = directory / "work"
            work.mkdir()
            files = {name: directory / name for name in
                     ("summary", "output", "helm.log", "kubectl.log", "manifest.log", "modes.log")}
            for path in files.values():
                path.touch()
            env = os.environ | {
                "PATH": f"{directory}:{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(files["summary"]),
                "GITHUB_OUTPUT": str(files["output"]), "HELM_LOG": str(files["helm.log"]),
                "KUBECTL_LOG": str(files["kubectl.log"]), "HELM_VERSION": "v4.2.4",
                "HELM_RESULT": HELM_RESULT, "HELM_STATUS": "0", "TMPDIR": str(work),
                "HELM_LIST": "[]", "HELM_HISTORY": "[]",
                "MANIFEST_LOG": str(files["manifest.log"]), "MODES_LOG": str(files["modes.log"]),
                "RELEASE": "app", "CHART": str(chart), "NAMESPACE": "app-test",
                "VALUES": f"{chart}/values-test.yaml\n", "SET_STRING": "image.digest=sha256:abc\nimage.version=1.2.3",
                "DIFF": "", "DIFF_STATUS": "0",
            } | overrides
            result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(list(work.iterdir()), [], "private Helm files must be removed on success and failure")
            read = {name: path.read_text() for name, path in files.items()}
            return result, read, str(chart)

    def test_dry_runs_and_diffs_without_installing(self):
        result, read, chart = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        common = (f"app {chart} --namespace app-test --values {chart}/values-test.yaml "
                  "--set-string image.digest=sha256:abc --set-string image.version=1.2.3")
        self.assertEqual(read["helm.log"].splitlines(), [
            "version --template {{ .Version }}",
            "list --namespace app-test --filter ^app$ --output json",
            f"upgrade --install {common} --take-ownership --dry-run=server --output json",
        ])
        self.assertRegex(read["kubectl.log"], r"^diff --namespace app-test --server-side --force-conflicts "
                                              r"--field-manager=helm --show-secrets=false --filename \S+\n$")
        self.assertEqual(read["output"], "changed=false\n")
        self.assertIn("The chart matches the live objects.", read["summary"])
        self.assertIn("Checked in `adopt` mode", read["summary"])

    def test_uses_the_ownership_rules_of_helm_deploy(self):
        # Without --take-ownership after a successful revision, the dry run
        # fails like the deploy when an upgrade would take over a foreign object.
        cases = (
            ('[{"revision":1,"status":"failed"}]', True, "Checked in `adopt` mode"),
            ('[{"revision":1,"status":"deployed"}]', False, "Checked in `upgrade` mode"),
            ('[{"revision":1,"status":"superseded"},{"revision":2,"status":"failed"}]', False,
             "Checked in `upgrade` mode"),
        )
        for history, adopts, summary in cases:
            with self.subTest(history=history):
                result, read, _ = self.run_script(HELM_LIST=EXISTS, HELM_HISTORY=history)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("history app --namespace app-test --output json", read["helm.log"])
                (command,) = [line for line in read["helm.log"].splitlines() if line.startswith("upgrade")]
                self.assertEqual("--take-ownership" in command, adopts)
                self.assertNotIn("--rollback-on-failure", command)
                self.assertIn(summary, read["summary"])

    def test_rejects_an_uninstalled_release_like_helm_deploy(self):
        result, read, _ = self.run_script(
            HELM_LIST=EXISTS, HELM_HISTORY='[{"revision":1,"status":"deployed"},{"revision":2,"status":"uninstalled"}]')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uninstalled with retained history", result.stderr)
        self.assertNotIn("upgrade", read["helm.log"])
        self.assertEqual(read["kubectl.log"], "")

    def test_diffs_the_exact_server_manifest_including_secrets_but_not_hooks(self):
        result, read, _ = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read["manifest.log"], MANIFEST)
        self.assertEqual(read["modes.log"].splitlines(), ["700", "600", "600"])
        for private in ("private-secret-value", "private-release-notes", "hook-only"):
            self.assertNotIn(private, result.stdout + result.stderr + read["summary"])

    def test_stops_after_a_helm_failure_without_logging_the_release(self):
        result, read, _ = self.run_script(HELM_STATUS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(read["kubectl.log"], "")
        self.assertEqual(read["output"] + read["summary"], "")
        self.assertNotIn("private-secret-value", result.stdout + result.stderr)

    def test_rejects_invalid_release_json_before_calling_kubectl(self):
        for release in ("not-json", "{}", '{"manifest":null}', '{"manifest":42}'):
            with self.subTest(release=release):
                result, read, _ = self.run_script(HELM_RESULT=release)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Helm returned no release manifest", result.stderr)
                self.assertEqual(read["kubectl.log"], "")
                self.assertEqual(read["output"] + read["summary"], "")

    def test_reports_differences_without_failing(self):
        result, read, _ = self.run_script(DIFF="-old\n+new", DIFF_STATUS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read["output"], "changed=true\n")
        self.assertIn("```diff\n-old\n+new\n```", read["summary"])

    def test_fails_when_the_server_rejects_the_objects(self):
        result, read, _ = self.run_script(DIFF_STATUS="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("kubectl diff exit 2", result.stderr)
        self.assertEqual(read["summary"], "")

    def test_requires_helm_4(self):
        result, read, _ = self.run_script(HELM_VERSION="v3.20.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Helm 4 is required", result.stderr)
        self.assertNotIn("upgrade", read["helm.log"])

    def test_rejects_invalid_inputs_before_calling_helm(self):
        for overrides in ({"RELEASE": "App"}, {"NAMESPACE": ""}, {"CHART": "/nonexistent"},
                          {"VALUES": "missing.yaml"}, {"SET_STRING": "a=b,c=d"}, {"SET_STRING": "novalue"}):
            with self.subTest(**overrides):
                result, read, _ = self.run_script(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(read["helm.log"], "")

    def test_legacy_kube_version_file_does_not_override_server_rendering(self):
        result, read, _ = self.run_script(KUBE_VERSION_FILE="/nonexistent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read["manifest.log"], MANIFEST)
        self.assertNotIn("--kube-version", read["helm.log"])

    def test_never_installs(self):
        script = SCRIPT.read_text()
        commands = [line for line in script.splitlines() if line.lstrip().startswith("helm upgrade")]
        self.assertEqual(len(commands), 1)
        self.assertIn("--dry-run=server", commands[0])
        for flag in ("--atomic", "--rollback-on-failure", "--wait"):
            self.assertNotIn(flag, script)


if __name__ == "__main__":
    unittest.main()
