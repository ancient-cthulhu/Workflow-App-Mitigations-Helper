#!/usr/bin/env python3
"""Read the baseline block out of veracode.yml and emit shell-friendly env lines.

The Workflow Integration is configured entirely through veracode.yml, so the
baseline feature is too: it reads veracode_static_scan.baseline and writes
KEY=value lines (defaults applied) suitable for appending to $GITHUB_ENV.

Usage:
    python3 config.py [--file veracode.yml] >> "$GITHUB_ENV"
"""
from __future__ import annotations

import argparse
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced clearly in CI
    sys.stderr.write("PyYAML is required (pip install pyyaml)\n")
    raise SystemExit(1)

DEFAULTS = {
    "BASELINE_ENABLED": "false",
    "BASELINE_MODE": "mitigated",          # none | full | mitigated
    "BASELINE_STORE_BRANCH": "main",       # branch in the veracode repo store
    "BASELINE_FAIL_SEVERITY": "Very High, High",
    "BASELINE_STRICT": "false",            # fail delta if a baseline is missing
    "BASELINE_REFRESH_ON_SCHEDULE": "true",
    "BASELINE_REFRESH_ON_DEFAULT_PUSH": "true",
    "BASELINE_PRUNE_ORPHANS": "true",
}

# veracode.yml key -> env key
KEYMAP = {
    "enabled": "BASELINE_ENABLED",
    "mode": "BASELINE_MODE",
    "store_branch": "BASELINE_STORE_BRANCH",
    "fail_on_severity": "BASELINE_FAIL_SEVERITY",
    "strict": "BASELINE_STRICT",
    "prune_orphans": "BASELINE_PRUNE_ORPHANS",
}


def _as_str(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def emit(path: str):
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    baseline = (doc.get("veracode_static_scan") or {}).get("baseline") or {}

    out = dict(DEFAULTS)
    for ykey, env_key in KEYMAP.items():
        if ykey in baseline and baseline[ykey] is not None:
            out[env_key] = _as_str(baseline[ykey])

    refresh = baseline.get("refresh") or {}
    if "on_schedule" in refresh:
        out["BASELINE_REFRESH_ON_SCHEDULE"] = _as_str(refresh["on_schedule"])
    if "on_default_branch_push" in refresh:
        out["BASELINE_REFRESH_ON_DEFAULT_PUSH"] = _as_str(refresh["on_default_branch_push"])

    for k, v in out.items():
        print(f"{k}={v}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--file", default="veracode.yml")
    args = p.parse_args(argv)
    emit(args.file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
