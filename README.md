# Shared GitHub Actions

Small, versioned CI building blocks for `abczzz13/wbso-backend` and
`abczzz13/forklog-backend`. Consumers pin a full commit SHA, not `main` or a
moving version tag. Dependabot proposes adoption of updates independently.

## Actions

All actions run at the consuming repository's root after its checkout with
`persist-credentials: false`. Scripts ship with the action; they never depend on
consumer-owned copies. Linux runners are supported; automation-quality currently
requires AMD64, and semgrep requires Docker. Run jobs with explicit timeouts and
read-only `contents` permissions unless a job publishes.

| Path | Contract |
| --- | --- |
| `go-quality` | Uses `go.mod` (override `go-version-file`), sets up Go and pinned golangci-lint, then verifies the lint configuration, lints, and rejects formatter diffs without modifying files. The caller owns `.golangci.yml`, build tags, and formatter choice. |
| `automation-quality` | Installs checksum-verified Actionlint and ShellCheck, checks workflows and tracked/untracked non-ignored shell scripts (sh, bash, dash; `vendor/` and `.semgrep-rules/` skipped), and runs Zizmor. Both tools stay on `PATH` for later steps of the same job. Optional `shellcheck-excludes` must be justified by the caller. No Go toolchain is installed or changed. |
| `go-security` | Sets up Go from `go.mod` (override `go-version-file`) and runs pinned govulncheck over `./...`. |
| `semgrep` | Uses pinned scanner and rules. `rules` is a newline-separated list of relative paths within the rule checkout; defaults to Go injection/deserialization. Ignores vendor and the rule checkout. Reserves `.semgrep-rules/` in the caller workspace; add it to the caller's `.gitignore`. |
| `commit-policy` | Checks `message`, or HEAD's commit message when omitted, using the same Commitizen version as the bump action. The caller checks PR titles on `edited` as well as normal PR events. |
| `commitizen-bump` | Requires `token` and `validated-sha`; optional `branch` defaults to `main`. Outputs `created`, `version`, `tag`, and `sha`. See release safety below. |

Example (replace `REVIEWED_COMMIT_SHA` with an actual full SHA):

```yaml
steps:
  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
    with:
      persist-credentials: false
  - uses: abczzz13/github-actions/go-quality@REVIEWED_COMMIT_SHA
  - uses: abczzz13/github-actions/semgrep@REVIEWED_COMMIT_SHA
    with:
      # Extend the defaults, for example with JWT rules for a service that issues tokens.
      rules: |
        go/lang/security/deserialization
        go/lang/security/injection
        go/jwt-go
```

Triggers, job dependencies, checkout revisions, services, generation checks,
artifact production, and deployment remain in each application repository.
Do not pass arbitrary shell commands into these actions or inherit every secret.

## Release safety

The bump action supports `.cz.toml`, `version_scheme = "semver"`, and
`tag_format = "$version"`. Current support is stable versions (`1.2.3`), not
prereleases. Commitizen still owns the increment rules, changelog (written to the
configured `changelog_file`), and configured version files. Its version has one
definition in `scripts/commitizen-requirements.in`, locked with hashes in
`scripts/commitizen-requirements.txt`, used by checking and bumping alike.

The caller **must**:

1. Gate release creation on successful same-repository main-push CI. For
   `workflow_run`, check event, conclusion, branch, and repository identity.
   Never invoke the write-capable action on a PR or arbitrary workflow artifact.
2. Serialize all version creation and recovery for the repository with
   `cancel-in-progress: false`.
3. Check out the validated SHA with full history and no persisted credentials.
4. Grant only the bump job `contents: write`, using the caller's `GITHUB_TOKEN`.
5. Resolve and validate the exact tag SHA after the bump, then build/verify the
   project's artifacts before publishing. A bump changes the source SHA.
