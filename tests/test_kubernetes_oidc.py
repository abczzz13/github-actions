import base64
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import yaml

from helpers import ROOT, write_executable

SETUP = ROOT / "scripts/kubernetes-oidc"
CREDENTIAL = ROOT / "scripts/kubernetes-oidc-credential"
REQUEST_TOKEN = "request-token"
EXP = 1_800_000_000
CLAIMS = {
    "sub": "repo:owner/app:environment:test",
    "job_workflow_ref": "owner/app/.github/workflows/deploy.yml@refs/heads/main",
    "iat": EXP - 300,
    "exp": EXP,
    # Encodes to base64url with '-' or '_' and needs padding when decoded.
    "pad": "??>>~~",
}
CA_PEM = (ROOT / "tests/fixtures/kubernetes-oidc/ca.pem").read_text()


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


HEADER = b64url(b'{"alg":"RS256"}')


def jwt(claims):
    return f"{HEADER}.{b64url(json.dumps(claims).encode())}.c2ln"


class TokenService:
    """Fake GitHub ID token endpoint. It records each requested audience."""

    def __init__(self):
        self.audiences = []
        # Requests that fail with 503 before the service answers with status.
        self.failures = 0
        self.status = 200
        self.body = json.dumps({"value": jwt(CLAIMS)})
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                if self.headers.get("Authorization") != f"bearer {REQUEST_TOKEN}" or query.get("api-version") != ["2.0"]:
                    self.send_error(403)
                    return
                service.audiences.append(query.get("audience", [None])[0])
                if service.failures:
                    service.failures -= 1
                    self.send_error(503)
                    return
                self.send_response(service.status)
                self.end_headers()
                body = service.body() if callable(service.body) else service.body
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/token?api-version=2.0"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class KubernetesOidcTests(unittest.TestCase):
    def setUp(self):
        self.tokens = TokenService()
        self.addCleanup(self.tokens.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def env(self, **overrides):
        github_env, github_output = self.directory / "env", self.directory / "output"
        github_env.touch()
        github_output.touch()
        return os.environ | {
            "ACTIONS_ID_TOKEN_REQUEST_URL": self.tokens.url, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQUEST_TOKEN,
            "RUNNER_TEMP": str(self.directory), "GITHUB_ENV": str(github_env), "GITHUB_OUTPUT": str(github_output),
            "SERVER": "https://api.example.com:6443", "CA_DATA": base64.b64encode(CA_PEM.encode()).decode(),
            "AUDIENCE": "infra-k8s-apps", "NAMESPACE": "app-test",
        } | overrides

    def run_setup(self, **overrides):
        env = {key: value for key, value in self.env(**overrides).items() if value is not None}
        return subprocess.run([str(SETUP)], env=env, capture_output=True, text=True)

    def test_writes_a_private_exec_kubeconfig_and_exports_it(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = (self.directory / "output").read_text()
        self.assertRegex(output, r"^kubeconfig=.+/kubernetes-oidc\.[^/]+/config\n$")
        path = Path(output.strip().split("=", 1)[1])
        self.assertEqual((self.directory / "env").read_text(), f"KUBECONFIG={path}\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        config = yaml.safe_load(path.read_text())
        self.assertEqual(config["clusters"][0]["cluster"], {
            "server": "https://api.example.com:6443",
            "certificate-authority-data": self.env()["CA_DATA"],
        })
        self.assertEqual(config["users"][0]["user"]["exec"], {
            "apiVersion": "client.authentication.k8s.io/v1",
            # Absolute, so the plugin runs from any working directory.
            "command": str(CREDENTIAL.resolve()),
            "args": ["infra-k8s-apps"],
            "interactiveMode": "Never",
            "provideClusterInfo": False,
        })
        self.assertEqual(config["contexts"][0]["context"]["namespace"], "app-test")
        self.assertEqual(config["current-context"], "github-actions")
        # The kubeconfig holds no token, so it never goes stale or leaks one.
        self.assertNotIn(jwt(CLAIMS), path.read_text())

    def test_requests_one_token_up_front_and_logs_only_claims(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tokens.audiences, ["infra-k8s-apps"])
        self.assertIn(
            "ID token: sub=repo:owner/app:environment:test "
            "job_workflow_ref=owner/app/.github/workflows/deploy.yml@refs/heads/main lifetime=300s",
            result.stdout,
        )
        for part in jwt(CLAIMS).split("."):
            self.assertNotIn(part, result.stdout + result.stderr)

    def test_fixture_payload_needs_base64url_decoding_and_padding(self):
        payload = jwt(CLAIMS).split(".")[1]
        self.assertRegex(payload, "[-_]")
        self.assertNotEqual(len(payload) % 4, 0)

    def test_omits_the_namespace_when_empty(self):
        result = self.run_setup(NAMESPACE="")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = (self.directory / "output").read_text().strip().split("=", 1)[1]
        self.assertNotIn("namespace", yaml.safe_load(Path(path).read_text())["contexts"][0]["context"])

    def test_credential_prints_a_fresh_exec_credential_that_expires_early(self):
        for _ in range(2):
            result = subprocess.run([str(CREDENTIAL), "aud:with/chars&x=y #"], env=self.env(), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            credential = json.loads(result.stdout)
            self.assertEqual(credential["kind"], "ExecCredential")
            self.assertEqual(credential["apiVersion"], "client.authentication.k8s.io/v1")
            self.assertEqual(credential["status"]["token"], jwt(CLAIMS))
            expected = datetime.fromtimestamp(EXP - 30, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.assertEqual(credential["status"]["expirationTimestamp"], expected)
        # Each call requests a new token; the audience survives URL encoding.
        self.assertEqual(self.tokens.audiences, ["aud:with/chars&x=y #"] * 2)

    def test_credential_retries_a_failed_token_request(self):
        self.tokens.failures = 1
        result = subprocess.run([str(CREDENTIAL), "aud"], env=self.env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"]["token"], jwt(CLAIMS))
        self.assertEqual(self.tokens.audiences, ["aud"] * 2)

    def test_credential_keeps_the_request_token_off_the_command_line(self):
        bin_directory, arguments = self.directory / "bin", self.directory / "curl-arguments"
        bin_directory.mkdir()
        # Records the arguments, then runs the real curl.
        write_executable(bin_directory / "curl", f'printf "%s\\n" "$@" >>"{arguments}"\nexec "{shutil.which("curl")}" "$@"')
        env = self.env(PATH=f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
        result = subprocess.run([str(CREDENTIAL), "aud"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("@-", arguments.read_text().splitlines())
        self.assertNotIn(REQUEST_TOKEN, arguments.read_text())

    def test_credential_requires_id_token_permission(self):
        result = subprocess.run([str(CREDENTIAL), "aud"], env=self.env(ACTIONS_ID_TOKEN_REQUEST_URL=""),
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("id-token: write", result.stderr)

    def test_fails_on_token_service_errors_and_invalid_tokens(self):
        for status, body in [
            (500, "{}"),
            (200, "not json"),
            (200, json.dumps({"value": ""})),
            (200, json.dumps({"value": "not.a-jwt"})),
            (200, json.dumps({"value": jwt({"sub": "no-exp"})})),
            (200, json.dumps({"value": jwt({"exp": "soon"})})),
        ]:
            with self.subTest(status=status, body=body):
                self.tokens.status, self.tokens.body = status, body
                result = self.run_setup()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.directory / "env").read_text(), "")
                self.assertEqual(list(self.directory.glob("kubernetes-oidc.*")), [])

    def test_accepts_valid_servers(self):
        for server in [
            "https://api.example.com", "https://api.example.com:6443/prefix",
            "https://localhost", "https://127.0.0.1:443", "https://[::1]:6443",
            "https://api.example.com.",
        ]:
            with self.subTest(server=server):
                result = self.run_setup(SERVER=server)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepts_a_bundle_and_line_wrapped_base64(self):
        bundle = (CA_PEM + CA_PEM).encode()
        result = self.run_setup(CA_DATA=base64.encodebytes(bundle).decode().replace("\n", "\r\n"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_inputs_before_requesting_a_token(self):
        not_pem = base64.b64encode(b"not a certificate").decode()
        incomplete_pem = "-----BEGIN CERTIFICATE-----\nnot a certificate"
        invalid_pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        for name, value in [
            ("SERVER", "http://api.example.com:6443"),
            ("SERVER", "https://user:pass@api.example.com"),
            ("SERVER", "https://@api.example.com"),
            ("SERVER", "https://api.example.com/?q=1"),
            ("SERVER", "https://api.example.com/?"),
            ("SERVER", "https://api.example.com/#"),
            ("SERVER", "https://:invalid-port"),
            ("SERVER", "https://api.example.com:invalid-port"),
            ("SERVER", "https://api.example.com:65536"),
            ("SERVER", "https://[not-an-ip]:6443"),
            ("SERVER", "https://[127.0.0.1]:6443"),
            ("SERVER", "https:///path"),
            ("SERVER", ""),
            ("CA_DATA", "%%%"),
            ("CA_DATA", not_pem),
            ("CA_DATA", base64.b64encode(incomplete_pem.encode()).decode()),
            ("CA_DATA", base64.b64encode(invalid_pem.encode()).decode()),
            ("CA_DATA", base64.b64encode((CA_PEM + invalid_pem).encode()).decode()),
            ("AUDIENCE", ""),
            ("AUDIENCE", "a b"),
            ("AUDIENCE", "a&b=c"),
            ("NAMESPACE", "App"),
            ("NAMESPACE", "-app"),
            ("NAMESPACE", "a" * 64),
            ("RUNNER_TEMP", None),
        ]:
            with self.subTest(name=name, value=value):
                result = self.run_setup(**{name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.tokens.audiences, [])
                self.assertEqual((self.directory / "env").read_text(), "")
                self.assertEqual((self.directory / "output").read_text(), "")
                self.assertEqual(list(self.directory.glob("kubernetes-oidc.*")), [])

    def test_action_passes_inputs_through_the_environment_only(self):
        action = yaml.safe_load((ROOT / "kubernetes-oidc/action.yml").read_text())
        (step,) = action["runs"]["steps"]
        self.assertEqual(step["run"], '"$ACTION_PATH/../scripts/kubernetes-oidc"')
        self.assertEqual(set(step["env"]), {"ACTION_PATH", "SERVER", "CA_DATA", "AUDIENCE", "NAMESPACE"})


@unittest.skipUnless(shutil.which("kubectl") and shutil.which("openssl"), "requires kubectl and openssl")
class KubectlTests(unittest.TestCase):
    """Proves that a real client runs the plugin and sends its token."""

    def setUp(self):
        self.tokens = TokenService()
        self.addCleanup(self.tokens.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        cert, key = self.directory / "tls.crt", self.directory / "tls.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], check=True, capture_output=True)
        self.seen = []
        self.expected_token = lambda: jwt(CLAIMS)
        test = self

        class Api(BaseHTTPRequestHandler):
            def do_GET(self):
                test.seen.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != f"Bearer {test.expected_token()}":
                    self.send_error(401)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"gitVersion":"v0.0.0-fixture"}')

            def log_message(self, *args):
                pass

        api = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        api.socket = context.wrap_socket(api.socket, server_side=True)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)

        self.github_env, self.github_output = self.directory / "env", self.directory / "output"
        self.github_env.touch()
        self.github_output.touch()
        self.env = os.environ | {
            "ACTIONS_ID_TOKEN_REQUEST_URL": self.tokens.url, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQUEST_TOKEN,
            "RUNNER_TEMP": str(self.directory), "GITHUB_ENV": str(self.github_env),
            "GITHUB_OUTPUT": str(self.github_output), "SERVER": f"https://127.0.0.1:{api.server_port}",
            "CA_DATA": base64.b64encode(cert.read_bytes()).decode(),
            "AUDIENCE": "infra-k8s-apps", "NAMESPACE": "",
        }

    def setup_kubeconfig(self):
        subprocess.run([str(SETUP)], env=self.env, check=True, capture_output=True, timeout=10)
        return self.github_output.read_text().strip().split("=", 1)[1]

    def test_kubectl_authenticates_with_the_plugin_token(self):
        kubeconfig = self.setup_kubeconfig()
        result = subprocess.run(["kubectl", "--kubeconfig", kubeconfig, "get", "--raw", "/version"],
                                env=self.env, capture_output=True, text=True, cwd="/", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"gitVersion": "v0.0.0-fixture"})
        self.assertEqual(self.seen, [f"Bearer {jwt(CLAIMS)}"])
        # One token at setup and one requested by kubectl through the plugin.
        self.assertEqual(self.tokens.audiences, ["infra-k8s-apps"] * 2)

    def test_kubectl_renews_credentials_in_one_process(self):
        issued = []

        def issue_token():
            now = int(time.time())
            # The plugin's early expiry is 2–3 seconds away, while the JWT stays valid.
            claims = CLAIMS | {"iat": now, "exp": now + 33, "jti": str(len(issued))}
            token = jwt(claims)
            issued.append({"token": token, "exp": claims["exp"]})
            return json.dumps({"value": token})

        self.tokens.body = issue_token
        self.expected_token = lambda: issued[-1]["token"] if time.time() < issued[-1]["exp"] else ""
        kubeconfig = self.setup_kubeconfig()
        proxy_socket = self.directory / "proxy.sock"
        # A single proxy process reuses client-go's credential cache across HTTP requests.
        proxy = subprocess.Popen(
            ["kubectl", "--kubeconfig", kubeconfig, "proxy", "--unix-socket", str(proxy_socket)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def stop_proxy():
            proxy.terminate()
            try:
                proxy.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.communicate()

        self.addCleanup(stop_proxy)
        deadline = time.monotonic() + 10
        while not proxy_socket.exists() and proxy.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(proxy_socket.exists(), "kubectl proxy did not create its socket")

        def request_version():
            connection = HTTPConnection("localhost", timeout=5)
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(5)
            try:
                connection.sock.connect(str(proxy_socket))
                connection.request("GET", "/version")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"gitVersion": "v0.0.0-fixture"})
            finally:
                connection.close()

        request_version()
        self.assertEqual(len(issued), 2)  # Setup plus the first proxy request.
        first = issued[-1]
        time.sleep(max(0, first["exp"] - 30 - time.time() + 0.1))
        request_version()
        self.assertIsNone(proxy.poll())
        self.assertLess(time.time(), first["exp"])
        self.assertEqual(len(issued), 3)
        self.assertNotEqual(first["token"], issued[-1]["token"])
        self.assertEqual(self.seen, [f"Bearer {first['token']}", f"Bearer {issued[-1]['token']}"])
        self.assertEqual(self.tokens.audiences, ["infra-k8s-apps"] * 3)


if __name__ == "__main__":
    unittest.main()
