#!/usr/bin/env python3
"""Bulk rollout of the Veracode baseline + mitigation feature across orgs.

This is a standalone admin tool (run by a CSE/admin), separate from the per-org
workflows. It pushes the baseline feature into the `veracode` integration repo
of every org that already has the GitHub Workflow Integration onboarded:

  * deploys helper/baseline/* and the two baseline workflows
  * injects the `baseline:` block under veracode_static_scan in veracode.yml,
    comment-preserving and idempotent

It deliberately mirrors the conventions of the integration rollout helper
(dry-run by default, --apply to change, enterprise/orgs-file discovery,
checkpoint/resume, parallel workers, a global GitHub rate limiter, and CSV +
JSON audit output) so it slots into the same operational muscle memory. It does
NOT call any Veracode API: baseline rollout is pure GitHub content work.

Assets are read from --assets-dir (default: this script's own directory), which
is expected to contain helper/baseline/* and .github/workflows/veracode-baseline-*.yml.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from base64 import b64decode, b64encode
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

API_VER = "2022-11-28"
DEFAULT_REPO_NAME = "veracode"

# target path in the veracode repo -> source path relative to --assets-dir
ASSET_FILES: dict[str, str] = {
    "helper/baseline/baseline_manager.py": "helper/baseline/baseline_manager.py",
    "helper/baseline/config.py": "helper/baseline/config.py",
    "helper/baseline/requirements.txt": "helper/baseline/requirements.txt",
    ".github/workflows/veracode-baseline-refresh.yml": ".github/workflows/veracode-baseline-refresh.yml",
    ".github/workflows/veracode-baseline-delta.yml": ".github/workflows/veracode-baseline-delta.yml",
}
BASELINE_BLOCK_FILE = "helper/baseline/veracode.baseline.block.yml"

_print_lock = threading.Lock()
_CONTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def tprint(*args: Any, **kwargs: Any) -> None:
    with _print_lock:
        print(*args, **kwargs)


def env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


# ---------------------------------------------------------------------------
# Global rate limiter (content writes are the binding GitHub constraint)
# ---------------------------------------------------------------------------

class _SlidingWindow:
    __slots__ = ("window", "_events", "_lock")

    def __init__(self, window_seconds: float) -> None:
        self.window = window_seconds
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, cutoff: float) -> None:
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def add(self) -> None:
        now = time.time()
        with self._lock:
            self._events.append(now)
            self._prune(now - self.window)

    def count(self) -> int:
        with self._lock:
            self._prune(time.time() - self.window)
            return len(self._events)

    def oldest(self) -> float | None:
        with self._lock:
            self._prune(time.time() - self.window)
            return self._events[0] if self._events else None


class _RateLimiter:
    """Shared limiter: hourly primary budget + content write minute/hour budgets."""

    def __init__(self) -> None:
        self.hourly = _SlidingWindow(3600)
        self.content_minute = _SlidingWindow(60)
        self.content_hour = _SlidingWindow(3600)
        self.concurrent = threading.Semaphore(50)
        self.hourly_cap = int(5000 * 0.80)      # 4000/hour
        self.content_min_cap = int(80 * 0.75)   # 60/min
        self.content_hour_cap = int(500 * 0.80)  # 400/hour
        self._warn_lock = threading.Lock()
        self._last_warn = 0.0

    def _warn(self, msg: str) -> None:
        now = time.time()
        with self._warn_lock:
            if now - self._last_warn < 10:
                return
            self._last_warn = now
        tprint(msg)

    def _wait_window(self, win: _SlidingWindow, cap: int, span: int, label: str, max_sleep: int) -> None:
        while win.count() >= cap:
            oldest = win.oldest()
            wait = max((oldest + span) - time.time(), 1.0) if oldest else float(max_sleep)
            self._warn(f"  [RATE LIMIT] {label} budget reached; pacing {int(wait)}s.")
            time.sleep(min(wait, max_sleep))

    def acquire(self, method: str) -> None:
        self._wait_window(self.hourly, self.hourly_cap, 3600, "hourly request", 30)
        if method.upper() in _CONTENT_METHODS:
            self._wait_window(self.content_minute, self.content_min_cap, 60, "content-write per-minute", 10)
            self._wait_window(self.content_hour, self.content_hour_cap, 3600, "content-write per-hour", 60)
        self.concurrent.acquire()
        self.hourly.add()
        if method.upper() in _CONTENT_METHODS:
            self.content_minute.add()
            self.content_hour.add()

    def release(self) -> None:
        self.concurrent.release()

    def snapshot(self) -> dict[str, int]:
        return {
            "requests_last_hour": self.hourly.count(),
            "hourly_cap": self.hourly_cap,
            "writes_last_hour": self.content_hour.count(),
            "content_hour_cap": self.content_hour_cap,
            "writes_last_minute": self.content_minute.count(),
            "content_min_cap": self.content_min_cap,
        }


_rate_limiter = _RateLimiter()


# ---------------------------------------------------------------------------
# GitHub request wrapper with retry + reactive backstop
# ---------------------------------------------------------------------------

def gh_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VER,
        "User-Agent": "veracode-baseline-rollout",
    }


def _check_rate_headers(r: requests.Response) -> None:
    rem = r.headers.get("X-RateLimit-Remaining")
    reset = r.headers.get("X-RateLimit-Reset")
    if not rem or not reset:
        return
    try:
        remaining, reset_time = int(rem), int(reset)
    except ValueError:
        return
    if remaining < 50:
        wait = max(reset_time - int(time.time()), 0) + 5
        tprint(f"  [RATE LIMIT] GitHub reports {remaining} remaining; sleeping {min(wait, 300)}s.")
        time.sleep(min(wait, 300))


def request(method: str, url: str, token: str, max_retries: int = 3, **kwargs: Any) -> requests.Response:
    for attempt in range(max_retries):
        _rate_limiter.acquire(method)
        try:
            r = requests.request(method, url, headers=gh_headers(token), timeout=45, **kwargs)
        except requests.exceptions.RequestException as exc:
            if attempt < max_retries - 1:
                time.sleep((2 ** attempt) * 2)
                continue
            raise
        finally:
            _rate_limiter.release()

        is_secondary = r.status_code in (403, 429) and "secondary rate limit" in (r.text or "").lower()
        if r.status_code == 429 or is_secondary:
            if attempt < max_retries - 1:
                time.sleep(int(r.headers.get("Retry-After", 60)))
                continue
            return r
        if r.status_code >= 500 and attempt < max_retries - 1:
            time.sleep((2 ** attempt) * 2)
            continue
        _check_rate_headers(r)
        return r
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Pagination + org discovery
# ---------------------------------------------------------------------------

def _parse_next(link_header: str) -> str | None:
    for part in (p.strip() for p in link_header.split(",")):
        if 'rel="next"' in part:
            left = part.split(";")[0].strip()
            if left.startswith("<") and left.endswith(">"):
                return left[1:-1]
    return None


def paginate(url: str, token: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    next_url: str | None = url
    while next_url:
        r = request("GET", next_url, token, params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"GET {next_url} failed: {r.status_code} {r.text}")
        yield from r.json()
        link = r.headers.get("Link") or r.headers.get("link")
        next_url = _parse_next(link) if link else None
        params = None


def list_orgs_enterprise(api_base: str, token: str, enterprise: str) -> list[str] | None:
    graphql = "https://api.github.com/graphql" if "api.github.com" in api_base else f"{api_base}/graphql"
    query = """
    query($enterprise: String!, $cursor: String) {
      enterprise(slug: $enterprise) {
        organizations(first: 100, after: $cursor) {
          nodes { login }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    orgs: list[str] = []
    cursor: str | None = None
    while True:
        variables: dict[str, Any] = {"enterprise": enterprise}
        if cursor:
            variables["cursor"] = cursor
        r = request("POST", graphql, token, json={"query": query, "variables": variables})
        if r.status_code != 200:
            return None
        data = r.json()
        if "errors" in data or not data.get("data", {}).get("enterprise"):
            return None
        block = data["data"]["enterprise"]["organizations"]
        orgs.extend(n["login"] for n in block.get("nodes", []) if "login" in n)
        page = block.get("pageInfo", {})
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return orgs or None


def list_orgs(api_base: str, token: str, enterprise: str | None, orgs_file: str | None) -> list[str]:
    if enterprise:
        print(f'Discovering orgs via enterprise GraphQL: "{enterprise}"')
        orgs = list_orgs_enterprise(api_base, token, enterprise)
        if orgs:
            print(f"[OK] Found {len(orgs)} orgs")
            return orgs
        raise RuntimeError(f"Enterprise '{enterprise}' returned no orgs (check slug / read:enterprise scope)")
    if orgs_file:
        with open(orgs_file, encoding="utf-8") as f:
            orgs = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        if orgs:
            print(f"[OK] Found {len(orgs)} orgs from {orgs_file}")
            return orgs
        raise RuntimeError(f"File '{orgs_file}' contains no org names")
    print("Discovering orgs via /user/orgs")
    orgs = [o["login"] for o in paginate(f"{api_base}/user/orgs", token, params={"per_page": 100}) if "login" in o]
    if not orgs:
        raise RuntimeError("Could not determine org list (set --enterprise or --orgs-file)")
    print(f"[OK] Found {len(orgs)} orgs")
    return orgs


# ---------------------------------------------------------------------------
# GitHub content helpers
# ---------------------------------------------------------------------------

def repo_exists(api_base: str, org: str, repo: str, token: str) -> bool:
    r = request("GET", f"{api_base}/repos/{org}/{repo}", token)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(f"{org}/{repo}: repo check {r.status_code} {r.text[:120]}")


def repo_is_empty(api_base: str, org: str, repo: str, token: str) -> bool:
    r = request("GET", f"{api_base}/repos/{org}/{repo}/commits", token, params={"per_page": 1})
    if r.status_code == 409:
        return True
    if r.status_code == 200:
        return len(r.json()) == 0
    return False


def get_file(api_base: str, org: str, repo: str, path: str, token: str, branch: str) -> tuple[str | None, str | None]:
    """Return (sha, decoded_text) or (None, None) if the file does not exist."""
    r = request("GET", f"{api_base}/repos/{org}/{repo}/contents/{path}", token, params={"ref": branch})
    if r.status_code == 200:
        data = r.json()
        text = b64decode(data.get("content", "")).decode("utf-8")
        return data.get("sha"), text
    return None, None


def put_file(
    api_base: str, org: str, repo: str, path: str, token: str, branch: str,
    new_text: str, message: str, force: bool = False,
    log: Callable[[str], None] = tprint,
) -> str:
    """Create or update a file. Returns 'created' | 'updated' | 'unchanged' | 'failed:<...>'."""
    sha, current = get_file(api_base, org, repo, path, token, branch)
    if current is not None and not force and current == new_text:
        return "unchanged"

    payload: dict[str, Any] = {
        "message": message,
        "content": b64encode(new_text.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    r = request("PUT", f"{api_base}/repos/{org}/{repo}/contents/{path}", token, json=payload)
    if r.status_code == 409 and sha:
        # stale sha race: refetch once and retry
        fresh_sha, _ = get_file(api_base, org, repo, path, token, branch)
        if fresh_sha:
            payload["sha"] = fresh_sha
            r = request("PUT", f"{api_base}/repos/{org}/{repo}/contents/{path}", token, json=payload)

    if r.status_code in (200, 201):
        return "updated" if sha else "created"
    if r.status_code == 422 and "protected branch" in (r.text or "").lower():
        log(f"  [{org}] {path}: protected branch rejects direct push; manual PR needed")
        return "failed:protected_branch"
    log(f"  [{org}] {path}: PUT {r.status_code} {r.text[:160]}")
    return f"failed:{r.status_code}"


# ---------------------------------------------------------------------------
# veracode.yml baseline-block injection (comment-preserving, idempotent)
# ---------------------------------------------------------------------------

_STATIC_KEY_RE = re.compile(r"^veracode_static_scan\s*:\s*$")
_TOPLEVEL_KEY_RE = re.compile(r"^[A-Za-z0-9_]")
_BASELINE_CHILD_RE = re.compile(r"^(\s+)baseline\s*:")


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _reindent(fragment: str, target_indent: int, base_indent: int = 2) -> str:
    """Shift a 2-space-based fragment to a different base indent if ever needed."""
    if target_indent == base_indent:
        return fragment
    delta = target_indent - base_indent
    out: list[str] = []
    for ln in fragment.splitlines(keepends=True):
        if ln.strip() == "":
            out.append(ln)
        elif delta > 0:
            out.append(" " * delta + ln)
        else:
            strip_n = min(-delta, _line_indent(ln))
            out.append(ln[strip_n:])
    return "".join(out)


def reconcile_baseline_block(content: str, block: str, insert_only: bool = False) -> tuple[str, str]:
    """Insert the baseline block as the first child of veracode_static_scan, or,
    if a baseline block already exists, replace exactly that block with the
    canonical one when it differs. Nothing outside the baseline block is touched:
    other keys, sibling comments, blank lines, and other top-level scan blocks
    are all preserved byte-for-byte.

    Returns (new_content, action):
      'injected'        - block was missing and was added
      'updated'         - block existed with different content and was replaced
      'already_current' - block existed and already matched canonical
      'already_present' - block existed and insert_only=True (left untouched)
      'no_static_block' - no veracode_static_scan key found
    """
    lines = content.splitlines(keepends=True)

    start = next((i for i, ln in enumerate(lines) if _STATIC_KEY_RE.match(ln)), None)
    if start is None:
        return content, "no_static_block"

    # End of the veracode_static_scan block = next column-0 key (or EOF).
    static_end = len(lines)
    for j in range(start + 1, len(lines)):
        if _TOPLEVEL_KEY_RE.match(lines[j]):
            static_end = j
            break

    # Locate an existing baseline: child within the static block.
    b_idx: int | None = None
    b_indent = 2
    for j in range(start + 1, static_end):
        m = _BASELINE_CHILD_RE.match(lines[j])
        if m:
            b_idx = j
            b_indent = len(m.group(1))
            break

    fragment = block if block.endswith("\n") else block + "\n"

    if b_idx is None:
        # Insert as the first child, immediately after the static-scan key line.
        new_content = "".join(lines[:start + 1]) + fragment + "".join(lines[start + 1:])
        return new_content, "injected"

    if insert_only:
        return content, "already_present"

    # Determine the exact line range of the existing baseline block: the
    # baseline: line plus every following deeper-indented (child) line, stopping
    # at the next line whose indent is <= baseline's (a sibling key or comment).
    b_end = static_end
    for j in range(b_idx + 1, static_end):
        if lines[j].strip() == "":
            continue
        if _line_indent(lines[j]) <= b_indent:
            b_end = j
            break
    # Keep any trailing blank lines as a separator before the next sibling.
    while b_end - 1 > b_idx and lines[b_end - 1].strip() == "":
        b_end -= 1

    canonical = _reindent(fragment, b_indent)
    existing = "".join(lines[b_idx:b_end])
    if existing == canonical:
        return content, "already_current"

    new_content = "".join(lines[:b_idx]) + canonical + "".join(lines[b_end:])
    return new_content, "updated"


# ---------------------------------------------------------------------------
# Asset loading
# ---------------------------------------------------------------------------

def load_assets(assets_dir: Path) -> tuple[dict[str, str], str]:
    """Load the files to deploy and the baseline block. Raises if anything is missing."""
    files: dict[str, str] = {}
    for target, source in ASSET_FILES.items():
        p = assets_dir / source
        if not p.is_file():
            raise FileNotFoundError(f"asset not found: {p}")
        files[target] = p.read_text(encoding="utf-8")
    block_path = assets_dir / BASELINE_BLOCK_FILE
    if not block_path.is_file():
        raise FileNotFoundError(f"baseline block fragment not found: {block_path}")
    return files, block_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stats + context
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime | None = None
    total_orgs: int = 0
    processed: int = 0
    repos_ok: int = 0
    repos_missing: int = 0
    repos_empty: int = 0
    files_created: int = 0
    files_updated: int = 0
    files_unchanged: int = 0
    files_failed: int = 0
    yml_injected: int = 0
    yml_updated: int = 0
    yml_already: int = 0
    yml_no_static: int = 0
    yml_missing: int = 0
    yml_failed: int = 0
    orgs_with_failures: int = 0


@dataclass
class Ctx:
    api_base: str
    web_base: str
    token: str
    apply: bool
    branch: str
    repo_name: str
    skip_files: bool
    skip_yml: bool
    yml_insert_only: bool
    force: bool
    assets: dict[str, str]
    baseline_block: str
    total_orgs: int
    report_path: Path
    checkpoint_file: Path
    stats: Stats = field(default_factory=Stats)
    stats_lock: threading.Lock = field(default_factory=threading.Lock)
    report_lock: threading.Lock = field(default_factory=threading.Lock)
    rows_lock: threading.Lock = field(default_factory=threading.Lock)
    checkpoint_lock: threading.Lock = field(default_factory=threading.Lock)
    issue_rows: list[list[str]] = field(default_factory=list)
    missing_repo_rows: list[list[str]] = field(default_factory=list)
    completed_orgs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-org processing
# ---------------------------------------------------------------------------

def process_org(org: str, idx: int, ctx: Ctx) -> None:
    pct = (idx / ctx.total_orgs * 100) if ctx.total_orgs else 100.0
    lines: list[str] = [f"\n[{idx}/{ctx.total_orgs} ({pct:.1f}%)] {org}"]
    log = lines.append

    now = datetime.now()
    entry: dict[str, Any] = {"org": org, "timestamp": now.isoformat()}
    repo = ctx.repo_name
    had_failure = False

    try:
        if not repo_exists(ctx.api_base, org, repo, ctx.token):
            entry["status"] = "repo_missing"
            with ctx.stats_lock:
                ctx.stats.repos_missing += 1
            with ctx.rows_lock:
                ctx.missing_repo_rows.append([org, repo, "repo_missing"])
            log(f"  Repo [{repo}]: MISSING (onboard the integration first); skipping")
            _finish(org, idx, ctx, entry, lines, had_failure=False)
            return

        if repo_is_empty(ctx.api_base, org, repo, ctx.token):
            entry["status"] = "repo_empty"
            with ctx.stats_lock:
                ctx.stats.repos_empty += 1
            with ctx.rows_lock:
                ctx.missing_repo_rows.append([org, repo, "repo_empty"])
            log(f"  Repo [{repo}]: EMPTY (import not complete); skipping")
            _finish(org, idx, ctx, entry, lines, had_failure=False)
            return

        with ctx.stats_lock:
            ctx.stats.repos_ok += 1

        # --- deploy files -----------------------------------------------------
        file_actions: dict[str, str] = {}
        if not ctx.skip_files:
            for target, text in ctx.assets.items():
                if not ctx.apply:
                    sha, current = get_file(ctx.api_base, org, repo, target, ctx.token, ctx.branch)
                    if current is None:
                        action = "would_create"
                    elif current != text:
                        action = "would_update"
                    else:
                        action = "unchanged"
                else:
                    action = put_file(
                        ctx.api_base, org, repo, target, ctx.token, ctx.branch, text,
                        message=f"baseline: deploy {target} [skip ci]", force=ctx.force, log=log,
                    )
                file_actions[target] = action
                with ctx.stats_lock:
                    if action in ("created", "would_create"):
                        ctx.stats.files_created += 1
                    elif action in ("updated", "would_update"):
                        ctx.stats.files_updated += 1
                    elif action == "unchanged":
                        ctx.stats.files_unchanged += 1
                    elif action.startswith("failed"):
                        ctx.stats.files_failed += 1
                        had_failure = True
            entry["files"] = file_actions

        # --- reconcile veracode.yml block -------------------------------------
        if not ctx.skip_yml:
            sha, yml = get_file(ctx.api_base, org, repo, "veracode.yml", ctx.token, ctx.branch)
            if yml is None:
                yml_action = "veracode_yml_missing"
                with ctx.stats_lock:
                    ctx.stats.yml_missing += 1
                had_failure = True
            else:
                new_yml, recon = reconcile_baseline_block(
                    yml, ctx.baseline_block, insert_only=ctx.yml_insert_only,
                )
                if recon in ("injected", "updated"):
                    if ctx.apply:
                        verb = "add" if recon == "injected" else "update"
                        res = put_file(
                            ctx.api_base, org, repo, "veracode.yml", ctx.token, ctx.branch, new_yml,
                            message=f"baseline: {verb} baseline block in veracode.yml [skip ci]", log=log,
                        )
                        if res in ("created", "updated"):
                            yml_action = recon
                        else:
                            yml_action = res
                            had_failure = True
                    else:
                        yml_action = "would_inject" if recon == "injected" else "would_update"
                    with ctx.stats_lock:
                        if yml_action in ("injected", "would_inject"):
                            ctx.stats.yml_injected += 1
                        elif yml_action in ("updated", "would_update"):
                            ctx.stats.yml_updated += 1
                        else:
                            ctx.stats.yml_failed += 1
                elif recon in ("already_current", "already_present"):
                    yml_action = recon
                    with ctx.stats_lock:
                        ctx.stats.yml_already += 1
                else:  # no_static_block
                    yml_action = "no_static_block"
                    with ctx.stats_lock:
                        ctx.stats.yml_no_static += 1
                    had_failure = True
            entry["veracode_yml"] = yml_action

        entry["status"] = "ok"

    except Exception as exc:
        entry["status"] = "error"
        entry["error"] = str(exc)
        had_failure = True
        log(f"  ERROR: {str(exc)[:120]}")

    # summary line
    if "files" in entry:
        c = sum(1 for a in entry["files"].values() if a in ("created", "would_create"))
        u = sum(1 for a in entry["files"].values() if a in ("updated", "would_update"))
        x = sum(1 for a in entry["files"].values() if a == "unchanged")
        f = sum(1 for a in entry["files"].values() if a.startswith("failed"))
        log(f"  Files: {c} new, {u} updated, {x} unchanged, {f} failed")
    if "veracode_yml" in entry:
        log(f"  veracode.yml: [{entry['veracode_yml']}]")

    _finish(org, idx, ctx, entry, lines, had_failure)


def _finish(org: str, idx: int, ctx: Ctx, entry: dict[str, Any], lines: list[str], had_failure: bool) -> None:
    if had_failure:
        with ctx.stats_lock:
            ctx.stats.orgs_with_failures += 1
        with ctx.rows_lock:
            ctx.issue_rows.append([org, entry.get("status", "?"), json.dumps(entry.get("files", {}))[:200]])

    with ctx.report_lock:
        with ctx.report_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry) + "\n")

    with ctx.stats_lock:
        ctx.stats.processed += 1

    with ctx.checkpoint_lock:
        ctx.completed_orgs.append(org)
        try:
            ctx.checkpoint_file.write_text(
                json.dumps({"last_org": org, "processed": len(ctx.completed_orgs), "completed": ctx.completed_orgs}, indent=2),
                encoding="utf-8", newline="\n",
            )
        except Exception:
            pass

    tprint("\n".join(lines))


# ---------------------------------------------------------------------------
# Report finalize + CSV
# ---------------------------------------------------------------------------

def finalize_report(path: Path) -> None:
    if not path.exists():
        return
    entries: list[Any] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(header)
        w.writerows(rows)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk rollout of the Veracode baseline feature across orgs")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only, no changes (default).")
    mode.add_argument("--apply", action="store_true", help="Deploy files and inject the veracode.yml block.")

    ap.add_argument("--enterprise", help="GitHub Enterprise slug for org discovery.")
    ap.add_argument("--orgs-file", help="File with one org login per line ('#' comments allowed).")
    ap.add_argument("--assets-dir", default=str(Path(__file__).resolve().parent.parent),
                    help="Directory holding helper/baseline/* and .github/workflows/* (default: repo root, the script's parent dir).")
    ap.add_argument("--repo-name", default=DEFAULT_REPO_NAME, help="Integration repo name in each org (default: veracode).")
    ap.add_argument("--branch", default="main", help="Branch to write to in each veracode repo (default: main).")
    ap.add_argument("--skip-files", action="store_true", help="Do not deploy helper/workflow files (yml only).")
    ap.add_argument("--skip-yml", action="store_true", help="Do not touch veracode.yml (files only).")
    ap.add_argument("--yml-insert-only", action="store_true",
                    help="Only add the baseline block when missing; never modify an existing one "
                         "(preserves per-repo hand-tuned baseline settings).")
    ap.add_argument("--force", action="store_true", help="Re-PUT files even when content is identical.")

    ap.add_argument("--api-base", default=env("GITHUB_API_BASE", "https://api.github.com"))
    ap.add_argument("--web-base", default=env("GITHUB_WEB_BASE", "https://github.com"))
    ap.add_argument("--token-env", default="GITHUB_TOKEN", help="Env var holding the GitHub PAT.")
    ap.add_argument("--out", default="out", help="Output directory (default: ./out).")
    ap.add_argument("--skip-to", help="Skip all orgs before this one.")
    ap.add_argument("--continue", dest="resume", action="store_true", help="Resume from checkpoint.json.")
    ap.add_argument("--workers", type=int, default=1, help="Parallel worker threads (recommended 3-5).")

    args = ap.parse_args()
    if not args.dry_run and not args.apply:
        args.dry_run = True
    if args.workers < 1:
        print("ERROR: --workers must be >= 1", file=sys.stderr)
        sys.exit(1)
    if args.skip_files and args.skip_yml:
        print("ERROR: --skip-files and --skip-yml together leave nothing to do", file=sys.stderr)
        sys.exit(1)

    token = env(args.token_env)
    if not token:
        print(f"ERROR: set {args.token_env}", file=sys.stderr)
        sys.exit(1)

    api_base = args.api_base.rstrip("/")
    web_base = args.web_base.rstrip("/")

    try:
        assets, baseline_block = load_assets(Path(args.assets_dir))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"MODE: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"  Target repo per org : {args.repo_name} (branch: {args.branch})")
    print(f"  Deploy files        : {'NO (--skip-files)' if args.skip_files else f'YES ({len(assets)} files)'}")
    print(f"  Inject veracode.yml : {'NO (--skip-yml)' if args.skip_yml else ('YES (insert-only)' if args.yml_insert_only else 'YES (insert + update)')}")
    print(f"  Assets dir          : {args.assets_dir}")
    print(f"  Workers             : {args.workers}")
    print(f"{'=' * 60}\n")

    # GitHub token sanity check
    r = request("GET", f"{api_base}/user", token)
    if r.status_code != 200:
        print(f"ERROR: GitHub token check failed ({r.status_code})", file=sys.stderr)
        sys.exit(1)
    print(f"[OK] GitHub token valid (user: {r.json().get('login', '?')})")

    orgs = list_orgs(api_base, token, args.enterprise, args.orgs_file)
    if args.orgs_file and args.enterprise:
        with open(args.orgs_file, encoding="utf-8") as f:
            keep = {ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")}
        orgs = [o for o in orgs if o in keep]
        print(f"[OK] Filtered to {len(orgs)} orgs from {args.orgs_file}")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "orgs.txt").write_text("".join(o + "\n" for o in orgs), encoding="utf-8")

    checkpoint_file = outdir / "checkpoint.json"
    if args.resume and checkpoint_file.exists():
        try:
            done = set(json.loads(checkpoint_file.read_text(encoding="utf-8")).get("completed", []))
            before = len(orgs)
            orgs = [o for o in orgs if o not in done]
            print(f"[RESUME] Skipping {before - len(orgs)} completed orgs\n")
        except Exception as exc:
            print(f"[WARNING] checkpoint load failed: {exc}")

    if args.skip_to and args.skip_to in orgs:
        i = orgs.index(args.skip_to)
        orgs = orgs[i:]
        print(f"[SKIP] Starting from {args.skip_to} (skipped {i})\n")

    total = len(orgs)

    if args.apply and not args.resume:
        print(f"About to modify {total} orgs in APPLY mode.")
        print("Type 'yes' to continue: ", end="")
        if input().strip().lower() != "yes":
            print("[CANCELLED]")
            sys.exit(0)

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = outdir / f"baseline_rollout_{run_ts}.json"

    ctx = Ctx(
        api_base=api_base, web_base=web_base, token=token, apply=args.apply,
        branch=args.branch, repo_name=args.repo_name,
        skip_files=args.skip_files, skip_yml=args.skip_yml, yml_insert_only=args.yml_insert_only,
        force=args.force,
        assets=assets, baseline_block=baseline_block,
        total_orgs=total, report_path=report_path, checkpoint_file=checkpoint_file,
        stats=Stats(total_orgs=total),
    )

    if args.workers > 1:
        print(f"[PARALLEL] {args.workers} workers\n")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(process_org, org, i, ctx): org for i, org in enumerate(orgs, 1)}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as exc:
                    tprint(f"[ERROR] {futures[fut]}: {exc}")
    else:
        for i, org in enumerate(orgs, 1):
            process_org(org, i, ctx)

    finalize_report(report_path)
    write_csv(outdir / "missing_veracode_repo.csv", ["organization", "repo", "note"], ctx.missing_repo_rows)
    write_csv(outdir / "baseline_rollout_issues.csv", ["organization", "status", "detail"], ctx.issue_rows)

    st = ctx.stats
    st.end_time = datetime.now()
    dur = str(st.end_time - st.start_time).split(".")[0]
    snap = _rate_limiter.snapshot()

    print(f"\n{'=' * 70}")
    print("EXECUTION SUMMARY")
    print(f"{'=' * 70}")
    print(f"Mode          : {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Duration      : {dur}")
    print(f"Organizations : {st.processed}/{st.total_orgs} processed")
    print(f"Repos         : {st.repos_ok} ready, {st.repos_missing} missing, {st.repos_empty} empty")
    if not args.skip_files:
        print(f"Files         : {st.files_created} new, {st.files_updated} updated, "
              f"{st.files_unchanged} unchanged, {st.files_failed} failed")
    if not args.skip_yml:
        print(f"veracode.yml  : {st.yml_injected} injected, {st.yml_updated} updated, "
              f"{st.yml_already} already current, {st.yml_no_static} no static block, "
              f"{st.yml_missing} missing, {st.yml_failed} failed")
    print(f"Orgs w/ issues: {st.orgs_with_failures} (see baseline_rollout_issues.csv)")
    print(f"Rate limits   : {snap['requests_last_hour']}/{snap['hourly_cap']} req/hour, "
          f"{snap['writes_last_hour']}/{snap['content_hour_cap']} writes/hour, "
          f"{snap['writes_last_minute']}/{snap['content_min_cap']} writes/min")
    print(f"{'=' * 70}")
    print("\nOutputs:", outdir.resolve())
    sys.exit(0)


if __name__ == "__main__":
    main()