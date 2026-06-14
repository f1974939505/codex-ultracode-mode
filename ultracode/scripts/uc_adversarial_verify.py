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

RISK_PATTERNS: list[tuple[str, str, str, str]] = [
    ("critical", "dangerous-delete", r"\brm\s+-rf\s+(/|~|\$HOME|\*)|shutil\.rmtree\(|\.unlink\(|os\.remove\(", "destructive deletion or broad file removal"),
    ("high", "silent-exception", r"except\s+Exception\s*:\s*(pass|return\s+None|continue)|bare\s+except|except\s*:\s*(pass|continue)", "silent error suppression hides broken details"),
    ("high", "test-disabled", r"pytest\.mark\.skip|pytest\.mark\.xfail|\bskip\(|describe\.skip|it\.skip|test\.skip", "test appears disabled or weakened"),
    ("medium", "placeholder", r"TODO|FIXME|XXX|NotImplemented|raise\s+NotImplementedError|pass\s*(#|$)", "placeholder left in changed code"),
    ("medium", "broad-success-claim", r"all\s+tests\s+pass|fully\s+verified|完全验证|全部通过|已全部完成|no\s+issues", "strong claim may need executed evidence"),
    ("medium", "weak-test", r"assert\s+True|expect\(true\)\.toBe\(true\)|console\.log\(|print\(.{0,80}debug", "weak assertion/debug residue"),
    ("medium", "network-install", r"curl\b.*\|\s*(sh|bash)|wget\b.*\|\s*(sh|bash)|pip\s+install\s+[^\n#]+|npm\s+install\s+[^\n#]+", "network install or pipe-to-shell path needs explicit review"),
    ("low", "float-exact", r"\b(float|double)\b.*==|==\s*np\.nan|math\.isnan\([^)]*\)\s*==", "numeric exactness/NaN comparison risk"),
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


def collect_git_snapshot(root: Path, base: str) -> dict[str, Any]:
    if not git_available(root):
        return {"available": False, "base": base, "status_short": "", "diff_stat": "", "diff_name_only": [], "diff_unified0": ""}
    _, status = run_cmd(["git", "status", "--short"], root)
    _, stat = run_cmd(["git", "diff", "--stat", base, "--"], root)
    _, names = run_cmd(["git", "diff", "--name-only", base, "--"], root)
    _, staged_names = run_cmd(["git", "diff", "--cached", "--name-only", "--"], root)
    _, unified = run_cmd(["git", "diff", "--unified=0", base, "--"], root, timeout=30)
    name_list = sorted(set([n.strip() for n in (names + "\n" + staged_names).splitlines() if n.strip()]))
    return {
        "available": True,
        "base": base,
        "status_short": status[:20_000],
        "diff_stat": stat[:20_000],
        "diff_name_only": name_list,
        "diff_unified0": unified[-80_000:],
    }


def collect_changed_files(root: Path, git_snapshot: dict[str, Any], explicit_paths: list[str]) -> list[Path]:
    paths: list[Path] = []
    if explicit_paths:
        for p in explicit_paths:
            candidate = (root / p).resolve()
            if candidate.exists():
                if candidate.is_file():
                    paths.append(candidate)
                elif candidate.is_dir():
                    sub, _ = walk_files(candidate, cap=2000)
                    paths.extend(x for x in sub if not should_skip(x))
    elif git_snapshot.get("available"):
        for name in git_snapshot.get("diff_name_only", []):
            p = (root / name).resolve()
            if p.exists() and p.is_file() and not should_skip(p):
                paths.append(p)
    if not paths:
        # Fallback: inspect a bounded set of likely source/config/docs files.
        all_files, _ = walk_files(root, cap=4000)
        for p in all_files:
            if not should_skip(p) and (p.suffix in SOURCE_SUFFIXES or p.suffix in DOC_SUFFIXES or p.name in CONFIG_NAMES):
                paths.append(p)
                if len(paths) >= 200:
                    break
    seen = set()
    out = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out[:500]


def line_number(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def scan_risk_patterns(root: Path, files: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        if path.suffix not in SOURCE_SUFFIXES and path.suffix not in DOC_SUFFIXES and path.name not in CONFIG_NAMES:
            continue
        text = safe_read(path)
        if not text:
            continue
        for severity, kind, pattern, why in RISK_PATTERNS:
            matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE))[:8]
            if not matches:
                continue
            evidence = []
            for m in matches:
                ln = line_number(text, m.start())
                snippet = text[m.start():m.end()].replace("\n", " ")[:180]
                evidence.append(f"{rel(path, root)}:L{ln}: {snippet}")
            findings.append(Finding(
                severity=severity,
                kind=kind,
                claim=f"Potential {kind}: {why}.",
                evidence=evidence,
                recommendation="Review this manually; either justify it in the ledger, remove it, or add targeted verification.",
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


def scan_test_gaps(root: Path, changed_files: list[Path], tests: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    changed_sources = [p for p in changed_files if p.suffix in SOURCE_SUFFIXES and not re.search(r"(^|/)(test|tests)(/|$)|test_|_test|\.spec\.|\.test\.", rel(p, root), flags=re.I)]
    if changed_sources and not tests:
        findings.append(Finding(
            severity="high",
            kind="no-tests-found",
            claim="Source files changed or inspected, but no recognizable test files were found.",
            evidence=[rel(p, root) for p in changed_sources[:20]],
            recommendation="Add targeted tests or explicitly document why deterministic tests are unavailable.",
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
        ))
    return findings


def load_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


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


def scan_run_artifact_gaps(run_artifacts: dict[str, Any], strict: bool) -> list[Finding]:
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
        if read_only:
            pass  # read-only audit: no code changed, so unrun project checks are expected, not a gap.
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


def gate_status(findings: list[Finding], strict: bool) -> dict[str, Any]:
    counts = Counter(f.severity for f in findings)
    critical_high = counts.get("critical", 0) + counts.get("high", 0)
    if counts.get("critical", 0) > 0:
        status = "fail"
    elif strict and critical_high > 0:
        status = "fail"
    elif critical_high > 0 or counts.get("medium", 0) > 0:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "strict": strict,
        "severity_counts": dict(counts),
        "completion_allowed": status == "pass" or (status == "warn" and not strict),
        "rule": "strict fails on any critical/high; non-strict warns unless critical verification failure exists",
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    findings = [Finding(**f) for f in report.get("findings", [])]
    lines = [
        f"# Ultracode adversarial verification: {Path(report['run_dir']).name if report.get('run_dir') else 'no-run-dir'}\n\n",
        f"Generated: {report['generated_at_utc']}\n\n",
        f"Workspace: `{report['workspace']}`\n\n",
        f"Gate: `{report['gate']['status']}`; completion_allowed: `{report['gate']['completion_allowed']}`; strict: `{report['gate']['strict']}`\n\n",
        "## Changed / inspected files\n\n",
    ]
    files = report.get("changed_files", [])
    if not files:
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
            lines.append(f"### {f.severity.upper()} — {f.kind}\n\n")
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
    changed_files = collect_changed_files(root, git_snapshot, args.path)
    tests = find_tests(root)
    run_artifacts = read_run_artifacts(run_dir)

    findings: list[Finding] = []
    findings.extend(scan_risk_patterns(root, changed_files))
    findings.extend(scan_test_gaps(root, changed_files, tests))
    findings.extend(scan_run_artifact_gaps(run_artifacts, args.strict))
    findings.extend(scan_adversarial_result_content(run_dir, run_artifacts, args.strict))

    claim_files = [Path(p).resolve() if Path(p).is_absolute() else (root / p).resolve() for p in args.claim_file]
    if run_dir:
        for name in ["ledger.md", "synthesis.md", "verification.md"]:
            p = run_dir / name
            if p.exists():
                claim_files.append(p)
    findings.extend(scan_claim_file(root, claim_files))

    gate = gate_status(findings, args.strict)
    out_run_dir = run_dir or (root / ".ultracode" / "adversarial-no-run")
    out_run_dir.mkdir(parents=True, exist_ok=True)
    write_adversarial_work_items(out_run_dir, args.task, changed_files, root)

    report = {
        "generated_at_utc": now_utc(),
        "workspace": str(root),
        "run_dir": str(out_run_dir),
        "task": args.task,
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
