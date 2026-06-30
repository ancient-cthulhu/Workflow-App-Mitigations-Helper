#!/usr/bin/env python3
"""Veracode baseline + mitigation store manager.

Lives inside the Veracode GitHub Workflow Integration repo (the repo you import
into your org and name 'veracode'). It owns the on-repo baseline store used for
Pipeline Scan delta scans, including mitigation-aware baselines.

Design goals (vs a pile of inline bash):
  * Deterministic, browsable layout keyed by the org/repo convention the
    workflow integration already uses: baselines/<org>/<repo>/<branch>/<artifact>/
  * Every stored baseline carries meta.json (scan_id, engine_version, finding
    counts, sha256, commit, timestamp) so the store is auditable.
  * Validation on write so a broken results.json never poisons a baseline.
  * Deterministic mitigation-overlay handling (no `ls -t` guessing).
  * Per-repo manifest.json instead of one global index, so concurrent refreshes
    of different repos never contend on the same file.
  * Standard library only. The only external dependency is the mitigation
    helper script (vcpipemit.py), invoked as a subprocess.

CLI:
  path      print the store dir for one artifact
  record    validate + store baseline(.json) [+ mitigated] + meta + manifest
  pull      copy a stored baseline into the workspace for a delta scan
  mitigate  fetch approved platform mitigations (by app profile) as a baseline
  merge     union a tech-debt baseline with a mitigations baseline
  prune     drop artifact dirs that no longer exist (artifact renames/removals)
  status    print the manifest for one repo/branch
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STORE_ROOT = "baselines"
BASELINE_NAME = "baseline.json"
MITIGATED_NAME = "baseline-mitigated-findings.json"
META_NAME = "meta.json"
MANIFEST_NAME = "manifest.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISSING = 3  # sentinel: no baseline found (non-strict callers continue)

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(value: str) -> str:
    """Filesystem-safe component. Reversible enough to stay human readable."""
    return _SAFE.sub("_", value.strip())


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _dump_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def repo_dir(store: Path, org: str, repo: str) -> Path:
    return store / _safe(org) / _safe(repo)


def artifact_dir(store: Path, org: str, repo: str, branch: str, artifact: str) -> Path:
    return repo_dir(store, org, repo) / _safe(branch) / _safe(artifact)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def validate_results(path: Path, *, require_success: bool = False) -> int:
    """Validate a Pipeline Scan results / mitigated-findings file.

    Returns the finding count. Raises ValueError on a structurally bad file so
    callers can fail loudly instead of committing garbage to the store.
    """
    if not path.is_file():
        raise ValueError(f"file not found: {path}")
    try:
        data = _load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} top-level is not an object")
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise ValueError(f"{path} has no 'findings' array")
    if require_success and data.get("scan_status") not in (None, "SUCCESS"):
        raise ValueError(f"{path} scan_status is {data.get('scan_status')!r}")
    return len(findings)


def _scan_meta(results: Path) -> dict:
    """Pull non-identifying scan metadata out of a results.json if present."""
    try:
        data = _load_json(results)
    except Exception:
        return {}
    return {
        k: data.get(k)
        for k in ("scan_id", "scan_status", "engine_version", "pipeline_scan", "dev_stage")
        if data.get(k) is not None
    }


# --------------------------------------------------------------------------- #
# manifest (per repo, not global, to avoid cross-repo commit contention)
# --------------------------------------------------------------------------- #
def _manifest_path(store: Path, org: str, repo: str) -> Path:
    return repo_dir(store, org, repo) / MANIFEST_NAME


def _update_manifest(store: Path, org: str, repo: str, branch: str, artifact: str, entry: dict) -> None:
    mpath = _manifest_path(store, org, repo)
    manifest = _load_json(mpath) if mpath.is_file() else {"org": org, "repo": repo, "branches": {}}
    branches = manifest.setdefault("branches", {})
    arts = branches.setdefault(branch, {})
    arts[artifact] = entry
    manifest["updated_at"] = _now()
    _dump_json(mpath, manifest)


def _drop_from_manifest(store: Path, org: str, repo: str, branch: str, artifacts) -> None:
    mpath = _manifest_path(store, org, repo)
    if not mpath.is_file():
        return
    manifest = _load_json(mpath)
    arts = manifest.get("branches", {}).get(branch, {})
    for a in artifacts:
        arts.pop(a, None)
    manifest["updated_at"] = _now()
    _dump_json(mpath, manifest)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_path(args) -> int:
    print(artifact_dir(Path(args.store), args.org, args.repo, args.branch, args.artifact))
    return EXIT_OK


def cmd_record(args) -> int:
    store = Path(args.store)
    results = Path(args.results)
    try:
        total = validate_results(results, require_success=True)
    except ValueError as exc:
        print(f"::error::baseline rejected: {exc}", file=sys.stderr)
        return EXIT_ERROR

    out = artifact_dir(store, args.org, args.repo, args.branch, args.artifact)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(results, out / BASELINE_NAME)

    meta = {
        "org": args.org,
        "repo": args.repo,
        "branch": args.branch,
        "artifact": args.artifact,
        "total_findings": total,
        "baseline_sha256": _sha256(out / BASELINE_NAME),
        "source_commit": args.commit,
        "recorded_at": _now(),
        "recorded_by": args.actor,
        "scan": _scan_meta(results),
    }

    mitigated_count = None
    if args.mitigated and Path(args.mitigated).is_file():
        try:
            mitigated_count = validate_results(Path(args.mitigated))
        except ValueError as exc:
            print(f"::warning::mitigated file ignored: {exc}", file=sys.stderr)
        else:
            shutil.copyfile(args.mitigated, out / MITIGATED_NAME)
            meta["mitigated_findings"] = mitigated_count
            meta["mitigated_sha256"] = _sha256(out / MITIGATED_NAME)

    _dump_json(out / META_NAME, meta)
    _update_manifest(
        store, args.org, args.repo, args.branch, _safe(args.artifact),
        {
            "total_findings": total,
            "mitigated_findings": mitigated_count,
            "updated_at": meta["recorded_at"],
            "commit": args.commit,
            "scan_id": meta["scan"].get("scan_id"),
        },
    )
    extra = "" if mitigated_count is None else f", mitigated {mitigated_count}"
    print(f"recorded {args.artifact}: {total} findings{extra} -> {out}")
    return EXIT_OK


def cmd_pull(args) -> int:
    store = Path(args.store)
    src = artifact_dir(store, args.org, args.repo, args.branch, args.artifact)
    name = MITIGATED_NAME if args.mode == "mitigated" else BASELINE_NAME
    candidate = src / name

    # mitigated mode falls back to the full baseline if no mitigated file exists
    if args.mode == "mitigated" and not candidate.is_file():
        candidate = src / BASELINE_NAME

    if not candidate.is_file():
        msg = f"no baseline for {args.org}/{args.repo}@{args.branch} :: {args.artifact}"
        if args.strict:
            print(f"::error::{msg}", file=sys.stderr)
            return EXIT_ERROR
        print(f"::warning::{msg} (delta will scan without a baseline)", file=sys.stderr)
        return EXIT_MISSING

    dest = Path(args.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(candidate, dest)
    print(str(dest))
    return EXIT_OK


def _resolve_app_guid(app_name: str, api_id: str, api_key: str) -> str | None:
    """Resolve a Veracode application profile name (the deterministic org/repo the
    Workflow Integration uses as appname) to its GUID, which is what vcpipemit
    requires. Returns the guid, or None if no exact-name match is found.
    """
    import requests  # lazy: only needed for the live mitigation overlay
    from veracode_api_signing.plugin_requests import RequestsAuthPluginVeracodeHMAC

    auth = RequestsAuthPluginVeracodeHMAC(api_key_id=api_id, api_key_secret=api_key)
    url = "https://api.veracode.com/appsec/v1/applications"
    page = 0
    while True:
        r = requests.get(url, params={"name": app_name, "size": 100, "page": page}, auth=auth, timeout=45)
        if r.status_code != 200:
            print(f"::warning::application lookup for {app_name!r} returned {r.status_code}", file=sys.stderr)
            return None
        body = r.json()
        for app in body.get("_embedded", {}).get("applications", []):
            if (app.get("profile") or {}).get("name") == app_name:
                return app.get("guid")
        meta = body.get("page", {})
        if page >= meta.get("total_pages", 1) - 1:
            return None
        page += 1


def cmd_mitigate(args) -> int:
    """Fetch APPROVED platform mitigations for the app profile and emit them as a
    Pipeline Scan baseline file.

    The app profile name is the deterministic org/repo the Workflow Integration
    uses as appname; this resolves it to a GUID and runs vcpipemit (-a <guid>),
    which writes baseline-<guid>.json. Output is isolated in a temp dir so the
    produced file is located deterministically (no `ls -t` guessing).

    Credentials are read from VERACODE_API_KEY_ID / VERACODE_API_KEY_SECRET (the
    same variables vcpipemit itself uses) or from --api-id / --api-key.
    """
    script = Path(args.script)
    results = Path(args.results)
    if not script.is_file():
        print(f"::error::mitigation script not found: {script}", file=sys.stderr)
        return EXIT_ERROR

    api_id = args.api_id or os.environ.get("VERACODE_API_KEY_ID") or os.environ.get("VERACODE_API_ID")
    api_key = args.api_key or os.environ.get("VERACODE_API_KEY_SECRET") or os.environ.get("VERACODE_API_KEY")
    if not api_id or not api_key:
        print("::warning::no Veracode credentials for mitigation overlay; "
              "delta will run without platform mitigations", file=sys.stderr)
        return EXIT_MISSING

    guid = args.app_guid or _resolve_app_guid(args.app_name, api_id, api_key)
    if not guid:
        print(f"::warning::no application profile named {args.app_name!r} on the platform; "
              "delta will run without platform mitigations", file=sys.stderr)
        return EXIT_MISSING

    cmd = [sys.executable, str(script.resolve()), "-a", guid, "-rf", BASELINE_NAME]
    if args.sandbox_guid:
        cmd += ["-s", args.sandbox_guid]

    env = {**os.environ, "VERACODE_API_KEY_ID": api_id, "VERACODE_API_KEY_SECRET": api_key}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shutil.copyfile(results, tmp / BASELINE_NAME)
        before = set(tmp.glob("*.json"))
        proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, env=env)
        produced = [p for p in tmp.glob("baseline-*.json") if p not in before]
        if proc.returncode != 0 or not produced:
            sys.stderr.write(proc.stdout + proc.stderr)
            print("::warning::mitigation overlay produced no file; "
                  "delta will run without platform mitigations", file=sys.stderr)
            return EXIT_MISSING
        chosen = max(produced, key=lambda p: p.stat().st_mtime)
        try:
            count = validate_results(chosen)
        except ValueError as exc:
            print(f"::warning::mitigation output invalid: {exc}", file=sys.stderr)
            return EXIT_MISSING
        dest = Path(args.output)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(chosen, dest)
        print(f"approved platform mitigations for {args.app_name}: {count} -> {dest}")
    return EXIT_OK


def _finding_key(finding: dict) -> str:
    """Stable identity for a pipeline finding, matching how Pipeline Scan keys
    baselines: the flaw_match object, falling back to type + location. issue_id
    is per-scan and deliberately excluded.
    """
    fm = finding.get("flaw_match")
    if isinstance(fm, dict) and fm:
        return "fm:" + json.dumps(fm, sort_keys=True)
    src = ((finding.get("files") or {}).get("source_file") or {})
    return "loc:" + json.dumps(
        [finding.get("cwe_id"), finding.get("issue_type_id"), src.get("file"), src.get("line")],
        sort_keys=True,
    )


def cmd_merge(args) -> int:
    """Union a tech-debt baseline with an approved-mitigations baseline into one
    Pipeline Scan baseline, so a single --baseline_file suppresses both existing
    findings (net-new gating) and platform-mitigated findings. Either input may
    be absent.
    """
    base = Path(args.baseline) if args.baseline else None
    mit = Path(args.mitigations) if args.mitigations else None
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    have_base = bool(base and base.is_file())
    have_mit = bool(mit and mit.is_file())

    if not have_base and not have_mit:
        print("::warning::no baseline and no mitigations to merge", file=sys.stderr)
        return EXIT_MISSING
    if have_base and not have_mit:
        shutil.copyfile(base, out)
        print(f"merged: baseline only -> {out}")
        return EXIT_OK
    if have_mit and not have_base:
        shutil.copyfile(mit, out)
        print(f"merged: mitigations only -> {out}")
        return EXIT_OK

    base_doc = _load_json(base)
    mit_doc = _load_json(mit)
    findings = list(base_doc.get("findings") or [])
    seen = {_finding_key(f) for f in findings}
    added = 0
    for f in mit_doc.get("findings") or []:
        k = _finding_key(f)
        if k not in seen:
            seen.add(k)
            findings.append(f)
            added += 1
    base_doc["findings"] = findings
    _dump_json(out, base_doc)
    print(f"merged: {len(findings) - added} baseline + {added} new mitigated = {len(findings)} -> {out}")
    return EXIT_OK


def cmd_prune(args) -> int:
    store = Path(args.store)
    branch_dir = repo_dir(store, args.org, args.repo) / _safe(args.branch)
    if not branch_dir.is_dir():
        return EXIT_OK
    keep = {_safe(a) for a in args.keep}
    removed = []
    for child in sorted(branch_dir.iterdir()):
        if child.is_dir() and child.name not in keep:
            shutil.rmtree(child)
            removed.append(child.name)
    if removed:
        _drop_from_manifest(store, args.org, args.repo, args.branch, removed)
        print(f"pruned orphan artifacts: {', '.join(removed)}")
    else:
        print("no orphan artifacts to prune")
    return EXIT_OK


def cmd_status(args) -> int:
    mpath = _manifest_path(Path(args.store), args.org, args.repo)
    if not mpath.is_file():
        print(f"no baselines recorded for {args.org}/{args.repo}")
        return EXIT_OK
    manifest = _load_json(mpath)
    arts = manifest.get("branches", {}).get(args.branch, {})
    if not arts:
        print(f"no baselines on branch {args.branch}")
        return EXIT_OK
    width = max(len(a) for a in arts)
    print(f"{'artifact'.ljust(width)}  total  mitigated  updated_at")
    for name, e in sorted(arts.items()):
        print(f"{name.ljust(width)}  {str(e.get('total_findings','?')).rjust(5)}  "
              f"{str(e.get('mitigated_findings','-')).rjust(9)}  {e.get('updated_at','')}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# arg parsing
# --------------------------------------------------------------------------- #
def _common_ids(p, *, artifact=True, branch=True):
    p.add_argument("--store", default=os.environ.get("BASELINE_STORE", STORE_ROOT))
    p.add_argument("--org", required=True)
    p.add_argument("--repo", required=True)
    if branch:
        p.add_argument("--branch", required=True)
    if artifact:
        p.add_argument("--artifact", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baseline_manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("path", help="print the store dir for one artifact")
    _common_ids(p)
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("record", help="validate and store a baseline")
    _common_ids(p)
    p.add_argument("--results", required=True, help="pipeline-scan results.json")
    p.add_argument("--mitigated", help="mitigation overlay output (optional)")
    p.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    p.add_argument("--actor", default=os.environ.get("GITHUB_ACTOR", ""))
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("pull", help="copy a stored baseline for a delta scan")
    _common_ids(p)
    p.add_argument("--mode", choices=("full", "mitigated"), default="full")
    p.add_argument("--dest", required=True, help="where to write the baseline file")
    p.add_argument("--strict", action="store_true", help="fail if no baseline exists")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("mitigate", help="fetch approved platform mitigations as a baseline file")
    p.add_argument("--script", required=True, help="path to vcpipemit.py")
    p.add_argument("--results", required=True, help="pipeline-scan results.json to match mitigations against")
    p.add_argument("--app-name", required=True, help="application profile name (the org/repo appname)")
    p.add_argument("--app-guid", help="application GUID (skips name resolution)")
    p.add_argument("--sandbox-guid", help="sandbox GUID to pull mitigations from (optional)")
    p.add_argument("--api-id", help="Veracode API id (else VERACODE_API_KEY_ID / VERACODE_API_ID env)")
    p.add_argument("--api-key", help="Veracode API key (else VERACODE_API_KEY_SECRET / VERACODE_API_KEY env)")
    p.add_argument("--output", default=MITIGATED_NAME)
    p.set_defaults(func=cmd_mitigate)

    p = sub.add_parser("merge", help="union a tech-debt baseline with a mitigations baseline")
    p.add_argument("--baseline", help="tech-debt baseline file (optional)")
    p.add_argument("--mitigations", help="approved-mitigations baseline file (optional)")
    p.add_argument("--output", required=True, help="combined baseline output path")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("prune", help="remove orphan artifact dirs")
    _common_ids(p, artifact=False)
    p.add_argument("--keep", nargs="*", default=[], help="artifacts that still exist")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("status", help="print the manifest for a repo/branch")
    _common_ids(p, artifact=False)
    p.set_defaults(func=cmd_status)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
