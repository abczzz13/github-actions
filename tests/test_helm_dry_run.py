import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from helpers import ROOT, write_executable

SCRIPT = ROOT / "scripts/helm-dry-run"

# helm records its arguments and renders one object; kubectl diff prints
# $DIFF and exits with $DIFF_STATUS.
HELM = """printf '%s\\n' "$*" >> "$HELM_LOG"
case "$1" in
version) echo "$HELM_VERSION" ;;
template) echo 'kind: ConfigMap' ;;
esac"""
KUBECTL = """printf '%s\\n' "$*" >> "$KUBECTL_LOG"
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
            (chart / "kubernetes-version").write_text("1.35.0\n")
            files = {name: directory / name for name in ("summary", "output", "helm.log", "kubectl.log")}
            for path in files.values():
                path.touch()
            env = os.environ | {
                "PATH": f"{directory}:{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(files["summary"]),
                "GITHUB_OUTPUT": str(files["output"]), "HELM_LOG": str(files["helm.log"]),
                "KUBECTL_LOG": str(files["kubectl.log"]), "HELM_VERSION": "v4.2.4",
                "RELEASE": "app", "CHART": str(chart), "NAMESPACE": "app-test",
                "VALUES": f"{chart}/values-test.yaml\n", "SET_STRING": "image.digest=sha256:abc\nimage.version=1.2.3",
                "KUBE_VERSION_FILE": str(chart / "kubernetes-version"), "DIFF": "", "DIFF_STATUS": "0",
            } | overrides
            result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
            read = {name: path.read_text() for name, path in files.items()}
            return result, read, str(chart)

    def test_dry_runs_and_diffs_without_installing(self):
        result, read, chart = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        common = (f"app {chart} --namespace app-test --values {chart}/values-test.yaml "
                  "--set-string image.digest=sha256:abc --set-string image.version=1.2.3")
        self.assertEqual(read["helm.log"].splitlines(), [
            "version --template {{ .Version }}",
            f"upgrade --install {common} --take-ownership --dry-run=server --hide-secret",
            f"template {common} --no-hooks --kube-version 1.35.0",
        ])
        self.assertRegex(read["kubectl.log"], r"^diff --namespace app-test --server-side --force-conflicts "
                                              r"--field-manager=helm --filename \S+\n$")
        self.assertEqual(read["output"], "changed=false\n")
        self.assertIn("The chart matches the live objects.", read["summary"])

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
                          {"VALUES": "missing.yaml"}, {"SET_STRING": "a=b,c=d"}, {"SET_STRING": "novalue"},
                          {"KUBE_VERSION_FILE": "/nonexistent"}):
            with self.subTest(**overrides):
                result, read, _ = self.run_script(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(read["helm.log"], "")

    def test_kube_version_is_optional(self):
        result, read, _ = self.run_script(KUBE_VERSION_FILE="")
        self.assertEqual(result.returncode, 0, result.stderr)
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
