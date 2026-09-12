"""Shared Commitizen policy and atomic release creation (Python standard library)."""

import base64
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib


VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")


def git(*args, env=None):
    return subprocess.run(
        ["git", *args], check=True, text=True, capture_output=True, env=env
    ).stdout.strip()


def cz(*args):
    # The credential is only for Git transport, never for Commitizen or its hooks.
    env = os.environ.copy()
    env.pop("RELEASE_TOKEN", None)
    return subprocess.run([sys.executable, "-m", "commitizen", *args], env=env)


def configured_version():
    config = tomllib.loads(Path(".cz.toml").read_text())["tool"]["commitizen"]
    if config.get("tag_format") != "$version" or config.get("version_scheme") != "semver":
        raise ValueError(".cz.toml must use semver and unprefixed $version tags")
    version = config["version"]
    if not VERSION.fullmatch(version):
        raise ValueError("only stable, unprefixed semantic versions are supported")
    return version


def create_release(validated_sha, branch, initial_version="", transport_env=None):
    """Validate source, calculate a bump, then push branch and tag atomically.

    The caller must gate this on successful same-repository main CI and serialize
    releases. Git's non-force, atomic push is the final concurrent-update guard.
    transport_env is passed only to fetch/push, never persisted in Git config.
    """
    if not SHA.fullmatch(validated_sha):
        raise ValueError("validated-sha must be a full commit SHA")
    git("check-ref-format", f"refs/heads/{branch}")
    if git("rev-parse", "HEAD") != validated_sha:
        raise ValueError("checkout does not match the validated SHA")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("release checkout must be clean")

    git("fetch", "--quiet", "--tags", "origin", f"refs/heads/{branch}", env=transport_env)
    if git("rev-parse", "FETCH_HEAD") != validated_sha:
        print("Release branch advanced; a newer validated run must create the version.")
        return None

    previous = configured_version()
    tags = git("tag", "--list").splitlines()
    bootstrap = previous not in tags
    if bootstrap:
        if initial_version != previous or any(VERSION.fullmatch(tag) for tag in tags):
            raise ValueError("current version tag is missing; explicit first-version bootstrap is required")
    else:
        git("merge-base", "--is-ancestor", f"refs/tags/{previous}", "HEAD")

    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    if bootstrap:
        # Commitizen intentionally refuses a bump from 0.1.0 to 0.1.0. Generate
        # its initial changelog, then establish that explicit baseline tag.
        cz("changelog", "--unreleased-version", initial_version, "--file-name", "CHANGELOG.md").check_returncode()
        git("add", "CHANGELOG.md")
        git("commit", "--allow-empty", "-m", f"bump: initial version {initial_version}")
        git("tag", initial_version)
    else:
        result = cz("bump", "--yes", "--changelog", "--check-consistency")
        if result.returncode == 21:
            if git("status", "--porcelain") or git("rev-parse", "HEAD") != validated_sha:
                raise ValueError("Commitizen reported no release but modified the checkout")
            print("No release-worthy commits; no version was created.")
            return None
        result.check_returncode()

    version = configured_version()
    sha = git("rev-parse", "HEAD")
    if version in tags or git("rev-parse", f"refs/tags/{version}^{{commit}}") != sha:
        raise ValueError("Commitizen did not create a new tag at the release commit")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Commitizen left uncommitted release changes")
    git(
        "push", "--atomic", "origin", f"HEAD:refs/heads/{branch}", f"refs/tags/{version}",
        env=transport_env,
    )
    return {"created": "true", "version": version, "tag": version, "sha": sha}


def main():
    if sys.argv[1:] == ["check"]:
        message = os.environ.get("COMMIT_MESSAGE", "")
        if not message:
            message = git("log", "-1", "--format=%B")
        return cz("check", "--message", message).returncode
    if sys.argv[1:] != ["bump"]:
        raise ValueError("usage: commitizen_release.py check|bump")

    token = os.environ.pop("RELEASE_TOKEN")
    if not token:
        raise ValueError("release token is required")
    transport_env = os.environ.copy()
    transport_env.pop("RELEASE_TOKEN", None)
    authorization = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    transport_env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {authorization}",
    })
    release = create_release(
        os.environ["VALIDATED_SHA"], os.environ["RELEASE_BRANCH"],
        os.environ.get("INITIAL_VERSION", ""), transport_env,
    )
    outputs = release or {"created": "false", "version": "", "tag": "", "sha": ""}
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for name, value in outputs.items():
            output.write(f"{name}={value}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        if release:
            summary.write(f"Created version **{release['version']}** at `{release['sha']}`.\n")
        else:
            summary.write("No version created; see the release-state explanation in the log.\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        # Transport credentials are environment-only, never part of this command.
        print(f"Release command failed: {error.cmd}", file=sys.stderr)
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
    except (KeyError, ValueError) as error:
        print(f"Commitizen policy failed: {error}", file=sys.stderr)
        sys.exit(1)
