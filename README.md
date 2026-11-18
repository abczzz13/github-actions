# Shared GitHub Actions

Small, versioned CI building blocks for `abczzz13/wbso-backend`,
`tdejongio/forklog-backend`, `tdejongio/forklog-frontend` and the Terraform
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
| `resolve-image` | Resolves `image:tag` to an immutable digest, pulls that digest for `platform` (default `linux/amd64`), and checks the image's OS, architecture and OCI `revision` and `version` labels. The pull is intentional: it proves that every layer of the digest can be downloaded. Outputs `digest`. With `allow-missing: 'true'`, an absent tag outputs an empty digest; registry errors always fail. The caller logs in to the registry first. Requires Docker CLI and Engine 28.1+ (API 1.49+). Installs the pinned buildx without selecting a builder or starting a separate BuildKit container. See the image resolution contract. |
| `kubernetes-oidc` | Writes a kubeconfig for `server` and `certificate-authority-data` whose exec credential requests a fresh GitHub Actions ID token for `audience` when the previous one is about to expire. Optional `namespace` sets the context namespace. Sets `KUBECONFIG` for later steps and outputs `kubeconfig`. Requires `id-token: write`, curl, jq and Python 3 with SSL support. See the Kubernetes OIDC contract. |
| `resolve-release` | Resolves an unprefixed semantic release `tag` (such as `1.2.3`) to its commit and requires that commit on the history of `reviewed-sha` (default: the workflow commit). Outputs `sha` and `version`. The caller checks out with `fetch-depth: 0`. Branches with a release-like name never stand in for the tag. |
| `tailnet-kubernetes-access` | Joins a tailnet with Tailscale workload identity, checks the network boundary, writes a `kubernetes-oidc` kubeconfig and checks the Kubernetes user. Outputs `kubeconfig` and sets `KUBECONFIG`. Requires `id-token: write`. See the tailnet Kubernetes access contract. |
| `setup-helm` | Installs the shared Helm version and outputs it as `version`. Consumers that pin this repository at one SHA therefore use one Helm; compare a Nix or local Helm with `version` in CI. |
| `helm-dry-run` | Checks a Helm release against the live cluster without installing it: `helm upgrade --install --take-ownership --dry-run=server`, then `kubectl diff --server-side --force-conflicts --field-manager=helm` of that exact server-rendered manifest without hooks. Takes `release`, `chart`, `namespace`, newline-separated `values` files and `set-string` pairs (no commas). The deprecated `kube-version-file` input is ignored; rendering uses the live cluster version. Outputs `changed` and writes the differences to the job summary. Requires Helm 4 (run `setup-helm` first), kubectl, jq and a kubeconfig. |
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

### Image resolution contract

`resolve-image` requires Docker CLI and Engine 28.1+ (API 1.49+) for
platform-specific image inspection, plus Bash and jq. It pulls and inspects the
same digest with an explicit `platform`, including when other architectures are
already cached. Image contents are never executed, so cross-platform resolution
does not require emulation.

`image` is a lowercase repository without a tag or digest and must include `/`.
Registry names use hostname labels with an optional numeric port. Repository
components start and end with letters or digits; separators are a single dot,
one or two underscores, or one or more dashes. The normalized repository name
must not exceed 255 characters.

Docker Hub references are normalized before any registry call: `library/alpine`
becomes `docker.io/library/alpine`, `index.docker.io` becomes `docker.io`, and
`docker.io/alpine` becomes `docker.io/library/alpine`. Other registries, including
`localhost` with an optional port, are unchanged. Missing-tag detection matches
Buildx's exact diagnostic for this normalized reference; other errors fail even
with `allow-missing: 'true'`.

### Kubernetes OIDC contract

`kubernetes-oidc` only authenticates. The API server must trust
`https://token.actions.githubusercontent.com` for `audience` through its JWT
authenticator, and RBAC decides what the resulting identity may do. The
username comes from the cluster's claim mapping, not from this action.

- The kubeconfig holds no token. Its exec credential plugin, shipped with the
  action, requests a new ID token on each call and sets `expirationTimestamp`
  30 seconds before the token's `exp`. A long `helm --wait` therefore keeps
  working after the first token expires. Each `kubectl` or `helm` process
  requests its own token, so a job that loops many short calls sends as many
  token requests.
- The plugin retries a failed token request three times. It passes the job's
  ID token request token to curl on stdin, never on its command line, because
  that token can mint ID tokens for any audience.
- The action requests one token up front, so a missing `id-token: write`
  permission or an unreachable token service fails this step. It logs the
  token's `sub`, `job_workflow_ref` and lifetime, never the token.
