# Shared GitHub Actions

Small, versioned CI building blocks for `abczzz13/wbso-backend`,
`abczzz13/forklog-backend`, `tdejongio/forklog-frontend` and the Terraform
repository `tdejongio/forklog-infra`.
Consumers pin a full commit SHA, not `main` or a
moving version tag, with the version as a trailing comment. Dependabot
discovers newer versions through this repository's semantic tags and proposes
updated SHAs independently.

## Actions

All actions run at the consuming repository's root after its checkout with
`persist-credentials: false`. Scripts ship with the action; they never depend on
consumer-owned copies. Linux runners are supported; automation-quality currently
requires AMD64, and semgrep and resolve-image require Docker. Run jobs with
explicit timeouts and read-only `contents` permissions unless a job publishes.

| Path | Contract |
| --- | --- |
| `go-quality` | Uses `go.mod` (override `go-version-file`), sets up Go and pinned golangci-lint, then verifies the lint configuration, lints, and rejects formatter diffs without modifying files. The caller owns `.golangci.yml`, build tags, and formatter choice. |
| `automation-quality` | Installs checksum-verified Actionlint and ShellCheck, checks workflows and tracked/untracked non-ignored shell scripts (sh, bash, dash; `vendor/` and `.semgrep-rules/` skipped), and runs Zizmor. Both tools stay on `PATH` for later steps of the same job. Optional `shellcheck-excludes` must be justified by the caller. No Go toolchain is installed or changed. |
| `go-security` | Sets up Go from `go.mod` (override `go-version-file`) and runs pinned govulncheck over `./...`. |
| `semgrep` | Uses pinned scanner and rules. `rules` is a newline-separated list of relative paths within the rule checkout; defaults to Go injection/deserialization. Ignores vendor and the rule checkout. Reserves `.semgrep-rules/` in the caller workspace; add it to the caller's `.gitignore`. |
| `commit-policy` | Checks `message`, or HEAD's commit message when omitted, using the same Commitizen version as the bump action. The caller checks PR titles on `edited` as well as normal PR events. |
| `commitizen-bump` | Requires `token` and `validated-sha`; optional `branch` defaults to `main`. Outputs `created`, `version`, `tag`, and `sha`. See release safety below. |
| `resolve-image` | Resolves `image:tag` to an immutable digest, pulls that digest for `platform` (default `linux/amd64`), and checks the image's OS, architecture and OCI `revision` and `version` labels. Outputs `digest`. With `allow-missing: 'true'`, an absent tag outputs an empty digest; registry errors always fail. The caller logs in to the registry first. Installs the pinned buildx without selecting it as the builder. |
| `terraform-quality` | Checks formatting repository-wide, then runs backend-free `init`, `validate` and TFLint for each of `roots` (default `environments/*`). Optional `run-tests: 'true'` runs offline tests. The caller owns `.tflint.hcl`. See the Terraform contract. |
| `terraform-plan` | Initializes the backend of `root` and plans it. Outputs `changes`. |
| `terraform-apply` | Plans `root` and applies exactly that plan. Requires `state-recovery`. Outputs `changes`, `errored-state-path` and `errored-state-artifact`. |

Example (replace `REVIEWED_COMMIT_SHA` with an actual full SHA):

