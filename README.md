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
| `go-quality` | Uses `go.mod` (override `go-version-file`), sets up Go, runs pinned golangci-lint, verifies its configuration, and rejects formatter diffs without modifying files. The caller owns `.golangci.yml`, build tags, and formatter choice. |
| `automation-quality` | Installs pinned Actionlint/ShellCheck, checks workflows and tracked/untracked non-ignored shell scripts, and runs Zizmor. Optional `shellcheck-excludes` must be justified by the caller. |
| `go-security` | Sets up Go from `go.mod` (override `go-version-file`) and runs pinned govulncheck over `./...`. |
| `semgrep` | Uses pinned scanner and rules. `rules` is a newline-separated list of paths within the rule checkout; defaults to Go injection/deserialization. WBSO additionally selects JWT rules. Ignores vendor and the rule checkout. Reserves `.semgrep-rules/` in the caller workspace. |
| `commit-policy` | Checks `message`, or HEAD's commit message when omitted, using the same Commitizen version as the bump action. The caller checks PR titles on `edited` as well as normal PR events. |
| `commitizen-bump` | Requires `token` and `validated-sha`; optional `branch` defaults to `main`. Outputs `created`, `version`, `tag`, and `sha`. See release safety below. |

Example (replace `REVIEWED_COMMIT_SHA` with an actual full SHA):

```yaml
steps:
  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
    with:
      persist-credentials: false
  - uses: abczzz13/github-actions/go-quality@REVIEWED_COMMIT_SHA
```

Triggers, job dependencies, checkout revisions, services, generation checks,
artifact production, and deployment remain in each application repository.
Do not pass arbitrary shell commands into these actions or inherit every secret.

## Release safety

The bump action supports `.cz.toml`, `version_scheme = "semver"`, and
`tag_format = "$version"`. Current support is stable versions (`1.2.3`), not
prereleases. Commitizen still owns the increment rules, changelog, and configured
version files. Its version has one definition in
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
the initial tag exists, normal increment calculation applies. Existing projects
do not pass this input. The action never pushes local intermediate state if
Commitizen fails; recovery after a successful push but failed artifact publication
uses the created tag.

## Maintenance and validation

Tool versions are owned here: action YAML pins wrappers and Go tools; the
Commitizen requirements file pins both policy and release parsing; scripts pin
Actionlint, ShellCheck (with checksum), and the Semgrep image. Zizmor's engine is
pinned independently from its wrapper. Dependabot covers actions and Commitizen;
review other tool/image/rules pins manually. No central application policy file
is imposed on consumers.

CI tests release calculation, first-version bootstrap, no-op behavior, stale
source, reruns, and atomic-push races against temporary local Git remotes. It also
runs actual Go-quality consumer fixtures for goimports and gci, including a
negative, non-mutating formatting test. Composite shell snippets are explicitly
ShellChecked in tests because Actionlint only parses workflows.

Local validation requires Python 3.13+, Commitizen from the requirements file,
PyYAML 6.0.3, Actionlint, and ShellCheck:

```nu
python -m unittest discover -s tests -v
./scripts/lint-automation
git diff --check
```

Review action input/output changes as public API changes. Test consumers before
updating their pins. Repository settings should allow squash merges only, use
read-only workflow tokens, and disallow Actions approval of PRs. Enable required
checks/reviews where the repository's GitHub plan supports them.
