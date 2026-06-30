# Veracode Baseline + Mitigation Management

Adds robust, auditable Pipeline Scan baseline management to the Veracode GitHub Workflow Integration. Baselines (and mitigation-aware baselines) live inside the `veracode` integration repo itself, are configured entirely from `veracode.yml`, and drive delta scans that fail only on net-new findings. A bulk rollout script deploys the whole feature across many organizations' `veracode` repos at scale, with rate limiting, checkpoint/resume, and audit trails.

This feature is meant to be added to the repo you import from `github.com/veracode/github-actions-integration` and name `veracode` in your organization.

-----

## How It Works

The feature has two workflows and one store, all configured by a single `baseline:` block in `veracode.yml`:

1. **Refresh** (`veracode-baseline-refresh.yml`) runs on a schedule, on default-branch merges, or on demand. It packages a target repo, runs a full Pipeline Scan per build artifact, optionally applies the mitigation overlay, and records each baseline (with metadata) into the store inside the `veracode` repo.
1. **Delta** (`veracode-baseline-delta.yml`) runs on push and pull request. It packages the target repo, pulls each artifact's stored baseline, and runs the Pipeline Scan with `--baseline_file`, breaking the build only on new findings at or above the configured severity.
1. **Store** lives at `baselines/<org>/<repo>/<branch>/<artifact>/` in the `veracode` repo. Each artifact carries `baseline.json`, an optional `baseline-mitigated-findings.json`, and a `meta.json` (scan id, engine version, finding counts, sha256, commit). A per-repo `manifest.json` summarizes branches and artifacts.

Because the store lives in the `veracode` repo, the refresh workflow commits with the repo's own `GITHUB_TOKEN`. There is no separate baseline repository and no long-lived personal access token.

> **The target repo is identified by `org/repo`**, the same convention the integration already uses, so no extra naming scheme is needed.

-----

## Repository Layout

```
veracode/
├── veracode.yml                                  # add the baseline: block (auto-injected by the rollout script)
├── baselines/                                    # the store (created on first refresh)
│   └── <org>/<repo>/<branch>/<artifact>/
│       ├── baseline.json
│       ├── baseline-mitigated-findings.json
│       └── meta.json
│   └── <org>/<repo>/manifest.json
├── helper/baseline/
│   ├── baseline_manager.py                       # store engine (standard library only)
│   ├── config.py                                 # reads the baseline block from veracode.yml
│   ├── requirements.txt
│   └── veracode.baseline.block.yml               # the block the rollout script injects
├── tools/
│   └── rollout.py              # bulk rollout across orgs
└── .github/workflows/
    ├── veracode-baseline-refresh.yml
    └── veracode-baseline-delta.yml
```

-----

## veracode.yml Configuration

All behaviour is controlled by a `baseline:` block under `veracode_static_scan`. `config.py` reads it and exports `BASELINE_*` variables that both workflows consume, so this is the only place you tune the feature.

```yaml
veracode_static_scan:
  # ... existing static scan settings stay as they are ...
  baseline:
    enabled: true                      # false keeps plain Pipeline Scans unchanged (staged rollout)
    mode: mitigated                    # none | full | mitigated
    store_branch: main                 # branch of the veracode repo holding the store
    fail_on_severity: "Very High, High"
    strict: false                      # true fails the delta when a baseline is missing
    prune_orphans: true                # drop baselines for artifacts that no longer exist
    refresh:
      on_schedule: true
      on_default_branch_push: true
```

|Key                            |Values                          |Meaning                                                       |
|-------------------------------|--------------------------------|--------------------------------------------------------------|
|`enabled`                      |`true` / `false`                |Master switch. When false, delta scans are skipped.           |
|`mode`                         |`none` / `full` / `mitigated`   |Which baseline the delta compares against.                    |
|`store_branch`                 |branch name                     |Branch of the `veracode` repo holding the store.              |
|`fail_on_severity`             |severity list                   |Severities that break the delta when introduced as new.       |
|`strict`                       |`true` / `false`                |Fail (vs scan without a baseline) when one is missing.        |
|`prune_orphans`                |`true` / `false`                |Remove stored baselines for artifacts no longer produced.     |
|`refresh.on_schedule`          |`true` / `false`                |Rebuild baselines on the scheduled run.                       |
|`refresh.on_default_branch_push`|`true` / `false`               |Rebuild a repo's baseline when it merges to its default branch.|

`mode: mitigated` ignores findings already mitigated or accepted on the Veracode platform and falls back to the full baseline when no mitigated file exists.

-----

## Wiring the Workflows

Both workflows are reusable (`workflow_call`) and take `target_repo` / `target_ref`, so they drop into the integration's master orchestration where the repo under scan is already known:

