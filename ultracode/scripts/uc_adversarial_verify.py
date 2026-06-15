#!/usr/bin/env python3
"""Adversarial verification gate for Ultracode Codex workflows.

This script is intentionally deterministic and dependency-free. It does not replace
LLM review. It creates an evidence bundle and a falsification checklist that pushes
Codex/subagents to check the details that commonly fail in large local coding tasks:
unsupported completion claims, untested behavior changes, broken CLI flags, stale docs,
unsafe broad edits, path/name mistakes, and edge-case regressions.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Single-segment directory names only: this set is matched against individual path
# parts, so a joined string like ".codex/ultracode" could never match (its parts are
# ".codex" and "ultracode" separately) — use ".codex". "storage" and "env" added so
# large data/venv dirs are pruned and the scan does not crawl them on a slow mount.
EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", ".codex", ".ultracode", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    "target", ".tox", ".idea", ".vscode", ".next", ".turbo", "coverage", ".cache",
    "storage",
}
WALK_BUDGET_SECONDS = 8.0


def walk_files(root: Path, cap: int = 4000, budget: float = WALK_BUDGET_SECONDS) -> tuple[list[Path], bool]:
    """Pruned, time-budgeted file walk; returns (files, truncated).

    Prunes EXCLUDE_DIRS and hidden dirs IN-PLACE via os.walk (pathlib.rglob can only
    filter after descending, which wedged this script on a 9p/drvfs WSL mount), and
    stops on a file-count cap or wall-clock budget so a huge/slow tree cannot stall it.
    """
    files: list[Path] = []
    start = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fn in filenames:
            files.append(Path(dirpath) / fn)
            if len(files) >= cap:
                return files, True
        if time.monotonic() - start > budget:
            return files, True
    return files, False

SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".kts", ".c", ".h", ".cpp", ".cc", ".hpp", ".f90", ".f95", ".f03",
    ".sh", ".bash", ".zsh", ".ps1",
}

DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
CONFIG_NAMES = {
    "pyproject.toml", "package.json", "tsconfig.json", "Cargo.toml", "go.mod", "Makefile",
    "CMakeLists.txt", "tox.ini", "pytest.ini", ".pre-commit-config.yaml", "AGENTS.md",
}

# Path segments and markers that indicate vendored/generated/minified files. These are
# not hand-written source, so a regex match in them is almost always a false positive
# (a minified bundle, a copied dependency). Scanned files matching this are skipped.
GENERATED_DIR_PARTS = {"assets", "vendor", "generated", "min", "minified", "third_party", "thirdparty", "node_modules"}

# Destructive-delete detection. A regex on `rm -rf <anything-with-a-slash>` false-fires on
# routine cleanup (`rm -rf dist/`), so target danger is judged the way the safety hook does
# (uc_hook_router._has_dangerous_target): only a real root/home/glob/single-segment-absolute
# target counts. This also catches GNU long flags and disk-wipe forms a literal regex misses.
# Note: no trailing \b on the short-flag alt — for combined flags like `-rf` the boundary
# between `r` and `f` is not a word boundary and would wrongly fail the match. `-R` (capital,
# valid recursive flag) is covered by [rR].
_RM_RECURSIVE_RE = re.compile(r"\s-[a-zA-Z]*[rR]|--recursive\b")
_RM_FORCE_RE = re.compile(r"\s-[a-zA-Z]*[fF]|--force\b")
_DANGEROUS_TARGET_RE = re.compile(
    r"(?:^|[\s'\"=(])"
    r"(?:/|/\*|~/?|\$\{?HOME\}?/?|\*|/[A-Za-z0-9._-]+/?)"
    r"(?:[\s'\")\\;&|]|$)"
)
_RMTREE_LITERAL_RE = re.compile(r"shutil\.rmtree\(\s*['\"](?:/|~)")
_DISK_DESTROY_RE = re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b|\bdd\b[^\n]*\bof=/dev/|>\s*/dev/(?:sd|nvme|hd|vd|disk|mmcblk)")


def _dangerous_delete(line: str) -> bool:
    """True if a line performs a genuinely destructive deletion or disk wipe."""
    if _RMTREE_LITERAL_RE.search(line) or _DISK_DESTROY_RE.search(line):
        return True
    if re.search(r"\brm\b", line) and _RM_RECURSIVE_RE.search(line) and _RM_FORCE_RE.search(line):
        return bool(_DANGEROUS_TARGET_RE.search(line))
    return False

# Risk patterns are heuristic. The 5th field is the target file class: "code" patterns
# run only on source/config files (they describe executable-code risk and false-fire in
# prose/docs), while "any" patterns also run on docs. These intentionally do NOT try to
# be a linter for the whole repo — they are a CHANGE review. The scanner restricts them
# to lines a diff actually ADDED whenever a diff exists (see scan_risk_patterns), so a
# pre-existing risky line that this run did not touch is never reported. The patterns
# are also deliberately conservative (e.g. dangerous-delete requires a literal root/home/
# glob target; network-install matches only pipe-to-shell, not documented `npm install`)
# so they do not fire on test fixtures, install docs, or routine single-file cleanup.
# Each entry: (severity, kind, matcher, why, targets). `matcher` is a regex string scanned
# over contiguous added-line blocks (so multi-line forms like a two-line `except:`/`pass`
# match), OR a callable(line)->bool applied per added line. `targets`: "code" runs only on
# source/config; "any" also runs on docs.
RISK_PATTERNS: list[tuple[str, str, Any, str, str]] = [
    ("critical", "dangerous-delete", _dangerous_delete, "recursive force-delete of a root/home/glob target, or a disk wipe", "code"),
    ("high", "silent-exception", r"except\s+(Exception\s*)?:\s*\n?\s*(pass|return\s+None|continue)|bare\s+except", "silent error suppression hides broken details", "code"),
    ("high", "test-disabled", r"pytest\.mark\.skip|pytest\.mark\.xfail|@?unittest\.skip(If|Unless)?\b|\.skipTest\(|describe\.skip\b|it\.skip\b|test\.skip\b|describe\.only\b|it\.only\b|test\.only\b|\bxit\(|\bxdescribe\(|\bt\.Skip\(", "test appears disabled, focused, or weakened", "code"),
    ("medium", "placeholder", r"\bTODO\b|\bFIXME\b|\bHACK\b|\bXXX\b|raise\s+NotImplemented(Error)?\b|\bNotImplementedError\b", "placeholder/unfinished marker", "any"),
    ("medium", "weak-test", r"assert\s+True\b|expect\(\s*true\s*\)\.toBe\(\s*true\s*\)|assert\s+1\s*===?\s*1\b", "weak/tautological assertion", "code"),
    ("low", "debug-residue", r"console\.log\(|console\.debug\(|print\(\s*['\"]?DEBUG|pdb\.set_trace\(|\bdebugger\s*;", "possible debug residue left in changed code", "code"),
    ("medium", "network-install", r"\b(curl|wget|fetch)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|dash|ksh|python[0-9.]*)\b", "pipe-to-shell network install needs explicit review", "any"),
    ("low", "float-exact", r"\b(float|double)\b[^\n=]{0,40}==|==\s*NaN\b|math\.isnan\([^)]*\)\s*==", "numeric exactness / NaN comparison risk", "code"),
]

CLAIM_WORDS = [
    "done", "completed", "fixed", "verified", "passed", "works", "all", "none", "no issues",
    "已完成", "完成", "修复", "验证", "通过", "全部", "没有问题", "无问题",
]


@dataclass
class Finding:
    severity: str
    kind: str
    claim: str
    evidence: list[str]
    recommendation: str
    # advisory findings cannot HARD-FAIL the gate in non-strict mode: they come from a
    # whole-repo fallback scan (no git diff to attribute them to this run), so they are
    # surfaced as observations, not as blockers tied to a change.
    advisory: bool = False


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_read(path: Path, max_bytes: int = 200_000) -> str:
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except Exception:
        return ""


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def should_skip(path: Path) -> bool:
    return any(part in EXCLUDE_DIRS for part in path.parts)


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))[-20_000:]
    except Exception as exc:  # noqa: BLE001
        return 127, f"{type(exc).__name__}: {exc}"


def git_available(root: Path) -> bool:
    code, _ = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], root)
    return code == 0


# The well-known empty-tree object: diffing against it makes every tracked/staged file
# read as fully ADDED. Used when there is no HEAD yet (a fresh `git init` with no commit),
# where `git diff HEAD` errors and would otherwise be misread as "nothing changed".
GIT_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _diff_names(out: str) -> list[str]:
    """Split git --name-only output into real paths, dropping any error/usage lines that
    git writes to stderr (captured together) on a bad revision."""
    bad = ("fatal:", "error:", "warning:", "usage:", "hint:")
    return [n.strip() for n in out.splitlines() if n.strip() and not n.strip().lower().startswith(bad)]


def collect_git_snapshot(root: Path, base: str) -> dict[str, Any]:
    if not git_available(root):
        return {"available": False, "base": base, "status_short": "", "diff_stat": "", "diff_name_only": [], "untracked": [], "diff_unified0": ""}
    # No-commit repo robustness: if base is HEAD but HEAD does not resolve (no commit yet),
    # diff against the empty tree so staged/committed files are seen as added instead of the
    # diff erroring and the run being misread as empty-clean.
    has_head = run_cmd(["git", "rev-parse", "--verify", "-q", "HEAD"], root)[0] == 0
    eff_base = base if (base != "HEAD" or has_head) else GIT_EMPTY_TREE
    # core.quotepath=false: otherwise git C-style-quotes/octal-escapes non-ASCII paths
    # (CJK/accented/emoji filenames) in BOTH --name-only and --unified output, so the
    # path never resolves on disk and never matches a parsed diff key -> the file (and any
    # risky added line in it) silently escapes the scan.
    qp = ["git", "-c", "core.quotepath=false"]
    _, status = run_cmd(["git", "status", "--short"], root)
    _, stat = run_cmd(qp + ["diff", "--stat", eff_base, "--"], root)
    _, names = run_cmd(qp + ["diff", "--name-only", eff_base, "--"], root)
    _, staged_names = run_cmd(qp + ["diff", "--cached", "--name-only", "--"], root)
    # Untracked, non-ignored files: a brand-new file is invisible to `git diff` but is a
    # real change (a very common agent action). Without this, a new file reads as
    # "nothing changed" (empty-clean) and is never reviewed.
    _, untracked_raw = run_cmd(qp + ["ls-files", "--others", "--exclude-standard"], root)
    untracked = sorted({n.strip() for n in untracked_raw.splitlines() if n.strip()})
    # Unified diff for added-line scoping: union worktree-vs-base and staged, so a change
    # that lives only in the index (staged then worktree-reverted) is still attributed.
    _, unified = run_cmd(qp + ["diff", "--unified=0", eff_base, "--"], root, timeout=30)
    _, unified_cached = run_cmd(qp + ["diff", "--cached", "--unified=0", "--"], root, timeout=30)
    name_list = sorted(set(_diff_names(names) + _diff_names(staged_names)))
    return {
        "available": True,
        "base": eff_base,
        "head_present": has_head,
        "status_short": status[:20_000],
        "diff_stat": stat[:20_000],
        "diff_name_only": name_list,
        "untracked": untracked,
        "diff_unified0": (unified + "\n" + unified_cached)[-120_000:],
    }


def _dedupe(paths: list[Path], cap: int = 500) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out[:cap]


def collect_changed_files(root: Path, git_snapshot: dict[str, Any], explicit_paths: list[str]) -> tuple[list[Path], str]:
    """Resolve the files to change-review and report HOW they were derived (provenance).

    Provenance is what keeps this gate honest:
      - "explicit"        : caller passed --path; review exactly those.
      - "diff"            : git available with a non-empty diff; review the changed files.
      - "empty-clean"     : git available and the diff is EMPTY -> nothing changed, so
                            there is nothing to change-review. Returns []. This is the fix
                            for read-only audits: "git available + empty diff" is the
                            unambiguous signal that the run changed nothing, and must NOT
                            be conflated with "no diff information available".
      - "fallback-no-git" : no git AND no --path -> we cannot know what changed, so inspect
                            a bounded set of files, but the caller marks these findings
                            advisory (they cannot be attributed to this run).
    """
    if explicit_paths:
        paths: list[Path] = []
        for p in explicit_paths:
            candidate = (root / p).resolve()
            if candidate.exists():
                if candidate.is_file():
                    paths.append(candidate)
                elif candidate.is_dir():
                    sub, _ = walk_files(candidate, cap=2000)
                    paths.extend(x for x in sub if not should_skip(x))
        return _dedupe(paths), "explicit"

    if git_snapshot.get("available"):
        # Tracked diff names UNION untracked (new) files: a new file is a real change.
        names = list(git_snapshot.get("diff_name_only", [])) + list(git_snapshot.get("untracked", []))
        if not names:
            # Clean working tree against base AND no new files: zero change-review scope.
            return [], "empty-clean"
        paths = []
        for name in names:
            p = (root / name).resolve()
            if p.exists() and p.is_file() and not should_skip(p):
                paths.append(p)
        return _dedupe(paths), "diff"

    # No VCS signal at all: bounded advisory scan of likely source/config/docs files.
    all_files, _ = walk_files(root, cap=4000)
    fallback: list[Path] = []
    for p in all_files:
        if not should_skip(p) and (p.suffix in SOURCE_SUFFIXES or p.suffix in DOC_SUFFIXES or p.name in CONFIG_NAMES):
            fallback.append(p)
            if len(fallback) >= 200:
                break
    return _dedupe(fallback), "fallback-no-git"


def looks_generated(path: Path, text: str) -> bool:
    """True for vendored/generated/minified files where pattern matches are noise."""
    name = path.name.lower()
    if ".min." in name or name.endswith((".min.js", ".min.css", ".bundle.js", ".lock")):
        return True
    if any(part.lower() in GENERATED_DIR_PARTS for part in path.parts):
        return True
    head = text[:2000]
    if "@generated" in head or "do not edit" in head.lower():
        return True
    # Minified payloads pack everything onto a few very long lines. Use the average line
    # length, not a single long line — a hand-written file with one long URL/data literal
    # must NOT be skipped wholesale (that would hide risky lines elsewhere in it).
    lines = text.split("\n", 200)[:200]
    if len(lines) >= 3:
        avg = sum(len(l) for l in lines) / len(lines)
        if avg > 600:
            return True
    return False


def _contiguous_blocks(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Group (lineno, text) pairs into (start_lineno, joined_text) runs of CONSECUTIVE
    line numbers, so a multi-line regex matches within a real contiguous block but cannot
    bridge two unrelated hunks (which would invent a false adjacency)."""
    blocks: list[tuple[int, str]] = []
    run: list[tuple[int, str]] = []
    prev: int | None = None
    for ln, text in lines:
        if prev is not None and ln != prev + 1:
            blocks.append((run[0][0], "\n".join(t for _, t in run)))
            run = []
        run.append((ln, text))
        prev = ln
    if run:
        blocks.append((run[0][0], "\n".join(t for _, t in run)))
    return blocks


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_added_lines(diff_unified0: str) -> dict[str, list[tuple[int, str]]]:
    """Parse `git diff --unified=0` into {relpath: [(new_line_number, added_text), ...]}.

    Only ADDED lines are returned, with their line number in the new file, so scans can
    target exactly what the change introduced instead of re-reading whole files.
    """
    out: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    new_lineno = 0
    for raw in diff_unified0.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                current = target[2:] if target.startswith(("a/", "b/")) else target
                out.setdefault(current, [])
            continue
        if raw.startswith("--- ") or raw.startswith("diff ") or raw.startswith("index "):
            continue
        m = _HUNK_RE.match(raw)
        if m:
            new_lineno = int(m.group(1))
            continue
        if current is None:
            continue
        if raw.startswith("+"):
            out[current].append((new_lineno, raw[1:]))
            new_lineno += 1
        elif raw.startswith("-"):
            # Removed line: does not consume a new-file line number.
            continue
    return out