```yaml
steps:
  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
    with:
      persist-credentials: false
  - uses: abczzz13/github-actions/go-quality@REVIEWED_COMMIT_SHA # REVIEWED_VERSION
  - uses: abczzz13/github-actions/semgrep@REVIEWED_COMMIT_SHA # REVIEWED_VERSION
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

### Terraform contract

**Terraform CLI.** Each Terraform action installs the pinned Terraform CLI without
the output wrapper, unless an unwrapped `terraform` of the required version is
already on `PATH`. So a job that runs several of these actions downloads Terraform
once. Set `terraform-version` to an exact version to use another release. To bring
your own CLI, install that version first (for example from a cache or a custom
image) and pass the same `terraform-version`; the action reuses it.

**Caller responsibilities.** The caller owns everything that grants access:
triggers, the GitHub environment, provider secrets, `id-token: write`, and cloud
authentication. Authenticate, or join a VPN, in earlier steps of the same job.

- The backend must be fully configured in the root; there is no
  `-backend-config` input. Production roots need a durable remote backend with
  locking. A root without a backend uses local state, which the runner loses
  even after a successful apply. Use local state only for disposable fixtures.
- Each root that uses external providers must commit a reviewed
  `.terraform.lock.hcl`. Every action initializes with `-lockfile=readonly`, so
  CI never changes provider selections. Generate the lock file locally with
  `terraform init -backend=false`.
- Variables come from Terraform's automatic loading only: `terraform.tfvars`,
  `*.auto.tfvars` (and their `.json` forms) in the root, or `TF_VAR_<name>`
  environment variables on the step. There is no `-var-file` input. Keep secret
  values out of committed variable files.
- Plan and apply output goes to the job log, including values not marked
  sensitive. If such values must not appear in logs, use another implementation.

**Roots.** Paths are relative to the repository and use letters, digits, `_`,
`.`, `/` and `-`. In `roots`, `*` matches within one path component; `**` is not
supported. Each pattern must match at least one directory; files are skipped.
Roots are resolved before Terraform runs: symlinks inside the checkout are
followed, and paths that resolve outside it are rejected. `terraform-quality`
checks patterns in input order, matches in lexical order, and each resolved
directory once.

**Quality.** Formatting runs `terraform fmt -check -recursive -diff` from the
repository root, so unformatted files outside `roots` also fail. That command skips
hidden directories, so the hidden directory holding a selected root (such as
`.infra`) is checked as well. Providers are downloaded once per run into a
temporary plugin cache, unless the caller sets `TF_PLUGIN_CACHE_DIR`.
`terraform-quality` does not authenticate and skips the backend, but it is not a
sandbox: `terraform test` can still create real infrastructure. Set
`run-tests: 'true'` only when every test uses mocked providers or is otherwise
offline. Do not give quality jobs provider credentials.

**Apply.** An apply job must:

1. Run only from the default branch at an approved revision. The action makes a
   **fresh** plan and applies it without a pause, so drift can change what it does.
   If approval must bind to the exact reviewed plan, use a workflow that stores
   and applies that plan file.
2. Share one concurrency group with the plans of the same root, with
   `cancel-in-progress: false`. Never cancel a running apply.
3. Keep cloud credentials valid longer than the job timeout, so the final state
   write succeeds.
4. Grant `contents: read` and, for OIDC, `id-token: write`.

**State recovery.** When Terraform cannot write state to the backend, it saves
`errored.tfstate` in the root. This file can contain passwords and private keys.
The action refuses to start while that file exists, and always outputs its
location as `errored-state-path`. `state-recovery` has no default:

- `github-artifact` uploads the file **without client-side encryption** as an
  artifact kept for 3 days. Anyone who can download the repository's artifacts can
  read it, which can be more people than can read remote state. The artifact name,
  output as `errored-state-artifact`, is unique per invocation; the output does not
  mean that an artifact exists.
- `none` uploads nothing. Preserve `errored-state-path` in a later step, for
  example by encrypting it to your own storage with `if: failure() || cancelled()`,
  or accept that it is lost with the runner.

Upload is best effort after failure or cancellation, and impossible after runner
loss. After a failed apply, inspect provider accounts and the emergency state
before retrying. Restore state only after review, then remove the file and the
artifact.

```yaml
jobs:
  plan:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    environment: test
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: aws-actions/configure-aws-credentials@REVIEWED_COMMIT_SHA # REVIEWED_VERSION
        with:
          role-to-assume: arn:aws:iam::123456789012:role/example
          aws-region: eu-west-1
      - uses: abczzz13/github-actions/terraform-plan@REVIEWED_COMMIT_SHA # REVIEWED_VERSION
        with:
          root: environments/test
```

In the caller's authorized apply job, after authentication:

```yaml
- uses: abczzz13/github-actions/terraform-apply@REVIEWED_COMMIT_SHA # REVIEWED_VERSION
  with:
    root: environments/test
    # Accept repository-artifact access to potentially secret emergency state.
    state-recovery: github-artifact
