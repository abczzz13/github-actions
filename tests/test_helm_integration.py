import base64
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Thread
import unittest
from urllib.parse import urlsplit

from helpers import ROOT, write_executable


@unittest.skipUnless(shutil.which("helm"), "Helm is not installed")
class HelmIntegrationTests(unittest.TestCase):
    def test_diffs_the_real_server_render_for_installs_and_upgrades(self):
        version = subprocess.run(["helm", "version", "--template", "{{ .Version }}"],
                                 check=True, capture_output=True, text=True).stdout
        if not version.startswith("v4."):
            self.skipTest("Helm 4 is required")

        requests = []
        # Status of the stored revision 1, or None when no release exists. Real
        # helm list and helm history read it, so this also checks the JSON
        # fields that helm_release_mode depends on.
        stored = None

        def encode(status):
            release = {
                "name": "app", "namespace": "app-test", "version": 1,
                "info": {"status": status},
                "chart": {"metadata": {"name": "app", "version": "1.0.0", "apiVersion": "v2"}},
                "manifest": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app\n",
            }
            return base64.b64encode(gzip.compress(json.dumps(release).encode())).decode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                requests.append(path)
                status = 200
                if path == "/version":
                    result = {"major": "1", "minor": "35", "gitVersion": "v1.35.0"}
                elif path == "/api":
                    result = {"kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"]}
                elif path == "/apis":
                    result = {"kind": "APIGroupList", "apiVersion": "v1", "groups": []}
                elif path == "/api/v1":
                    result = {
                        "kind": "APIResourceList", "apiVersion": "v1", "groupVersion": "v1",
                        "resources": [{"name": name, "kind": kind, "namespaced": True,
                                       "verbs": ["get", "list", "create", "patch", "update"]}
                                      for name, kind in (("configmaps", "ConfigMap"), ("secrets", "Secret"))],
                    }
                elif path == "/api/v1/namespaces/app-test/configmaps":
                    item = {
                        "kind": "ConfigMap", "apiVersion": "v1",
                        "metadata": {"name": "sh.helm.release.v1.app.v1", "namespace": "app-test",
                                     "labels": {"name": "app", "owner": "helm", "status": stored, "version": "1"}},
                        "data": {"release": encode(stored)} if stored else {},
                    }
                    result = {"kind": "ConfigMapList", "apiVersion": "v1", "items": [item] if stored else []}
                elif path == "/api/v1/namespaces/app-test/configmaps/source":
                    result = {"kind": "ConfigMap", "apiVersion": "v1", "metadata": {"name": "source"},
                              "data": {"value": "from-cluster"}}
                elif path == "/api/v1/namespaces/app-test/configmaps/app":
                    result = {"kind": "ConfigMap", "apiVersion": "v1", "metadata": {"name": "app"}}
                elif path == "/openapi/v3":
                    result = {"paths": {}}
                elif path == "/openapi/v2":
                    # An empty protobuf OpenAPI document is enough for this
                    # rendering test. Schema and admission checks are not tested.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    return
                else:
                    status = 404
                    result = {"kind": "Status", "apiVersion": "v1", "status": "Failure",
                              "reason": "NotFound", "code": 404}
                body = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                requests.append(f"WRITE {self.path}")
                self.send_error(500, "This test must not write to the cluster")

            do_PATCH = do_POST
            do_PUT = do_POST
            do_DELETE = do_POST

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                chart = directory / "chart"
                (chart / "templates").mkdir(parents=True)
                (chart / "Chart.yaml").write_text("apiVersion: v2\nname: app\nversion: 1.0.0\n")
                (chart / "templates/app.yaml").write_text('''apiVersion: v1
kind: ConfigMap
metadata:
  name: app
data:
  mode: {{ ternary "upgrade" "install" .Release.IsUpgrade | quote }}
  source: {{ (lookup "v1" "ConfigMap" .Release.Namespace "source").data.value | quote }}
  capability: {{ .Capabilities.APIVersions.Has "v1/ConfigMap" | quote }}
{{- if .Release.IsUpgrade }}
---
apiVersion: v1
kind: Secret
metadata:
  name: upgrade-only
stringData:
  password: private-integration-secret
{{- end }}
''')
                (chart / "templates/hook.yaml").write_text('''apiVersion: v1
kind: ConfigMap
metadata:
  name: hook-only
  annotations:
    helm.sh/hook: pre-install,pre-upgrade
''')
                kubeconfig = directory / "config"
                kubeconfig.write_text(json.dumps({
                    "apiVersion": "v1", "kind": "Config",
                    "clusters": [{"name": "test", "cluster": {"server": f"http://127.0.0.1:{server.server_port}"}}],
                    "users": [{"name": "test", "user": {}}],
                    "contexts": [{"name": "test", "context": {"cluster": "test", "user": "test"}}],
                    "current-context": "test",
                }))
                kubeconfig.chmod(0o600)
                tools = directory / "tools"
                tools.mkdir()
                manifest = directory / "manifest"
                write_executable(tools / "kubectl", '''while [ "$#" -gt 0 ]; do
  if [ "$1" = --filename ]; then cp "$2" "$MANIFEST_LOG"; exit 0; fi
  shift
done
exit 2''')
                env = os.environ | {
                    "PATH": f"{tools}:{os.environ['PATH']}", "KUBECONFIG": str(kubeconfig),
                    "HELM_DRIVER": "configmap", "RELEASE": "app", "CHART": str(chart), "NAMESPACE": "app-test",
                    "VALUES": "", "SET_STRING": "", "MANIFEST_LOG": str(manifest),
                    "GITHUB_OUTPUT": str(directory / "output"), "GITHUB_STEP_SUMMARY": str(directory / "summary"),
                }
                summary = directory / "summary"
                for stored, deploy_mode in ((None, "adopt"), ("failed", "adopt"), ("deployed", "upgrade")):
                    with self.subTest(stored=stored):
                        summary.write_text("")
                        result = subprocess.run([str(ROOT / "scripts/helm-dry-run")], env=env,
                                                capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        rendered = manifest.read_text()
                        installed = stored is not None
                        mode = "upgrade" if installed else "install"
                        self.assertIn(f'mode: "{mode}"', rendered)
                        self.assertIn('source: "from-cluster"', rendered)
                        self.assertIn('capability: "true"', rendered)
                        self.assertEqual("upgrade-only" in rendered, installed)
                        self.assertNotIn("hook-only", rendered)
                        self.assertNotIn("private-integration-secret", result.stdout + result.stderr)
                        self.assertIn(f"Checked in `{deploy_mode}` mode", summary.read_text())
                with self.subTest(stored="uninstalled"):
                    stored = "uninstalled"
                    result = subprocess.run([str(ROOT / "scripts/helm-dry-run")], env=env,
                                            capture_output=True, text=True, timeout=30)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("uninstalled with retained history", result.stderr)
                self.assertIn("/api/v1/namespaces/app-test/configmaps/source", requests)
                self.assertFalse(any(path.startswith("WRITE ") for path in requests), requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