def line_number(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _pattern_applies(targets: str, path: Path) -> bool:
    """A "code" pattern runs only on source/config; "any" also runs on docs."""
    is_doc = path.suffix in DOC_SUFFIXES
    if targets == "any":
        return True
    return not is_doc  # "code"


def scan_risk_patterns(
    root: Path,
    files: Iterable[Path],
    added_map: dict[str, list[tuple[int, str]]] | None = None,
    advisory: bool = False,
) -> list[Finding]:
    """Scan changed files for risk patterns.

    When `added_map` is provided (a real git diff exists) each pattern is matched ONLY
    against lines the diff ADDED — so a pre-existing risky line in a touched file, or any
    untouched file, is never flagged. Without it (explicit --path or no-git fallback) the
    whole file is scanned and `advisory` should be set for the no-git case.
    """
    findings: list[Finding] = []
    for path in files:
        if path.suffix not in SOURCE_SUFFIXES and path.suffix not in DOC_SUFFIXES and path.name not in CONFIG_NAMES:
            continue
        text = safe_read(path)
        if not text or looks_generated(path, text):
            continue
        relp = rel(path, root)
        # Which (line_number, line_text) pairs to scan:
        #  - diff mode, file in the parsed diff -> only the lines it ADDED.
        #  - diff mode, file NOT in the diff (a brand-new untracked file) -> whole file is new.
        #  - whole-file mode (explicit --path / no-git fallback) -> the whole file.
        if added_map is not None and relp in added_map:
            scan_lines = added_map[relp]
            if not scan_lines:
                continue  # in the diff but only removed lines -> nothing added to review
        else:
            scan_lines = list(enumerate(text.splitlines(), start=1))
        blocks = _contiguous_blocks(scan_lines)
        grouped: dict[tuple[str, str, str], list[str]] = {}

        def _record(severity: str, kind: str, why: str, ln: int, snippet: str) -> None:
            bucket = grouped.setdefault((severity, kind, why), [])
            if len(bucket) < 8:
                bucket.append(f"{relp}:L{ln}: {snippet.strip()[:180]}")

        for severity, kind, matcher, why, targets in RISK_PATTERNS:
            if not _pattern_applies(targets, path):
                continue
            if callable(matcher):
                for ln, line in scan_lines:
                    if matcher(line):
                        _record(severity, kind, why, ln, line)
            else:
                for start, btext in blocks:
                    for m in re.finditer(matcher, btext, flags=re.IGNORECASE | re.MULTILINE):
                        ln = start + btext.count("\n", 0, m.start())
                        _record(severity, kind, why, ln, m.group(0).replace("\n", " "))
        for (severity, kind, why), evidence in grouped.items():
            findings.append(Finding(
                severity=severity,
                kind=kind,
                claim=f"Potential {kind}: {why}.",
                evidence=evidence,
                recommendation="Review this manually; either justify it in the ledger, remove it, or add targeted verification.",
                advisory=advisory,
            ))
    return findings


def find_tests(root: Path) -> list[Path]:
    tests: list[Path] = []
    all_files, _ = walk_files(root, cap=4000)
    for p in all_files:
        if should_skip(p):
            continue
        name = p.name.lower()
        parts = {part.lower() for part in p.parts}
        if (
            name.startswith("test_") or name.endswith("_test.py") or name.endswith(".test.ts") or
            name.endswith(".spec.ts") or name.endswith(".test.js") or name.endswith(".spec.js") or
            name.endswith("_test.go") or "tests" in parts or "test" in parts
        ):
            tests.append(p)
    return tests[:500]


def likely_test_for(source: Path, root: Path, tests: list[Path]) -> list[str]:
    stem = source.stem.lower()
    rel_source = rel(source, root).lower().replace("/", " ").replace("_", "-")
    hits = []
    for t in tests:
        r = rel(t, root).lower()
        if stem in r or stem.replace("-", "_") in r:
            hits.append(rel(t, root))
            continue
        text = safe_read(t, max_bytes=80_000).lower()
        if source.stem.lower() in text or rel_source in text:
            hits.append(rel(t, root))
    return sorted(set(hits))[:10]


def scan_test_gaps(root: Path, changed_files: list[Path], tests: list[Path], advisory: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    changed_sources = [p for p in changed_files if p.suffix in SOURCE_SUFFIXES and not re.search(r"(^|/)(test|tests)(/|$)|test_|_test|\.spec\.|\.test\.", rel(p, root), flags=re.I)]
    if changed_sources and not tests:
        findings.append(Finding(
            severity="high",
            kind="no-tests-found",
            claim="Source files changed or inspected, but no recognizable test files were found.",
            evidence=[rel(p, root) for p in changed_sources[:20]],
            recommendation="Add targeted tests or explicitly document why deterministic tests are unavailable.",
            advisory=advisory,
        ))
        return findings
    uncovered = []
    for src in changed_sources[:80]:
        hits = likely_test_for(src, root, tests)
        if not hits:
            uncovered.append(rel(src, root))
    if uncovered:
        findings.append(Finding(
            severity="medium",
            kind="no-nearby-test-evidence",
            claim="Changed source files have no obvious nearby or referencing tests.",
            evidence=uncovered[:30],
            recommendation="Ask an adversarial tester to design counterexamples for these files, or add tests near the changed behavior.",
            advisory=advisory,
        ))
    return findings


def load_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _run_read_only(run_dir: Path | None) -> bool:
    """True when the run's own metadata marks it read-only (no code change expected).

    Defense in depth: even if the caller forgets --read-only, run.json (written by
    uc_bootstrap from the route's read_only flag) makes a no-change audit's findings advisory.
    """
    if not run_dir:
        return False
    meta = load_json_file(run_dir / "run.json")
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("read_only")) or str(meta.get("mode", "")) in {"audit", "plan-only", "research"}


def read_run_artifacts(run_dir: Path | None) -> dict[str, Any]:
    if not run_dir:
        return {"available": False}
    out: dict[str, Any] = {"available": run_dir.exists(), "run_dir": str(run_dir)}
    if not run_dir.exists():
        return out
    for name in ["run.json", "verification.json"]:
        p = run_dir / name
        out[name] = load_json_file(p) if p.exists() else None
    for name in ["ledger.md", "synthesis.md", "verification.md"]:
        p = run_dir / name
        out[name] = safe_read(p, max_bytes=120_000) if p.exists() else ""
    results_dir = run_dir / "results"
    result_files = []
    if results_dir.exists():
        result_files = [str(p.relative_to(run_dir)) for p in sorted(results_dir.glob("*")) if p.is_file()]
    out["result_files"] = result_files
    out["adversarial_result_files"] = [p for p in result_files if re.search(r"advers|claim|edge|review|verify", p, flags=re.I)]
    return out


def scan_claim_file(root: Path, claim_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for p in claim_files:
        if not p.exists() or not p.is_file():
            continue
        text = safe_read(p, max_bytes=120_000)
        lines = []
        for i, line in enumerate(text.splitlines(), start=1):
            lower = line.lower()
            if any(w in lower for w in CLAIM_WORDS) and len(line.strip()) > 8:
                lines.append(f"{rel(p, root)}:L{i}: {line.strip()[:220]}")
        if lines:
            findings.append(Finding(
                severity="medium",
                kind="claims-need-evidence",
                claim="Completion or verification claims were found; verify each against diff, commands, and run artifacts.",
                evidence=lines[:30],
                recommendation="Have a claim-checker subagent mark each claim as supported, unsupported, or needs-confirmation.",
            ))
    return findings


def scan_run_artifact_gaps(run_artifacts: dict[str, Any], strict: bool, has_changes: bool = True) -> list[Finding]:
    findings: list[Finding] = []
    if not run_artifacts.get("available"):
        findings.append(Finding(
            severity="medium" if strict else "low",
            kind="no-run-dir",
            claim="No Ultracode run directory was supplied or found; final claims cannot be tied to a durable ledger.",
            evidence=[],
            recommendation="Run uc_bootstrap.py and keep verification/adversarial artifacts under .ultracode/runs/<run-id>/.",
        ))
        return findings
    verification = run_artifacts.get("verification.json")
    if not verification:
        findings.append(Finding(
            severity="high" if strict else "medium",
            kind="missing-verification-json",
            claim="The run directory has no verification.json artifact.",
            evidence=[str(run_artifacts.get("run_dir", ""))],
            recommendation="Run uc_verify.py --execute where safe, or explicitly record skipped checks and reasons.",
        ))
    else:
        results = verification.get("results", []) if isinstance(verification, dict) else []
        failed = [r for r in results if isinstance(r, dict) and r.get("status") not in {"pass"}]
        if failed:
            findings.append(Finding(
                severity="critical",
                kind="verification-failed",
                claim="One or more verification commands failed, timed out, or errored.",
                evidence=[f"{r.get('command')}: {r.get('status')} exit={r.get('exit_code')}" for r in failed[:20]],
                recommendation="Do not mark the task complete until failures are fixed or explicitly scoped out.",
            ))
        scan = verification.get("scan", {}) if isinstance(verification, dict) else {}
        read_only = isinstance(scan, dict) and scan.get("read_only") is True
        detected_cmds = verification.get("commands", []) if isinstance(verification, dict) else []
        if read_only or not has_changes:
            # No code changed (read-only audit, or empty/clean diff): the project's own
            # test/build commands existing-but-unrun is expected, not a gap to block on.
            pass
        elif verification.get("executed") is not True and detected_cmds:
            findings.append(Finding(
                severity="medium",
                kind="verification-detected-not-executed",
                claim="Verification commands were detected but not executed.",
                evidence=[c.get("command", "") for c in detected_cmds[:20]],
                recommendation="Execute safe checks or list exact blockers.",
            ))
        elif not failed:
            # Verification passed — but expose HOW thin it was, so a green gate is
            # not mistaken for behavioral proof when only a parse check ran.
            cmds = verification.get("commands", []) if isinstance(verification, dict) else []
            kinds = {str(c.get("kind", "")) for c in cmds if isinstance(c, dict)}
            if cmds and not (kinds - {"syntax"}):
                findings.append(Finding(
                    severity="low",
                    kind="verification-shallow",
                    claim="Verification passed but only ran a syntax/parse check (e.g. py_compile); this proves files parse, not that behavior is correct.",
                    evidence=[c.get("command", "") for c in cmds[:10]],
                    recommendation="Add and run real tests/build/lint before presenting this as full verification.",
                ))
    if strict and not run_artifacts.get("adversarial_result_files"):
        findings.append(Finding(
            severity="high",
            kind="missing-adversarial-subagent-result",
            claim="Strict mode requires at least one adversarial/claim/edge/review result file under results/.",
            evidence=[str(run_artifacts.get("run_dir", ""))],
            recommendation="Spawn ultracode_adversary and ultracode_claim_checker workers, then rerun uc_merge_results.py.",
        ))
    return findings


def _result_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def scan_adversarial_result_content(run_dir: Path | None, run_artifacts: dict[str, Any], strict: bool) -> list[Finding]:
    """Inspect the CONTENT of adversarial worker result files, not just their existence.

    A filename that merely matches advers|claim|edge|review|verify must not satisfy
    the strict gate. Worker-reported failures and the worker's own critical/high
    findings are surfaced into the gate so they actually block completion.
    """
    findings: list[Finding] = []
    if not run_dir:
        return findings
    for relp in run_artifacts.get("adversarial_result_files") or []:
        p = run_dir / relp
        if not p.exists() or not p.is_file():
            continue
        substantive = False
        if p.suffix.lower() == ".json":
            records = _result_records(load_json_file(p))
            for rec in records:
                status = str(rec.get("status", "")).lower()
                if status in {"fail", "failed", "blocked"}:
                    findings.append(Finding(
                        severity="high",
                        kind="adversarial-worker-failed",
                        claim=f"Adversarial worker result `{relp}` reported status='{status}'.",
                        evidence=[relp, str(rec.get("summary", ""))[:200]],
                        recommendation="Resolve the worker's reported failure or record it as an explicit unresolved risk before claiming completion.",
                    ))
                for wf in rec.get("findings") or []:
                    if not isinstance(wf, dict):
                        continue
                    sev = str(wf.get("severity", "")).lower()
                    if sev in {"critical", "high"}:
                        findings.append(Finding(
                            severity=sev,
                            kind="adversarial-worker-finding",
                            claim=f"{relp}: {str(wf.get('claim', '')).strip()[:200]}",
                            evidence=[str(e) for e in (wf.get("evidence") or [])][:10] or [relp],
                            recommendation="Address this adversarial finding or list it as an explicit unresolved risk.",
                        ))
            substantive = any(
                str(rec.get("status", "")).strip()
                or rec.get("findings")
                or str(rec.get("summary", "")).strip()
                or rec.get("evidence")
                for rec in records
            )
            # Trust-but-verify: a worker that REPORTS a clean judgment (verdict=pass /
            # status=ok) but carries no evidence, no findings, and only a trivial summary
            # is an unvalidated self-report — exactly what lets a worker "pass" the gate
            # without actually checking. Surface it (always, not only strict) so the gate
            # is tied to substance, not to a status string the model typed.
            for rec in records:
                verdict = str(rec.get("verdict", "")).lower()
                status = str(rec.get("status", "")).lower()
                claims_ok = verdict == "pass" or (status == "ok" and verdict in {"", "n/a", "not-applicable"})
                has_substance = bool(rec.get("evidence")) or bool(rec.get("findings")) or len(str(rec.get("summary", "")).strip()) >= 40
                if claims_ok and not has_substance:
                    findings.append(Finding(
                        severity="high" if strict else "medium",
                        kind="adversarial-result-unsupported",
                        claim=f"Worker result `{relp}` reports a clean judgment (status='{status}', verdict='{verdict}') with no evidence, findings, or substantive summary — an unvalidated self-report.",
                        evidence=[relp],
                        recommendation="Re-run the worker requiring real evidence, or treat its 'pass' as unverified and do not rely on it.",
                    ))
        else:
            substantive = len(safe_read(p, max_bytes=20_000).strip()) >= 40
        if strict and not substantive:
            findings.append(Finding(
                severity="high",
                kind="adversarial-result-non-substantive",
                claim=f"Adversarial result `{relp}` exists but has no parseable status, findings, summary, or evidence; a filename alone must not pass the strict gate.",
                evidence=[relp],
                recommendation="Have the adversarial worker emit a real structured result (status + evidence + findings).",
            ))
    return findings


# External quantitative metrics that, if asserted in a final claim, must be backed by a
# captured source in the run (a fetch output, command result, or worker evidence). Catches
# laundered/hallucinated traction numbers (e.g. GitHub stars/forks never actually fetched).
_METRIC_RE = re.compile(
    r"\b(?:stars?|forks?|watchers?|subscribers?|issues?|contributors?|commits?|downloads?|"
    r"last[-\s]?month|last[-\s]?week|weekly|monthly|stargazers?|星标|下载量?|提交数?|贡献者)\b[^\n]{0,40}?\d"
    r"|\d[\d,.]*\s*(?:stars?|forks?|downloads?|commits?|issues?|contributors?|颗?星|次下载|个?提交)",
    re.IGNORECASE,
)
# Inline source tokens that, if on the same line as a metric, count as a cited source.
_SOURCE_TOKEN_RE = re.compile(r"https?://|github\.com|npmjs|api\.npmjs|\bnpm\b|\bgh\b|\bcurl\b|\bwget\b|git\s+(?:log|rev-list|shortlog)", re.IGNORECASE)


def scan_unsourced_metrics(run_dir: Path | None, run_artifacts: dict[str, Any], strict: bool) -> list[Finding]:
    """Flag external quantitative metrics asserted in the final claims that are NOT backed
    by any captured source in the run. A number is considered sourced if it appears in a
    captured artifact (worker results, verification.json, evidence/captured files) OR the
    claim line carries an inline source token (URL / npm / gh / curl / git log)."""
    findings: list[Finding] = []
    if not run_dir:
        return findings
    claim_text = (run_artifacts.get("ledger.md") or "") + "\n" + (run_artifacts.get("synthesis.md") or "")
    if not claim_text.strip():
        return findings
    # Corroboration corpus: everything captured in the run EXCEPT the claim surfaces.
    corpus_parts: list[str] = []
    try:
        for sub in ("results", "evidence", "captured"):
            d = run_dir / sub
            if d.is_dir():
                for fp in sorted(d.glob("**/*")):
                    if fp.is_file():
                        corpus_parts.append(safe_read(fp, max_bytes=120_000))
        for name in ("verification.json", "repo_inventory.json"):
            fp = run_dir / name
            if fp.exists():
                corpus_parts.append(safe_read(fp, max_bytes=120_000))
    except Exception:
        pass
    corpus = "\n".join(corpus_parts)
    unsourced: list[str] = []
    for i, line in enumerate(claim_text.splitlines(), start=1):
        if not _METRIC_RE.search(line):
            continue
        if _SOURCE_TOKEN_RE.search(line):
            continue  # cites a source inline
        numbers = re.findall(r"\d[\d,.]*", line)
        # A metric line is corroborated only if every salient number appears in the corpus.
        salient = [n for n in numbers if len(n.replace(",", "").replace(".", "")) >= 1]
        if salient and all(n in corpus for n in salient):
            continue
        unsourced.append(f"L{i}: {line.strip()[:200]}")
        if len(unsourced) >= 12:
            break
    if unsourced:
        findings.append(Finding(
            severity="high" if strict else "medium",
            kind="unsourced-external-metric",
            claim="External quantitative metrics appear in the final claims with no captured source in the run (no fetch output, command result, or worker evidence backs them).",
            evidence=unsourced,
            recommendation="Persist the fetch/command output that produced each metric into the run dir (results/ or evidence/), cite the source inline, or remove/qualify the number as unverified. Do not launder recalled numbers as fetched data.",
        ))
    return findings


def write_adversarial_work_items(run_dir: Path, task: str, changed_files: list[Path], root: Path) -> None:
    rows = [
        {
            "id": "adversarial-claim-check",
            "role": "ultracode_claim_checker",
            "path": "ledger.md, synthesis.md, verification.md, changed files",
            "objective": "Compare final claims against actual diff, run artifacts, file paths, command output, and package contents. Mark every unsupported claim.",
            "expected_output": "JSON result with supported/unsupported/needs-confirmation claims and exact evidence.",
        },
        {
            "id": "adversarial-edge-probes",
            "role": "ultracode_edge_tester",
            "path": ", ".join(rel(p, root) for p in changed_files[:30]) or "changed files",
            "objective": "Design minimal counterexamples and edge-case probes for changed behavior. Run only safe checks or provide exact commands.",
            "expected_output": "JSON result with counterexample probes, results, and missing tests.",
        },
        {
            "id": "adversarial-contract-review",
            "role": "ultracode_adversary",
            "path": "public APIs, CLI scripts, configs, docs, install commands",
            "objective": "Try to falsify compatibility, public interface, CLI flag, path/name, and documentation claims. Focus on small detail mistakes.",
            "expected_output": "JSON result with severity-ranked issues and evidence.",
        },
        {
            "id": "adversarial-verification-audit",
            "role": "ultracode_verifier",
            "path": "verification.json, package scripts, tests, CI/build config",
            "objective": "Judge whether verification is sufficient for the stated task; identify missing checks and false confidence.",
            "expected_output": "JSON result with commands run/skipped and adequacy verdict.",
        },
    ]
    csv_path = run_dir / "adversarial_work_items.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "role", "path", "objective", "expected_output"])
        writer.writeheader()
        writer.writerows(rows)
    prompt = f"""# Adversarial verification spawn prompt

Task under review:

```text
{task}
```

Use Codex subagents explicitly. Prefer CSV fan-out with `adversarial_work_items.csv` from this run directory.

Each adversarial worker must stay within its assigned scope and return exactly one JSON object with keys:

```json
{{"id":"...","agent":"...","scope":"<your assigned lane>","status":"ok|blocked|error","verdict":"pass|concerns|fail","summary":"...","evidence":[],"findings":[],"changes":[],"verification":[],"recommendations":[],"open_questions":[]}}
```

Keep the two axes separate: `status` is execution only (ok | blocked | error — did you finish your review?); `verdict` is your judgment (pass | concerns | fail). A completed review with a negative judgment is status=ok, verdict=fail — never put the judgment into status.

Adversarial rule: do not rubber-stamp. Try to prove the result wrong. For every issue, include the exact file/path/command/line or state why evidence is unavailable. Focus on small-detail failures: wrong file names, wrong install command, wrong CLI flags, unsupported final claims, stale docs, missed edge cases, and skipped tests.

Suggested instruction template:

```text
Review `{{path}}` for `{{objective}}` as `{{role}}`. Treat the current answer/patch as suspect. Find reasons it could be wrong. Return exactly one JSON object and call report_agent_job_result exactly once.
```
"""
    (run_dir / "adversarial_spawn_prompt.md").write_text(prompt, encoding="utf-8")


def severity_rank(sev: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(sev, 9)


def gate_status(findings: list[Finding], strict: bool, provenance: str = "diff") -> dict[str, Any]:
    """Compute the gate status, separating gating findings from advisory ones.

    A finding is *advisory* when it comes from a whole-repo fallback scan with no git diff
    to attribute it to this run (provenance "fallback-no-git"). Advisory findings are
    reported but, in non-strict mode, can only raise the status to "warn" — never "fail" —
    so a deterministic pattern match on code this run did not change cannot hard-block
    completion. Run-artifact failures (e.g. a verification command that failed) and
    findings tied to an actual diff still gate normally.
    """
    gating = [f for f in findings if not getattr(f, "advisory", False)]
    advisory = [f for f in findings if getattr(f, "advisory", False)]
    counts = Counter(f.severity for f in gating)
    adv_counts = Counter(f.severity for f in advisory)
    critical_high = counts.get("critical", 0) + counts.get("high", 0)
    adv_critical_high = adv_counts.get("critical", 0) + adv_counts.get("high", 0)
    if counts.get("critical", 0) > 0:
        status = "fail"
    elif strict and critical_high > 0:
        status = "fail"
    elif strict and adv_critical_high > 0:
        # Strict mode honors advisory critical/high too (the caller opted into rigor).
        status = "fail"
    elif critical_high > 0 or counts.get("medium", 0) > 0 or advisory:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "strict": strict,
        "provenance": provenance,
        "severity_counts": dict(counts),
        "advisory_counts": dict(adv_counts),
        "completion_allowed": status == "pass" or (status == "warn" and not strict),
        "rule": "critical (non-advisory) or strict critical/high fails; advisory/fallback findings only warn in non-strict; clean/empty diff has no change-review findings",
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    findings = [Finding(**f) for f in report.get("findings", [])]
    provenance = report.get("scope_provenance", report.get("gate", {}).get("provenance", "diff"))
    prov_note = {
        "empty-clean": "git working tree is clean against the diff base — this run changed nothing, so change-review scans were skipped (no findings can be attributed to it).",
        "diff": "scanned only the lines this run's diff ADDED (pre-existing/untouched code is out of scope).",
        "explicit": "scanned the explicitly supplied --path targets.",
        "fallback-no-git": "no git diff available — bounded whole-file scan; these findings are ADVISORY (cannot be attributed to this run) and do not hard-fail the gate in non-strict mode.",
    }.get(provenance, "")
    lines = [
        f"# Ultracode adversarial verification: {Path(report['run_dir']).name if report.get('run_dir') else 'no-run-dir'}\n\n",
        f"Generated: {report['generated_at_utc']}\n\n",
        f"Workspace: `{report['workspace']}`\n\n",
        f"Gate: `{report['gate']['status']}`; completion_allowed: `{report['gate']['completion_allowed']}`; strict: `{report['gate']['strict']}`\n\n",
        f"Scope: `{provenance}` — {prov_note}\n\n",
        "## Changed / inspected files\n\n",
    ]
    files = report.get("changed_files", [])
    if not files:
        if provenance == "empty-clean":
            lines.append("No files changed (clean working tree). Nothing to change-review.\n\n")
        else:
            lines.append("No changed file list was detected. The script used fallback inspection when possible.\n\n")
    else:
        for f in files[:120]:
            lines.append(f"- `{f}`\n")
        if len(files) > 120:
            lines.append(f"- ... {len(files) - 120} more\n")
        lines.append("\n")
    lines.append("## Findings\n\n")
    if not findings:
        lines.append("No deterministic adversarial findings. This does not prove correctness; still run the generated adversarial subagent prompt for high-impact changes.\n\n")
    else:
        for f in sorted(findings, key=lambda x: (severity_rank(x.severity), x.kind, x.claim)):
            adv = " (advisory — pre-existing, no diff to attribute)" if getattr(f, "advisory", False) else ""
            lines.append(f"### {f.severity.upper()} — {f.kind}{adv}\n\n")
            lines.append(f"{f.claim}\n\n")
            if f.evidence:
                lines.append("Evidence:\n")
                for e in f.evidence[:20]:
                    lines.append(f"- {e}\n")
            lines.append(f"\nRecommendation: {f.recommendation}\n\n")
    lines.append("## Required adversarial worker passes\n\n")
    lines.append("- `adversarial-claim-check`: verify claims against concrete files and command outputs.\n")
    lines.append("- `adversarial-edge-probes`: design counterexamples and minimal regression probes.\n")
    lines.append("- `adversarial-contract-review`: check APIs, CLI flags, install commands, docs, and path names.\n")
    lines.append("- `adversarial-verification-audit`: judge whether the verification is sufficient.\n\n")
    lines.append("Artifacts: `adversarial_verification.json`, `adversarial_work_items.csv`, `adversarial_spawn_prompt.md`.\n")
    path.write_text("".join(lines), encoding="utf-8")


def update_ledger(run_dir: Path, gate: dict[str, Any]) -> None:
    ledger = run_dir / "ledger.md"
    existing = ledger.read_text(encoding="utf-8", errors="replace") if ledger.exists() else f"# Ultracode final ledger: {run_dir.name}\n"
    marker = "\n## Adversarial verification artifact\n"
    add = (
        f"{marker}\n"
        f"- Adversarial report: `adversarial_verification.md`\n"
        f"- Adversarial JSON: `adversarial_verification.json`\n"
        f"- Adversarial worker CSV: `adversarial_work_items.csv`\n"
        f"- Gate status: `{gate['status']}`\n"
        f"- Completion allowed by deterministic gate: `{gate['completion_allowed']}`\n"
    )
    if marker in existing:
        existing = existing.split(marker)[0].rstrip() + "\n" + add
    else:
        existing = existing.rstrip() + "\n" + add
    ledger.write_text(existing, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic adversarial verification for an Ultracode run.")
    parser.add_argument("--workspace", default=".", help="Repo/workspace root.")
    parser.add_argument("--run-dir", default=None, help="Ultracode run directory. Defaults to newest .ultracode/runs/* when available.")
    parser.add_argument("--task", default="", help="Task text under review, used in adversarial spawn prompt.")
    parser.add_argument("--base", default="HEAD", help="Git diff base for changed-file detection.")
    parser.add_argument("--path", action="append", default=[], help="Explicit file/dir to inspect. Can be repeated.")
    parser.add_argument("--claim-file", action="append", default=[], help="File containing final claims or answer text. Can be repeated.")
    parser.add_argument("--strict", action="store_true", help="Fail gate on missing verification/adversarial result files or high findings.")
    parser.add_argument("--read-only", action="store_true", help="Read-only/audit run (this run changes no code): deterministic change-review findings are advisory (warn, never hard-fail) since they cannot be attributed to this run.")
    parser.add_argument("--execute", action="store_true", help="Also execute uc_verify.py --execute before adversarial gate when run-dir is present.")
    parser.add_argument("--timeout", type=int, default=120, help="Per-command timeout for delegated uc_verify.py.")
    args = parser.parse_args(argv)

    root = Path(args.workspace).resolve()
    if not root.exists():
        print(f"workspace does not exist: {root}", file=sys.stderr)
        return 2

    run_dir: Path | None = Path(args.run_dir).resolve() if args.run_dir else None
    if run_dir is None:
        # Prefer the pointer written by uc_route/uc_bootstrap (robust to --out-dir),
        # then scan the current (.ultracode/runs) and legacy (.codex/ultracode/runs) roots.
        pointer = root / ".ultracode" / "last_run_dir"
        try:
            if pointer.exists():
                p = Path(pointer.read_text(encoding="utf-8").strip())
                if p.is_dir():
                    run_dir = p
        except Exception:
            pass
        if run_dir is None:
            for rel_parts in ((".ultracode", "runs"), (".codex", "ultracode", "runs")):
                base = root.joinpath(*rel_parts)
                if base.exists():
                    runs = sorted(p for p in base.glob("*") if p.is_dir())
                    if runs:
                        run_dir = runs[-1]
                        break

    if args.execute and run_dir:
        verify_script = Path(__file__).resolve().parent / "uc_verify.py"
        cmd = [sys.executable, str(verify_script), "--workspace", str(root), "--run-dir", str(run_dir), "--execute", "--timeout", str(args.timeout)]
        try:
            subprocess.run(cmd, text=True, timeout=max(args.timeout * 3, 60))
        except subprocess.TimeoutExpired:
            # Do not crash the gate: a missing/partial verification.json is caught
            # downstream by scan_run_artifact_gaps and surfaced as a finding.
            print(f"uc_verify.py timed out after {max(args.timeout * 3, 60)}s; continuing to gate.", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"uc_verify.py could not run: {type(exc).__name__}: {exc}; continuing to gate.", file=sys.stderr)

    git_snapshot = collect_git_snapshot(root, args.base)
    changed_files, provenance = collect_changed_files(root, git_snapshot, args.path)
    # Real diff -> scan only ADDED lines; no-git fallback -> advisory whole-file scan.
    added_map = parse_added_lines(git_snapshot.get("diff_unified0", "")) if provenance == "diff" else None
    # read-only is the run's own metadata (route -> bootstrap run.json) OR the explicit flag.
    read_only = args.read_only or _run_read_only(run_dir)
    # Only findings tied to an actual diff on a code-changing run can HARD-FAIL. Everything
    # else — a no-git fallback, an explicit --path inspection, or any read-only run — is
    # advisory (surfaced, warns in non-strict, never blocks), because those findings cannot
    # be attributed to a change THIS run introduced. This is the universal rule that keeps a
    # read-only audit (even on a dirty tree or via --path) from hard-failing on pre-existing code.
    advisory = not (provenance == "diff" and not read_only)
    has_changes = provenance in {"diff", "explicit"} and bool(changed_files)
    tests = find_tests(root)
    run_artifacts = read_run_artifacts(run_dir)

    findings: list[Finding] = []
    findings.extend(scan_risk_patterns(root, changed_files, added_map=added_map, advisory=advisory))
    findings.extend(scan_test_gaps(root, changed_files, tests, advisory=advisory))
    findings.extend(scan_run_artifact_gaps(run_artifacts, args.strict, has_changes=has_changes))
    findings.extend(scan_adversarial_result_content(run_dir, run_artifacts, args.strict))
    findings.extend(scan_unsourced_metrics(run_dir, run_artifacts, args.strict))

    claim_files = [Path(p).resolve() if Path(p).is_absolute() else (root / p).resolve() for p in args.claim_file]
    if run_dir:
        for name in ["ledger.md", "synthesis.md", "verification.md"]:
            p = run_dir / name
            if p.exists():
                claim_files.append(p)
    findings.extend(scan_claim_file(root, claim_files))

    gate = gate_status(findings, args.strict, provenance=provenance)
    out_run_dir = run_dir or (root / ".ultracode" / "adversarial-no-run")
    out_run_dir.mkdir(parents=True, exist_ok=True)
    write_adversarial_work_items(out_run_dir, args.task, changed_files, root)

    report = {
        "generated_at_utc": now_utc(),
        "workspace": str(root),
        "run_dir": str(out_run_dir),
        "task": args.task,
        "scope_provenance": provenance,
        "git": git_snapshot,
        "changed_files": [rel(p, root) for p in changed_files],
        "tests_detected": [rel(p, root) for p in tests[:200]],
        "run_artifacts": run_artifacts,
        "findings": [asdict(f) for f in findings],
        "gate": gate,
    }
    json_path = out_run_dir / "adversarial_verification.json"
    md_path = out_run_dir / "adversarial_verification.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(md_path, report)
    if run_dir:
        update_ledger(run_dir, gate)

    print(json.dumps({
        "ok": True,
        "run_dir": str(out_run_dir),
        "gate": gate,
        "findings": len(findings),
        "report": str(md_path),
        "spawn_prompt": str(out_run_dir / "adversarial_spawn_prompt.md"),
    }, ensure_ascii=False, indent=2))
    return 1 if gate["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