```yaml
jobs:
  baseline-delta:
    if: github.event_name == 'push' || github.event_name == 'pull_request'
    uses: ./.github/workflows/veracode-baseline-delta.yml
    with:
      target_repo: ${{ <repo the orchestration is scanning> }}
      target_ref:  ${{ <ref being scanned> }}
    secrets:
      VERACODE_API_ID: ${{ secrets.VERACODE_API_ID }}
      VERACODE_API_KEY: ${{ secrets.VERACODE_API_KEY }}
      TARGET_CHECKOUT_TOKEN: ${{ <installation token your master flow uses> }}

  baseline-refresh:
    if: github.event_name == 'schedule'
    uses: ./.github/workflows/veracode-baseline-refresh.yml
    with:
      target_repo: ${{ <repo> }}
      target_ref:  ${{ <default branch> }}
    secrets:
      VERACODE_API_ID: ${{ secrets.VERACODE_API_ID }}
      VERACODE_API_KEY: ${{ secrets.VERACODE_API_KEY }}
      TARGET_CHECKOUT_TOKEN: ${{ <installation token> }}
```

Tokens used by the workflows:

- **Store writes**: the refresh workflow uses `GITHUB_TOKEN` with `contents: write`. No PAT.
- **Target repo checkout**: pass the App installation token (or a PAT with read on target repos) as `TARGET_CHECKOUT_TOKEN`. This is the one seam to connect to whatever your master orchestration already uses to read target repos.

-----

## baseline_manager.py

The store engine. Standard library only, callable directly for local inspection or scripting.

```
baseline_manager.py record  --org O --repo R --branch B --artifact A --results results.json [--mitigated m.json]
baseline_manager.py pull    --org O --repo R --branch B --artifact A --mode mitigated --dest out.json [--strict]
baseline_manager.py mitigate --script vcpipemit.py --results results.json --applicationname R --output m.json
baseline_manager.py prune   --org O --repo R --branch B --keep a.jar b.war
baseline_manager.py status  --org O --repo R --branch B
```

|Command   |Purpose                                                                       |
|----------|------------------------------------------------------------------------------|
|`record`  |Validate a results file and store it with metadata + manifest update.         |
|`pull`    |Copy a stored baseline into the workspace for a delta scan (sentinel if absent).|
|`mitigate`|Run the mitigation overlay in an isolated dir and emit one validated file.     |
|`prune`   |Remove stored artifact baselines that no longer exist.                         |
|`status`  |Print the manifest for one repo/branch.                                        |

Validation rejects any results file that is not valid JSON, lacks a `findings` array, or did not scan successfully, so a broken scan never poisons the store. Concurrent refreshes of different repos never contend because each writes only under its own path and a per-repo manifest.

-----

## Bulk Rollout

`tools/rollout.py` deploys this feature into the `veracode` repo of every organization that already has the integration onboarded. For each org it:

1. Confirms the `veracode` repo exists and is not empty (skips otherwise; onboard the integration first).
1. Deploys `helper/baseline/*` and the two baseline workflows via the GitHub Contents API (create, update on change, or skip when identical).
1. Reconciles the `baseline:` block under `veracode_static_scan` in `veracode.yml`. It inserts the block as the first child when missing, and when a block already exists it replaces exactly that block with the canonical one if the two differ. Everything outside the baseline block (other keys, sibling comments, blank lines, and the SCA and IaC blocks) is preserved unchanged, and re-runs converge to `already_current`. Use `--yml-insert-only` to add a missing block but never modify an existing one (preserves per-repo hand-tuned settings).

It calls no Veracode API. The rollout is pure GitHub content work. It mirrors the integration rollout helper's conventions, dry-run by default, enterprise/orgs-file discovery, checkpoint/resume, parallel workers, a global GitHub rate limiter, and CSV + JSON output.

### Modes

|Mode   |Flag       |Behavior                                                       |
|-------|-----------|---------------------------------------------------------------|
|Dry-run|*(default)*|Reports what would change per org. No writes.                  |
|Apply  |`--apply`  |Deploys files and injects the veracode.yml block.             |

### Quickstart

```bash
export GITHUB_TOKEN="..."

# Phase 1: audit what would change across the fleet
python tools/rollout.py --enterprise YOUR-ENTERPRISE

# Phase 2: deploy, 5 workers
python tools/rollout.py --apply --enterprise YOUR-ENTERPRISE --workers 5
```

Run it from the `veracode` repo root so `--assets-dir` defaults to the repo and reads the canonical asset files.

### Requirements

```bash
pip install requests pyyaml
git --version        # not required by the rollout itself; needed by the workflows at scan time
```

Python 3.10+ (the tooling uses modern type-hint syntax).

### GitHub Token Permissions

|Operation                     |Required Scopes                          |
|------------------------------|-----------------------------------------|
|Dry-run                       |`read:org`                               |
|`--enterprise` (org discovery)|+ `read:enterprise`                      |
|`--apply`                     |+ `repo`, `workflow`                     |

`workflow` scope is required because the rollout writes files under `.github/workflows/`. No `admin:org` is needed; the rollout sets no secrets.

### Command-Line Reference

