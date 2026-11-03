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
    # Without an explicit platform, a multi-platform store selects the host.
    if [[ "${3:-}" != --platform || "${4:-}" != "$PLATFORM" ]]; then LABEL_ARCH="$HOST_ARCH"; fi
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
                "HOST_ARCH": "amd64", "LABEL_OS": "linux", "LABEL_ARCH": "amd64",
                "LABEL_REVISION": REVISION, "LABEL_VERSION": "1.2.3",
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
            f"image inspect --platform linux/amd64 {IMAGE}@{DIGEST}",
        ])

    def test_inspects_the_requested_platform_not_the_cached_host_variant(self):
        result, output, calls = self.resolve(PLATFORM="linux/arm64", LABEL_ARCH="arm64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, f"digest={DIGEST}\n")
        self.assertEqual(calls[1:], [
            f"pull --platform linux/arm64 {IMAGE}@{DIGEST}",
            f"image inspect --platform linux/arm64 {IMAGE}@{DIGEST}",
        ])

    def test_normalizes_docker_hub_references(self):
        for image, canonical in [
            ("library/alpine", "docker.io/library/alpine"),
            ("docker.io/alpine", "docker.io/library/alpine"),
            ("docker.io/library/alpine", "docker.io/library/alpine"),
            ("index.docker.io/alpine", "docker.io/library/alpine"),
            ("index.docker.io/library/alpine", "docker.io/library/alpine"),
            ("owner/app", "docker.io/owner/app"),
            ("owner/nested/app", "docker.io/owner/nested/app"),
            ("index.docker.io/owner/app", "docker.io/owner/app"),
        ]:
            with self.subTest(image=image):
                result, output, calls = self.resolve(IMAGE=image)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, f"digest={DIGEST}\n")
                self.assertEqual(calls, [
                    f"buildx imagetools inspect {canonical}:1.2.3 --format {{{{json .Manifest}}}}",
                    f"pull --platform linux/amd64 {canonical}@{DIGEST}",
                    f"image inspect --platform linux/amd64 {canonical}@{DIGEST}",
                ])
                # Buildx v0.35.0 reports the normalized name, not the input.
                result, output, calls = self.resolve(
                    IMAGE=image, INSPECT="error", ALLOW_MISSING="true",
                    INSPECT_ERROR=f"ERROR: {canonical}:1.2.3: not found",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, "digest=\n")
                self.assertEqual(len(calls), 1)

    def test_accepts_valid_repository_names(self):
        for image in [
            "localhost/app", "localhost:5000/owner/app", "registry.example:5000/owner/app",
            "ghcr.io/owner/app.name_with__separators--and---dashes",
            "ghcr.io/" + "a" * 247,
        ]:
            with self.subTest(image=image):
                result, output, _ = self.resolve(IMAGE=image)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, f"digest={DIGEST}\n")

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
            ("leading repository separator", {"IMAGE": "ghcr.io/owner/-app"}),
            ("trailing repository separator", {"IMAGE": "ghcr.io/owner/app."}),
            ("dot repository component", {"IMAGE": "ghcr.io/owner/."}),
            ("parent repository component", {"IMAGE": "ghcr.io/owner/.."}),
            ("empty repository component", {"IMAGE": "ghcr.io/owner//app"}),
            ("trailing slash", {"IMAGE": "ghcr.io/owner/app/"}),
            ("repeated dots", {"IMAGE": "ghcr.io/owner/app..name"}),
            ("three underscores", {"IMAGE": "ghcr.io/owner/app___name"}),
            ("mixed separators", {"IMAGE": "ghcr.io/owner/app._name"}),
            ("invalid registry label", {"IMAGE": "-registry.example/app"}),
            ("empty registry label", {"IMAGE": "registry..example/app"}),
            ("invalid registry port", {"IMAGE": "registry.example:abc/app"}),
            ("long repository name", {"IMAGE": "ghcr.io/" + "a" * 248}),
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
        # The built-in driver does not pull or boot a separate BuildKit image.
        self.assertEqual(setup["with"], {
            "version": "v0.35.0", "cache-binary": False, "driver": "docker", "use": False,
        })
        self.assertEqual(action["inputs"]["allow-missing"]["default"], "false")


if __name__ == "__main__":
    unittest.main()
