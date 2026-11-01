import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from helpers import ROOT

IMAGE = "ghcr.io/owner/app"
REVISION = "a" * 40
DIGEST = "sha256:" + "b" * 64

# A fake docker that serves one registry tag. Every value comes from the
# environment, so each test changes only what it is about.
DOCKER = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$CALLS"
case "$1 $2" in
  'buildx imagetools')
    if [[ "$INSPECT" == missing ]]; then echo "ERROR: $4: not found" >&2; exit 1; fi
    if [[ "$INSPECT" == error ]]; then printf '%s\\n' "$INSPECT_ERROR" >&2; exit 1; fi
    printf '{"digest":"%s"}\\n' "$MANIFEST_DIGEST" ;;
  'pull --platform') [[ "$PULL" == ok ]] ;;
  'image inspect')
    printf '[{"Os":"%s","Architecture":"%s","Config":{"Labels":{"org.opencontainers.image.revision":"%s","org.opencontainers.image.version":"%s"}}}]\\n' \\
      "$LABEL_OS" "$LABEL_ARCH" "$LABEL_REVISION" "$LABEL_VERSION" ;;
  *) echo "unexpected docker $*" >&2; exit 1 ;;
esac
"""


class ResolveImageTests(unittest.TestCase):
    def resolve(self, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            docker = directory / "docker"
            docker.write_text(DOCKER)
            docker.chmod(0o700)
            output, calls = directory / "output", directory / "calls"
            output.touch()
            calls.touch()
            env = os.environ | {
                "PATH": f"{directory}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output), "CALLS": str(calls),
                "IMAGE": IMAGE, "TAG": "1.2.3", "REVISION": REVISION, "VERSION": "1.2.3",
                "PLATFORM": "linux/amd64", "ALLOW_MISSING": "false",
                "INSPECT": "ok", "INSPECT_ERROR": "", "MANIFEST_DIGEST": DIGEST, "PULL": "ok",
                "LABEL_OS": "linux", "LABEL_ARCH": "amd64", "LABEL_REVISION": REVISION, "LABEL_VERSION": "1.2.3",
            } | overrides
            result = subprocess.run([str(ROOT / "scripts/resolve-image")], env=env, capture_output=True, text=True)
            return result, output.read_text(), calls.read_text().splitlines()

    def test_outputs_the_verified_digest(self):
        result, output, calls = self.resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, f"digest={DIGEST}\n")
        # The image is pulled and inspected by digest, never by the movable tag.
        self.assertEqual(calls[1:], [
            f"pull --platform linux/amd64 {IMAGE}@{DIGEST}",
            f"image inspect {IMAGE}@{DIGEST}",
        ])

    def test_reports_an_absent_tag_only_when_allowed(self):
        result, output, calls = self.resolve(INSPECT="missing", ALLOW_MISSING="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "digest=\n")
        self.assertEqual(len(calls), 1)
        result, output, _ = self.resolve(INSPECT="missing")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(output, "")

    def test_never_treats_a_registry_error_as_absent(self):
        for error in [
            f"ERROR: {IMAGE}:1.2.3: unexpected status: 503",
            "ERROR: failed to fetch anonymous token: not found",
            f"ERROR: {IMAGE}:1.2.3-rc: not found",
            f"ERROR: {IMAGE}:1.2.3: not found: retry later",
        ]:
            with self.subTest(error):
                result, output, calls = self.resolve(INSPECT="error", INSPECT_ERROR=error, ALLOW_MISSING="true")
                self.assertEqual(result.returncode, 1)
                self.assertIn(error, result.stderr)
                self.assertEqual(output, "")
                self.assertEqual(len(calls), 1)

    def test_rejects_images_that_do_not_match(self):
        for name, overrides in [
            ("mutable digest", {"MANIFEST_DIGEST": "latest"}),
            ("short digest", {"MANIFEST_DIGEST": "sha256:" + "b" * 63}),
            ("failed pull", {"PULL": "fail"}),
            ("other OS", {"LABEL_OS": "windows"}),
            ("other architecture", {"LABEL_ARCH": "arm64"}),
            ("other source", {"LABEL_REVISION": "c" * 40}),
            ("other version", {"LABEL_VERSION": "1.2.4"}),
            ("other platform", {"PLATFORM": "linux/arm64"}),
        ]:
            with self.subTest(name):
                result, output, _ = self.resolve(**overrides)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(output, "")

    def test_rejects_inputs_before_contacting_the_registry(self):
        for name, overrides in [
            ("image with tag", {"IMAGE": f"{IMAGE}:latest"}),
            ("image with digest", {"IMAGE": f"{IMAGE}@{DIGEST}"}),
            ("uppercase image", {"IMAGE": "ghcr.io/Owner/app"}),
            ("image without repository", {"IMAGE": "ghcr.io"}),
            ("empty tag", {"TAG": ""}),
            ("tag with option", {"TAG": "-v"}),
            ("tag with path", {"TAG": "1.2.3/x"}),
            ("short revision", {"REVISION": "a" * 39}),
            ("named revision", {"REVISION": "main"}),
            ("empty version", {"VERSION": ""}),
            ("platform with variant", {"PLATFORM": "linux/arm64/v8"}),
            ("loose allow-missing", {"ALLOW_MISSING": "yes"}),
        ]:
            with self.subTest(name):
                result, output, calls = self.resolve(**overrides)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(output, "")
                self.assertEqual(calls, [], "docker must not run for rejected inputs")

    def test_action_pins_the_buildx_that_reports_missing_tags(self):
        action = yaml.safe_load((ROOT / "resolve-image/action.yml").read_text())
        setup = action["runs"]["steps"][0]
        self.assertTrue(setup["uses"].startswith("docker/setup-buildx-action@"))
        self.assertEqual(setup["with"], {"version": "v0.35.0", "cache-binary": False, "use": False})
        self.assertEqual(action["inputs"]["allow-missing"]["default"], "false")


if __name__ == "__main__":
    unittest.main()