|Flag                |Default                 |Description                                                                 |
|--------------------|------------------------|---------------------------------------------------------------------------|
|`--apply`           |off (dry-run)           |Make changes. Without it, the run is read-only.                            |
|`--enterprise SLUG` |-                       |Discover orgs via the enterprise GraphQL API.                             |
|`--orgs-file FILE`  |-                       |One org per line. Used as scope alone, or as a filter with `--enterprise`. |
|`--assets-dir DIR`  |repo root               |Where the asset files are read from.                                       |
|`--repo-name NAME`  |`veracode`              |Integration repo name in each org.                                         |
|`--branch NAME`     |`main`                  |Branch to write to in each veracode repo.                                  |
|`--skip-files`      |off                     |Inject veracode.yml only; do not deploy files.                            |
|`--skip-yml`        |off                     |Deploy files only; do not touch veracode.yml.                             |
|`--yml-insert-only` |off                     |Add the block only when missing; never modify an existing one.            |
|`--force`           |off                     |Re-PUT files even when content is identical.                              |
|`--api-base URL`    |`https://api.github.com`|Override for GHES.                                                         |
|`--web-base URL`    |`https://github.com`    |Override for GHES.                                                         |
|`--token-env VAR`   |`GITHUB_TOKEN`          |Env var holding the GitHub PAT.                                            |
|`--out DIR`         |`./out`                 |Output directory.                                                          |
|`--skip-to ORG`     |-                       |Skip all orgs before this one.                                            |
|`--continue`        |-                       |Resume from `out/checkpoint.json`.                                        |
|`--workers N`       |`1`                     |Parallel worker threads (recommended 3-5).                               |

### Output Files

|File                             |Description                                                |
|---------------------------------|-----------------------------------------------------------|
|`orgs.txt`                       |All discovered orgs, one per line.                         |
|`baseline_rollout_<timestamp>.json`|Per-org result log, written incrementally (crash-safe).  |
|`checkpoint.json`                |Completed orgs, for `--continue`.                          |
|`missing_veracode_repo.csv`      |Orgs whose `veracode` repo is missing or empty.            |
|`baseline_rollout_issues.csv`    |Orgs where any file or yml step failed.                    |

### Audit Report Example

```json
{
  "org": "acme-dev",
  "status": "ok",
  "files": {
    "helper/baseline/baseline_manager.py": "created",
    "helper/baseline/config.py": "created",
    "helper/baseline/requirements.txt": "created",
    ".github/workflows/veracode-baseline-refresh.yml": "created",
    ".github/workflows/veracode-baseline-delta.yml": "created"
  },
  "veracode_yml": "injected"
}
```

`files.<path>` values: `created`, `updated`, `unchanged`, `would_create`, `would_update`, `failed:<reason>`.

`veracode_yml` values: `injected`, `updated`, `already_current`, `already_present` (insert-only mode), `no_static_block`, `veracode_yml_missing`, `would_inject`, `would_update`, `failed:<reason>`.

`status` values: `ok`, `repo_missing`, `repo_empty`, `error`.

### Parallel Execution

By default the rollout processes one org at a time. `--workers N` runs orgs concurrently with a thread pool. A full apply averages about six writes per org (five files plus one yml), so the binding constraint is GitHub's content-creation secondary limit, not raw throughput.

|Window                            |GitHub limit|Tool's safe target  |
|----------------------------------|------------|--------------------|
|Primary hourly requests           |5,000/hour  |4,000/hour (80%)    |
|Content-creating writes per minute|80/min      |60/min (75%)        |
|Content-creating writes per hour  |500/hour    |400/hour (80%)      |
|Concurrent in-flight requests     |100         |50 (50%)            |

A global limiter shared by all workers paces writes to stay below these ceilings, sleeping until the oldest event in a window ages out. `Retry-After` is honored on secondary-limit responses, and `X-RateLimit-Remaining` is checked as a backstop. The execution summary prints the limiter's final state. `--workers` composes with `--continue`: the checkpoint records all completed orgs and the run replays from there regardless of completion order.

-----

## Migrating Existing Baselines

If you previously stored baselines in a separate template repo using the `<org>_<repo>` slug layout, carry them forward to the nested `<org>/<repo>` layout:

```bash
# from the old store root
for d in baselines/*_*/; do
  slug=$(basename "$d")
  org=${slug%%_*}; repo=${slug#*_}
  mkdir -p "NEW/baselines/$org/$repo"
  cp -r "$d"/* "NEW/baselines/$org/$repo/"
done
```

`meta.json` and `manifest.json` are regenerated on the next refresh, so migrated baselines work immediately and gain metadata when they next rebuild. The slug split above assumes the org name contains no underscore, which is why the new layout uses nested directories.

-----

## Security Notes

- The store lives in the `veracode` repo; the refresh workflow commits with `GITHUB_TOKEN` (`contents: write`). No long-lived PAT is used for storage.
- The bulk rollout sets no secrets and calls no Veracode API. It needs `repo` and `workflow` scope to write content, nothing more.
- All baseline files are validated before they enter the store, so a failed or malformed scan cannot overwrite a good baseline.
- The rollout is read-only by default; all changes require explicit `--apply`.
- veracode.yml injection is additive and idempotent; existing configuration and comments are preserved.

-----

## Support

Supported platforms: GitHub.com, GitHub Enterprise Cloud, GitHub Enterprise Server (set `--api-base` and `--web-base`).

For issues, provide `out/baseline_rollout_<timestamp>.json`, your platform type, and the command used.

> This is a community tool and is not officially supported by Veracode.