```

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
5. Resolve the exact tag SHA after the bump, then build/verify the project's
   artifacts from that SHA before publishing. A bump changes the source SHA.
   Either validate the tag SHA again or prove that each check being skipped
   remains equivalent to its result at `validated-sha`:
   - The tag commit's only parent is `validated-sha`.
   - Every changed path is explicitly allowed and is read by neither the
     skipped checks nor the artifact build (typically `.cz.toml` and the
     changelog). Workflow `paths-ignore` filters are not evidence of this:
     tooling may still read those files.
   - Skipped checks do not depend on changed Git metadata, including commit
     IDs/timestamps, tags, `git describe`, or VCS-stamped version data.

   Fail closed: if any condition is uncertain, validate the tag again. A
   version file the build reads (Commitizen `version_files`) rules out the
   proof. Manual recovery has no `validated-sha`, so it always validates the tag.
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

## Releasing this repository

This repository releases itself with its own `commitizen-bump` action: the
`release` job in `.github/workflows/ci.yml` runs after the CI jobs on every
`main` push, following the rules above. Without a breaking marker, `fix:`,
`refactor:`, and `perf:` commits produce a patch tag; `feat:` produces a minor
tag. Other types (such as `docs:`, `test:`, and `chore:`) are a no-op. A `!`
marker (for example, `feat!:` or `refactor!:`) or a `BREAKING CHANGE:` footer
produces a major tag, including while the version is `0.x`.

This repository deliberately requires a major release for **any** change to an
action's inputs, outputs, or documented contract, including backward-compatible
additions. This is stricter than SemVer; mark those commits accordingly. CI
checks PR titles with `commit-policy`, including when titles are edited. Use the
validated title as the squash commit title; reviewers must still verify that
the selected increment matches the change.

The tag points at the bump commit, which changes only `.cz.toml` and
`CHANGELOG.md` on top of the CI-validated commit. Tests enforce this with the
repository's actual release configuration. Consumer actions do not read this
action repository's release metadata; `commitizen-bump` reads the caller's
`.cz.toml`. The action implementation is therefore unchanged by the bump.
Consumers pin the SHA of a tag, never `main`.

## Maintenance and validation

### Go-quality caching internals

Caching is a best-effort optimization; its layout and keys are implementation
details, not part of the action's public contract. Every run attempts to restore
available caches. Module downloads are cached separately by the configured Go
version file and workspace `go.sum` files, avoiding a full module-cache upload
on each source commit. Build and lint analysis caches use per-commit keys.

Restores prefer an exact key, then the newest entry matching each fallback
prefix in order; this is not necessarily the newest cache overall. Only
successful non-PR default-branch quality checks save missing entries, and
exact hits are not uploaded again. Both `pull_request` and
`pull_request_target` runs restore caches but do not save them, even though
`pull_request_target` uses the base branch ref. Callers remain responsible for
checking out trusted source in other default-branch workflows. Build and
analysis caches are content-addressed, so older entries remain safe to reuse.

Before updating consumer pins, validate caching in real GitHub Actions runs:

- Exercise a cold cache, an exact-hit rerun, and a later commit using fallback
  caches. Confirm PR runs restore without saving.
- Record module and build/analysis archive sizes and total restore/lint/save
  time, comparing against the consumer's current pin.
- Check repository cache usage and eviction pressure from per-commit entries.
  Do not roll out without confirming an end-to-end benefit.

Local tests check configuration and guards, not GitHub's cache service or
performance.

### Tool pins

Tool versions are owned here. Dependabot covers the pins in the first group; the
second group is reviewed manually, so check each entry when bumping anything nearby.

| Pin | Location | Updated by |
| --- | --- | --- |
| `actions/*` (including `actions/cache`), `golangci-lint-action`, `zizmor-action`, `setup-terraform`, `setup-tflint` | `*/action.yml`, `.github/workflows/ci.yml` | Dependabot (github-actions) |
| Commitizen and its transitive dependencies | `scripts/commitizen-requirements.in` → `.txt` | Dependabot (pip, pip-compile format) |
| Test dependencies | `tests/requirements.in` → `.txt` | Dependabot (pip, pip-compile format) |
| golangci-lint engine | `go-quality/action.yml` (`version`) | manual; [releases](https://github.com/golangci/golangci-lint/releases) |
| govulncheck | `go-security/action.yml` (`@v…`) | manual; [releases](https://github.com/golang/vuln/tags) |
| Actionlint tarball and checksum | `scripts/automation-quality` | manual; [releases](https://github.com/rhysd/actionlint/releases) (`*_checksums.txt`) |
| ShellCheck tarball and checksum | `scripts/automation-quality` | manual; [releases](https://github.com/koalaman/shellcheck/releases) |
| Zizmor engine | `automation-quality/action.yml` (`version`) | manual; [releases](https://github.com/zizmorcore/zizmor/releases) |
| Semgrep image tag and digest | `scripts/semgrep` | manual; [Docker Hub](https://hub.docker.com/r/semgrep/semgrep/tags) |
| Terraform default version | `scripts/terraform-version` (`default`) | manual; [releases](https://github.com/hashicorp/terraform/releases) |
| TFLint version and checksums | `terraform-quality/action.yml` (`tflint_version`, `checksums` of the Linux AMD64 and ARM64 zips) | manual; [releases](https://github.com/terraform-linters/tflint/releases) (`checksums.txt`) |
| semgrep-rules revision | `semgrep/action.yml` (`ref`) | manual; [repository](https://github.com/semgrep/semgrep-rules) |

No central application policy file is imposed on consumers.

To change a Python dependency, edit the `.in` file and regenerate the lock from
its directory with `pip-compile --allow-unsafe --generate-hashes --no-annotate
--strip-extras --output-file <name>.txt <name>.in` (from `pip-tools`). Every
installation uses `--require-hashes`, so an unlocked or tampered package fails.

CI tests release calculation, first-version bootstrap (including the configured
changelog file), no-op behavior, stale source, reruns, and atomic-push races
against temporary local Git remotes; Go-quality cache keys, save guards, and
path discovery (including environment overrides); Semgrep rule-path validation and
automation-lint file selection against fake `docker`/`shellcheck` executables;
Terraform root selection and deduplication, symlink containment, hidden-root
formatting, the plugin cache, test opt-in, CLI version reuse, recovery policies,
unique recovery names, plan exit codes, saved-plan apply and temporary-plan
cleanup against fake `terraform`/`tflint` executables in `tests/test_terraform.py`;
actual Go-quality consumer fixtures for goimports and gci; and an actual Terraform
consumer fixture that is checked, planned, applied with local state and planned
again, without accounts. Both kinds of consumer fixture include a negative,
non-mutating formatting test; the Terraform one also covers a hidden root.

A separate hosted recovery fixture uses a fake Terraform executable, never a real
backend or provider. The action reuses it through `terraform-version`, which also
covers the bring-your-own CLI path. It fails apply in a hidden root with synthetic emergency
state, downloads the artifact and verifies its bytes; an ordinary failure must
produce no artifact. It downloads by the action's output rather than predicting
the generated name.

`tests/test_terraform_integration.py` also runs real Terraform against a localhost
HTTP backend that rejects state writes. It verifies that the actual failed state
write produces complete emergency state in a hidden root and removes the saved
plan, without external providers or accounts. The Terraform consumer job runs
this test with the pinned CLI that `terraform-quality` installed. Local discovery skips it when Terraform is absent.

Release waits for both Terraform jobs. Local tests cannot verify GitHub's artifact
service or cancellation behavior. Cancellation/runner-loss recovery is not
claimed as tested.

Composite shell snippets are explicitly ShellChecked in tests, using the pinned
ShellCheck that automation-quality leaves on `PATH`, because Actionlint only
parses workflows.

Local validation requires Python 3.13+, Actionlint, and ShellCheck. Install a
Terraform CLI as well to include the backend recovery integration test:

```nu
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r scripts/commitizen-requirements.txt -r tests/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
./scripts/lint-automation
git diff --check
```

Review action input/output changes as public API changes. Test consumers before
updating their pins. Repository settings should allow squash merges only, use
PR titles as the default squash commit titles, read-only workflow tokens, and
disallow Actions approval of PRs. Enable required checks/reviews where the
repository's GitHub plan supports them.