- The kubeconfig is written with mode `0600` under `RUNNER_TEMP`. It replaces
  `KUBECONFIG` for later steps of the job. The cluster, user and context names
  are always `kubernetes` and `github-actions`, so do not merge two of these
  kubeconfigs in one `KUBECONFIG` list: their entries clash and the first one
  wins. Run the action again to switch clusters.
- Inputs are checked before a token is requested: `server` must be an `https://`
  URL with a host and a valid optional port, without credentials, query or
  fragment, and a bracketed host must be an IPv6 address. `kubectl` reports
  other URL errors on its first call. Python 3's SSL library parses the
  base64-encoded PEM certificates in `certificate-authority-data`.
  `audience` must be at most 255 of `A-Za-z0-9._:/-`, and `namespace` empty or a
  valid namespace name.
- `job_workflow_ref` names the workflow file that runs the job. Inside a
  reusable workflow that is the reusable workflow, not its caller, so write the
  cluster's claim rules to match. Composite actions, including this one and
  `tailnet-kubernetes-access`, do not change it. Network access to the API server, for
  example through a VPN, is the caller's job and comes in an earlier step.

```yaml
deploy:
  runs-on: ubuntu-24.04
  timeout-minutes: 20
  environment: test
  permissions:
    contents: read
    id-token: write
  steps:
    - uses: abczzz13/github-actions/kubernetes-oidc@REVIEWED_COMMIT_SHA # REVIEWED_VERSION
      with:
        server: ${{ vars.KUBERNETES_SERVER }}
        certificate-authority-data: ${{ vars.KUBERNETES_CA_DATA }}
        audience: infra-k8s-apps
        namespace: app-test
    - run: helm upgrade --install app ./deploy/chart --rollback-on-failure --wait --timeout 15m
```

### Tailnet Kubernetes access contract

`tailnet-kubernetes-access` is for a cluster whose API server is reachable
only over a tailnet. It runs these steps, and any failure stops the job before
a later step:

1. Joins the tailnet with `tailscale/github-action` using the trust credential
   `tailscale-oauth-client-id`, the ID token `tailscale-audience` and
   `tailscale-tags`. The caller grants `id-token: write`.
2. Checks the network boundary. At least one peer must carry exactly the tag
   `control-plane-tag`. Every control plane must accept TCP
   `control-plane-port` on at least one address (three attempts per address) and
   refuse `control-plane-denied-ports` on every IPv4 and IPv6 address.
   Every other visible peer must refuse `peer-denied-ports` on every address.
   Ports must be between 1 and 65535. Probes run in parallel (up to 32 at
   once), because a denied port is usually dropped and waits for the 5-second
   timeout; results are reported in peer order. Tailscale also lists
   peers that may only connect to the runner, such as operators' devices, so a
   visible peer is probed rather than assumed reachable. Peers without a
   Tailscale IP are logged and skipped.
3. Writes a kubeconfig exactly as `kubernetes-oidc` does, with `server`,
   `certificate-authority-data`, `audience` and `namespace`.
4. Requires `kubectl auth whoami` to return exactly `expected-username` and the
   identity to be denied `list nodes`, `create namespaces` and
   `list secrets --all-namespaces`. A `can-i` answer other than `no`, including
   an API error, fails. The job summary lists the user and its permissions in
   `namespace`.

The defaults deny SSH, HTTPS, the Kubernetes API, 8443 and Talos 50000 on
other peers, and Talos and SSH on the control planes. Run it as a direct step
of the job whose identity the cluster trusts; it is a composite action, so
`job_workflow_ref` stays that of the calling workflow.

```yaml
- uses: abczzz13/github-actions/tailnet-kubernetes-access@REVIEWED_COMMIT_SHA # REVIEWED_VERSION
  with:
    tailscale-oauth-client-id: ${{ vars.TS_OIDC_CLIENT_ID }}
    tailscale-audience: ${{ vars.TS_OIDC_AUDIENCE }}
    tailscale-tags: tag:app-deployer
    control-plane-tag: tag:k8s-controlplane
    server: ${{ vars.KUBERNETES_SERVER }}
    certificate-authority-data: ${{ vars.KUBERNETES_CA_DATA }}
    audience: k8s-apps
    namespace: app-test
    expected-username: github:repo:owner@1/app@2:environment:test
- uses: abczzz13/github-actions/setup-helm@REVIEWED_COMMIT_SHA # REVIEWED_VERSION
- uses: abczzz13/github-actions/helm-dry-run@REVIEWED_COMMIT_SHA # REVIEWED_VERSION
  with:
    release: app
    chart: release/deploy/chart
    namespace: app-test
    values: release/deploy/chart/values-test.yaml
    set-string: |
      image.digest=${{ needs.source.outputs.digest }}
      image.version=${{ needs.source.outputs.version }}
```

