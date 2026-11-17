import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

import yaml

from helpers import ROOT, write_executable

BOUNDARY = ROOT / "scripts/tailnet-boundary-check"
IDENTITY = ROOT / "scripts/kubernetes-identity-check"
TAG = "tag:k8s-controlplane"
USER = "github:repo:owner@1/app@2:environment:test"


def listener(address):
    """A listening socket accepts TCP connections without calling accept()."""
    sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET)
    try:
        sock.bind((address, 0))
        sock.listen()
    except OSError:
        sock.close()
        raise
    return sock


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.bin = Path(self.directory.name)
        self.sockets = []
        self.calls = self.bin / "tailscale.calls"
        # The control plane and an operator device, on separate loopback addresses.
        self.api = self.listen("127.0.0.2")
        self.closed = self.free_port("127.0.0.2")

    def tearDown(self):
        for sock in self.sockets:
            sock.close()
        self.directory.cleanup()

    def listen(self, address):
        sock = listener(address)
        self.sockets.append(sock)
        return sock.getsockname()[1]

    def listen_ipv6(self):
        try:
            return self.listen("::1")
        except OSError as error:
            self.skipTest(f"IPv6 loopback is unavailable: {error}")

    def free_port(self, address):
        sock = listener(address)
        port = sock.getsockname()[1]
        sock.close()
        return port

    def check(self, peers, **overrides):
        status = {"Peer": {str(index): peer for index, peer in enumerate(peers)}}
        self.calls.write_text("")
        write_executable(self.bin / "tailscale", f"echo called >> '{self.calls}'\ncat <<'JSON'\n{json.dumps(status)}\nJSON")
        env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}", "CONTROL_PLANE_TAG": TAG,
            "CONTROL_PLANE_PORT": str(self.api), "CONTROL_PLANE_DENIED_PORTS": str(self.closed),
            "PEER_DENIED_PORTS": str(self.closed),
        } | overrides
        return subprocess.run([str(BOUNDARY)], env=env, capture_output=True, text=True, timeout=60)

    def control_plane(self, address="127.0.0.2"):
        return {"DNSName": "cp-1.", "Tags": [TAG], "TailscaleIPs": [address]}

    def test_accepts_a_reachable_control_plane_and_closed_peers(self):
        device = {"DNSName": "laptop.", "TailscaleIPs": ["127.0.0.3"]}
        result = self.check([self.control_plane(), device, {"DNSName": "offline.", "TailscaleIPs": None}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"cp-1.: TCP {self.closed} denied; {self.api} reachable.", result.stdout)
        self.assertIn(f"laptop.: TCP {self.closed} denied.", result.stdout)
        self.assertIn("offline.: no Tailscale IP, not probed.", result.stdout)

    def test_rejects_a_reachable_peer(self):
        open_port = self.listen("127.0.0.3")
        device = {"DNSName": "laptop.", "Tags": ["tag:other"], "TailscaleIPs": ["127.0.0.3"]}
        result = self.check([self.control_plane(), device], PEER_DENIED_PORTS=f"{self.closed} {open_port}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"laptop. (127.0.0.3) is reachable on TCP {open_port}.", result.stderr)

    def test_rejects_a_reachable_control_plane_admin_port(self):
        talos = self.listen("127.0.0.2")
        result = self.check([self.control_plane()], CONTROL_PLANE_DENIED_PORTS=str(talos))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"is reachable on TCP {talos}.", result.stderr)

    def test_requires_a_reachable_control_plane(self):
        result = self.check([{"DNSName": "laptop.", "TailscaleIPs": ["127.0.0.3"]}])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"No {TAG} peer is visible", result.stderr)
        result = self.check([self.control_plane()], CONTROL_PLANE_PORT=str(self.closed))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"is not reachable on TCP {self.closed}.", result.stderr)

    def test_probes_peers_without_a_dns_name(self):
        open_port = self.listen("127.0.0.3")
        for name in ("", None):
            with self.subTest(name=name):
                device = {"DNSName": name, "TailscaleIPs": ["127.0.0.3"]}
                result = self.check([self.control_plane(), device], PEER_DENIED_PORTS=str(open_port))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"unnamed peer (127.0.0.3) is reachable on TCP {open_port}", result.stderr)

    def test_accepts_a_control_plane_without_a_dns_name(self):
        node = self.control_plane() | {"DNSName": ""}
        result = self.check([node])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{self.api} reachable", result.stdout)

    def test_rejects_a_peer_reachable_only_on_its_second_ipv6_address(self):
        open_port = self.listen_ipv6()
        device = {"DNSName": "dual-stack.", "TailscaleIPs": ["127.0.0.3", "::1"]}
        result = self.check([self.control_plane(), device], PEER_DENIED_PORTS=str(open_port))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"dual-stack. (::1) is reachable on TCP {open_port}", result.stderr)

    def test_rejects_a_control_plane_admin_port_on_its_second_ipv6_address(self):
        open_port = self.listen_ipv6()
        node = self.control_plane() | {"TailscaleIPs": ["127.0.0.2", "::1"]}
        result = self.check([node], CONTROL_PLANE_DENIED_PORTS=str(open_port))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"cp-1. (::1) is reachable on TCP {open_port}", result.stderr)

    def test_accepts_an_ipv6_only_control_plane(self):
        api = self.listen_ipv6()
        result = self.check([self.control_plane("::1")], CONTROL_PLANE_PORT=str(api))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_control_plane_needs_only_one_working_api_address(self):
        self.listen_ipv6()
        node = self.control_plane() | {"TailscaleIPs": ["::1", "127.0.0.2"]}
        result = self.check([node])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{self.api} reachable", result.stdout)

    def test_rejects_out_of_range_ports_before_reading_tailscale(self):
        for name in ("CONTROL_PLANE_PORT", "CONTROL_PLANE_DENIED_PORTS", "PEER_DENIED_PORTS"):
            for value in ("0", "00000", "65536", "99999"):
                with self.subTest(name=name, value=value):
                    result = self.check([self.control_plane()], **{name: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("must be between 1 and 65535", result.stderr)
                    self.assertEqual(self.calls.read_text(), "")
        result = self.check([self.control_plane()], PEER_DENIED_PORTS="22 99999")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text(), "")

    def test_accepts_range_endpoints_and_leading_zeroes(self):
        # No peer is needed to check input validation; status must still be read.
        result = self.check([], CONTROL_PLANE_PORT="00001", PEER_DENIED_PORTS="00022 65535")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No tag:k8s-controlplane peer is visible", result.stderr)
        self.assertEqual(self.calls.read_text(), "called\n")

    def test_a_node_with_extra_tags_is_not_a_control_plane(self):
        node = self.control_plane() | {"Tags": [TAG, "tag:other"]}
        result = self.check([node])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is visible", result.stderr)

    def test_rejects_invalid_inputs(self):
        for name, value in (("CONTROL_PLANE_TAG", "controlplane"), ("CONTROL_PLANE_PORT", "https"),
                            ("CONTROL_PLANE_DENIED_PORTS", "22,50000"), ("PEER_DENIED_PORTS", "")):
            with self.subTest(name=name):
                result = self.check([self.control_plane()], **{name: value})
                self.assertNotEqual(result.returncode, 0)


# Answers whoami with $USERNAME (empty: unauthorized) and can-i with $CAN_I on
# stdout and $CAN_I_STDERR on stderr; records every call.
KUBECTL = """printf '%s\\n' "$*" >> "$CALLS"
case "$1 $2" in
'auth whoami')
  [ -n "$USERNAME" ] || { echo 'error: Unauthorized' >&2; exit 1; }
  printf '{"status":{"userInfo":{"username":"%s"}}}' "$USERNAME" ;;
'auth can-i')
  if [ "$3" = --list ]; then echo 'Resources  Verbs'; exit 0; fi
  [ -z "$CAN_I_STDERR" ] || echo "$CAN_I_STDERR" >&2
  [ -z "$CAN_I" ] || echo "$CAN_I"
  [ "$CAN_I" = yes ] ;;
*) exit 1 ;;
esac"""


class IdentityTests(unittest.TestCase):
    def check(self, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            write_executable(directory / "kubectl", KUBECTL)
            summary, calls = directory / "summary", directory / "calls"
            summary.touch()
            calls.touch()
            env = os.environ | {
                "PATH": f"{directory}:{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(summary),
                "CALLS": str(calls), "EXPECTED_USERNAME": USER, "USERNAME": USER, "NAMESPACE": "app-test",
                "CAN_I": "no", "CAN_I_STDERR": "",
            } | overrides
            result = subprocess.run([str(IDENTITY)], env=env, capture_output=True, text=True)
            return result, summary.read_text(), calls.read_text().splitlines()

    def test_accepts_the_expected_user_without_cluster_access(self):
        result, summary, calls = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"- Kubernetes user: `{USER}`", summary)
        self.assertIn("auth can-i --list --namespace app-test", calls)
        self.assertIn("auth can-i list secrets --all-namespaces", calls)

    def test_rejects_another_user(self):
        result, summary, _ = self.check(USERNAME="github:repo:owner@1/other@3:environment:test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected", result.stderr)
        self.assertEqual(summary, "")

    def test_keeps_warnings_of_a_denial_in_the_log(self):
        warning = "Warning: resource 'nodes' is not namespace scoped"
        result, _, _ = self.check(CAN_I_STDERR=warning)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(warning, result.stderr)

    def test_rejects_a_rejected_token(self):
        result, summary, _ = self.check(USERNAME="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rejected the token", result.stderr)
        self.assertEqual(summary, "")

    def test_rejects_cluster_wide_access_and_api_errors(self):
        # An API error is not a denial, whether reported on stdout or stderr.
        for answer, stderr in (("yes", ""), ("error: forbidden", ""), ("not a permission answer", ""),
                               ("", "error: connection refused")):
            with self.subTest(answer=answer, stderr=stderr):
                result, _, _ = self.check(CAN_I=answer, CAN_I_STDERR=stderr)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Expected the identity to be denied 'list nodes'", result.stderr)

    def test_rejects_invalid_inputs_before_calling_kubectl(self):
        for overrides in ({"EXPECTED_USERNAME": ""}, {"NAMESPACE": "App"}, {"NAMESPACE": ""}):
            with self.subTest(**overrides):
                result, _, calls = self.check(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])


class ActionTests(unittest.TestCase):
    def test_runs_the_checks_in_order_after_joining(self):
        steps = yaml.safe_load((ROOT / "tailnet-kubernetes-access/action.yml").read_text())["runs"]["steps"]
        self.assertTrue(steps[0]["uses"].startswith("tailscale/github-action@"))
        self.assertEqual(
            [step["run"] for step in steps[1:]],
            [f'"$ACTION_PATH/../scripts/{name}"' for name in
             ("tailnet-boundary-check", "kubernetes-oidc", "kubernetes-identity-check")],
        )


if __name__ == "__main__":
    unittest.main()
