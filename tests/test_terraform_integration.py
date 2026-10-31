from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from helpers import ROOT


@unittest.skipUnless(shutil.which("terraform"), "Install Terraform to run the backend recovery integration test.")
class TerraformIntegrationTests(unittest.TestCase):
    def test_failed_backend_write_preserves_real_state_and_removes_saved_plan(self):
        writes = []

        class RejectingBackend(BaseHTTPRequestHandler):
            def do_GET(self):
                # An absent state starts a new, disposable workspace.
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                writes.append(json.loads(body))
                self.send_response(500)
                self.end_headers()

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as directory, ThreadingHTTPServer(("127.0.0.1", 0), RejectingBackend) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                repository = Path(directory) / "repository"
                root = repository / ".infra"
                root.mkdir(parents=True)
                (root / "main.tf").write_text(
                    'terraform {\n  backend "http" {}\n}\n'
                    'resource "terraform_data" "example" {\n  input = "synthetic"\n}\n'
                )
                runner_temp = Path(directory) / "runner temp"
                runner_temp.mkdir()
                output = Path(directory) / "outputs"
                # Caller flags, workspaces and backend credentials must not affect this fixture.
                env = {key: value for key, value in os.environ.items() if not key.startswith("TF_")}
                env.update({
                    "TERRAFORM_ROOT": ".infra",
                    "TERRAFORM_STATE_RECOVERY": "github-artifact",
                    "TF_HTTP_ADDRESS": f"http://127.0.0.1:{server.server_port}/state",
                    "TF_HTTP_RETRY_MAX": "0",
                    "CHECKPOINT_DISABLE": "1",
                    "GITHUB_OUTPUT": str(output),
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_RUN_ID": "42",
                    "GITHUB_RUN_ATTEMPT": "2",
                })
                result = subprocess.run(
                    [str(ROOT / "scripts/terraform-run"), "apply"], cwd=repository, env=env,
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("Failed to persist state to backend", result.stderr)
                self.assertTrue(writes, "Terraform must try to save actual state to the backend")
                recovered = json.loads((root / "errored.tfstate").read_text())
                self.assertEqual(recovered, writes[-1])
                self.assertEqual(len(recovered["resources"]), 1)
                resource = recovered["resources"][0]
                self.assertEqual((resource["type"], resource["name"]), ("terraform_data", "example"))
                self.assertEqual(resource["instances"][0]["attributes"]["input"]["value"], "synthetic")
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual(values["errored-state-path"], ".infra/errored.tfstate")
                self.assertRegex(values["errored-state-artifact"], r"\Aerrored-tfstate-\.infra-42-2-[A-Za-z0-9]{16}\Z")
                self.assertEqual(list(runner_temp.iterdir()), [], "the sensitive saved plan must be removed")
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