`helm-dry-run` never installs. Its server dry run does not run hooks, and the
diff leaves hooks out, so a hook's prerequisites need their own check. The diff
uses the release manifest from that dry run, preserving upgrade state, cluster
capabilities and `lookup` results. There is no second, offline render. The release
JSON and manifest, including Secrets, stay in private temporary files that are
removed on exit. `kubectl diff` hides Secret values in the log and job summary.

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
| `actions/*` (including `actions/cache`), `golangci-lint-action`, `zizmor-action`, `setup-buildx-action`, `setup-terraform`, `setup-tflint` | `*/action.yml`, `.github/workflows/ci.yml` | Dependabot (github-actions) |
| Commitizen and its transitive dependencies | `scripts/commitizen-requirements.in` → `.txt` | Dependabot (pip, pip-compile format) |
| Test dependencies | `tests/requirements.in` → `.txt` | Dependabot (pip, pip-compile format) |
| golangci-lint engine | `go-quality/action.yml` (`version`) | manual; [releases](https://github.com/golangci/golangci-lint/releases) |
| govulncheck | `go-security/action.yml` (`@v…`) | manual; [releases](https://github.com/golang/vuln/tags) |
| Actionlint tarball and checksum | `scripts/automation-quality` | manual; [releases](https://github.com/rhysd/actionlint/releases) (`*_checksums.txt`) |
| ShellCheck tarball and checksum | `scripts/automation-quality` | manual; [releases](https://github.com/koalaman/shellcheck/releases) |
| Zizmor engine | `automation-quality/action.yml` (`version`) | manual; [releases](https://github.com/zizmorcore/zizmor/releases) |
| Buildx engine | `resolve-image/action.yml` (`version`) | manual; [releases](https://github.com/docker/buildx/releases); re-verify the exact missing-tag diagnostic when updating |
| Semgrep image tag and digest | `scripts/semgrep` | manual; [Docker Hub](https://hub.docker.com/r/semgrep/semgrep/tags) |
| Helm version | `setup-helm/action.yml` (`version` and output) | manual; [releases](https://github.com/helm/helm/releases) |
| Tailscale client version | `tailnet-kubernetes-access/action.yml` (`tailscale-version`) | manual; [releases](https://github.com/tailscale/tailscale/releases) |
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
image resolution, Docker Hub normalization, exact missing-tag diagnostics,
platform selection with a cached host variant, label checks and rejected inputs
against a fake `docker` in `tests/test_resolve_image.py`;
kubeconfig contents and file mode, the up-front token request, exec credentials
and their early expiry, base64url claims, audience encoding, a retried token
request, the request token kept off curl's command line, token-service and
token errors, parsed URLs and PEM bundles, and rejected inputs against a local
fake token service, plus real `kubectl` sending the plugin's token to a local
HTTPS server and renewing credentials in one proxy process after early expiry
(skipped without `kubectl` or `openssl`) in `tests/test_kubernetes_oidc.py`;
release resolution against real Git tags in `tests/test_resolve_release.py`;
network boundary checks against IPv4 and IPv6 loopback sockets, unnamed peers,
invalid port ranges and parallel probing in `tests/test_tailnet_kubernetes_access.py`;
exact server-manifest reuse, private temporary files and cleanup on failure
against fake tools in `tests/test_helm_dry_run.py`, plus real Helm 4 install and
upgrade dry runs against a local API fixture in `tests/test_helm_integration.py`
(skipped without Helm 4; schema, admission and real-cluster access are not tested);
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
state, downloads the artifact and verifies its SHA-256 digest and exact bytes;
an ordinary failure must produce no artifact. It looks up the action's output
name rather than predicting it. `tests/verify_terraform_recovery_artifact.py`
polls the REST API for expected artifact availability with bounded backoff
(up to 31 seconds of waits per lookup or download phase), because discovery can
lag a successful upload. It retries
transient service and network errors, but never authentication errors, digest
mismatches or incorrect contents. API errors never count as proof of absence.
Only the recovery fixture job has `actions: read` for these API calls. Local
tests cover delayed discovery, exhausted retries, API errors, checksum and
content failures, and keeping the token off blob-storage redirects.

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
Terraform CLI as well to include the backend recovery integration test, and
Helm 4 to include the local API rendering integration test:

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