6. Keep publication in the same workflow: pushes by `GITHUB_TOKEN` do not trigger
   another tag-push workflow. A manual recovery selects an existing tag and must
   validate it again, not invoke another bump or overwrite published artifacts.

The action fetches the current release branch and tags. If main advanced, it
returns a successful no-op. Otherwise it creates the bump and pushes branch and
tag atomically, without force; a concurrent push rejects both. Credentials are
transport-only environment configuration, never persisted in Git or passed to
Commitizen. No eligible commits also returns `created=false` with empty outputs.

For a new project, configure `.cz.toml` with the desired initial version and pass
that same value in `initial-version`. Bootstrap is accepted only when no stable
semantic tags exist and the configured version matches. It releases exactly
that version, regardless of historical feature/breaking-change commits. Once
the initial tag exists, normal increment calculation applies and
`initial-version` is ignored rather than rejected, so the input can be removed
at leisure; existing projects do not pass it. The action never pushes local
intermediate state if Commitizen fails; recovery after a successful push but
failed artifact publication uses the created tag.

## Maintenance and validation

Tool versions are owned here. Dependabot covers the pins in the first group; the
second group is reviewed manually, so check each entry when bumping anything nearby.

| Pin | Location | Updated by |
| --- | --- | --- |
| `actions/*`, `golangci-lint-action`, `zizmor-action` | `*/action.yml`, `.github/workflows/ci.yml` | Dependabot (github-actions) |
| Commitizen and its transitive dependencies | `scripts/commitizen-requirements.in` → `.txt` | Dependabot (pip, pip-compile format) |
| Test dependencies | `tests/requirements.in` → `.txt` | Dependabot (pip, pip-compile format) |
| golangci-lint engine | `go-quality/action.yml` (`version`) | manual; [releases](https://github.com/golangci/golangci-lint/releases) |
| govulncheck | `go-security/action.yml` (`@v…`) | manual; [releases](https://github.com/golang/vuln/tags) |
| Actionlint tarball and checksum | `scripts/automation-quality` | manual; [releases](https://github.com/rhysd/actionlint/releases) (`*_checksums.txt`) |
| ShellCheck tarball and checksum | `scripts/automation-quality` | manual; [releases](https://github.com/koalaman/shellcheck/releases) |
| Zizmor engine | `automation-quality/action.yml` (`version`) | manual; [releases](https://github.com/zizmorcore/zizmor/releases) |
| Semgrep image tag and digest | `scripts/semgrep` | manual; [Docker Hub](https://hub.docker.com/r/semgrep/semgrep/tags) |
| semgrep-rules revision | `semgrep/action.yml` (`ref`) | manual; [repository](https://github.com/semgrep/semgrep-rules) |

No central application policy file is imposed on consumers.

To change a Python dependency, edit the `.in` file and regenerate the lock from
its directory with `pip-compile --allow-unsafe --generate-hashes --no-annotate
--strip-extras --output-file <name>.txt <name>.in` (from `pip-tools`). Every
installation uses `--require-hashes`, so an unlocked or tampered package fails.

CI tests release calculation, first-version bootstrap (including the configured
changelog file), no-op behavior, stale source, reruns, and atomic-push races
against temporary local Git remotes; Semgrep rule-path validation and
automation-lint file selection against fake `docker`/`shellcheck` executables;
and actual Go-quality consumer fixtures for goimports and gci, including a
negative, non-mutating formatting test. Composite shell snippets are explicitly
ShellChecked in tests, using the pinned ShellCheck that automation-quality leaves
on `PATH`, because Actionlint only parses workflows.

Local validation requires Python 3.13+, Actionlint, and ShellCheck:

```nu
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r scripts/commitizen-requirements.txt -r tests/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
./scripts/lint-automation
git diff --check
```

Review action input/output changes as public API changes. Test consumers before
updating their pins. Repository settings should allow squash merges only, use
read-only workflow tokens, and disallow Actions approval of PRs. Enable required
checks/reviews where the repository's GitHub plan supports them.
