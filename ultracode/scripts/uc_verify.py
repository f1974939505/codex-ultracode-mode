#!/usr/bin/env python3
"""Detect and optionally execute project verification commands for Ultracode runs."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Directories that must never be walked: VCS metadata, dependency/build caches, the
# Ultracode run tree, and large data dirs. Pruning these is what keeps the scan from
# wedging on a huge or slow (e.g. WSL 9p/drvfs) filesystem.
EXCLUDE_DIR_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", ".codex",
    "node_modules", "dist", "build", "target", ".next", ".turbo", "coverage",
    "storage", ".cache", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode",
}
WALK_BUDGET_SECONDS = 8.0
PY_FILE_CAP = 200


def collect_py_files(root: Path, cap: int = PY_FILE_CAP, budget: float = WALK_BUDGET_SECONDS) -> tuple[list[Path], bool]:
    """Pruned, time-budgeted walk for *.py files; returns (files, truncated).

    Uses os.walk with in-place dirname pruning so excluded/hidden directories are
    never descended into (pathlib.rglob filters only AFTER descending, which is what
    hung this script on a 9p/drvfs mount). Bails out on a file-count cap or a
    wall-clock budget so a giant or slow tree can never wedge detection.
    """
    files: list[Path] = []
    start = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                files.append(Path(dirpath) / fn)
                if len(files) >= cap:
                    return files, True
        if time.monotonic() - start > budget:
            return files, True
    return files, False


def safe_read(path: Path, max_bytes: int = 65536) -> str:
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except Exception:
        return ""


def detect_commands(root: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    commands: list[dict[str, str]] = []
    scan: dict[str, Any] = {}
    if (root / "package.json").exists():
        try:
            pkg = json.loads(safe_read(root / "package.json"))
            scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
            for name in ["test", "lint", "typecheck", "build"]:
                if isinstance(scripts, dict) and name in scripts:
                    commands.append({"kind": name, "command": f"npm run {name}", "reason": f"package.json scripts.{name}"})
        except Exception:
            commands.append({"kind": "test", "command": "npm test", "reason": "package.json present"})
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tox.ini").exists():
        commands.append({"kind": "test", "command": "python -m pytest -q", "reason": "Python project/test config present"})
    py_files, py_truncated = collect_py_files(root)
    scan["py_files_scanned"] = len(py_files)
    scan["py_scan_truncated"] = py_truncated
    if py_files and not py_truncated:
        # Quote each path: filenames with spaces or shell metacharacters would
        # otherwise word-split under shell=True and falsely fail the syntax check.
        quoted = " ".join(shlex.quote(str(p.relative_to(root))) for p in py_files)
        commands.append({"kind": "syntax", "command": "python -m py_compile " + quoted, "reason": f"Python syntax check ({len(py_files)} files)"})
    elif py_truncated:
        scan["py_scan_note"] = "skipped blanket python syntax check: workspace too large or scan budget exceeded (e.g. slow/large filesystem); rely on the project's own test/lint commands"
    if (root / "Cargo.toml").exists():
        commands.append({"kind": "test", "command": "cargo test", "reason": "Cargo.toml present"})
    if (root / "go.mod").exists():
        commands.append({"kind": "test", "command": "go test ./...", "reason": "go.mod present"})
    if (root / "Makefile").exists() or (root / "makefile").exists():
        makefile = root / ("Makefile" if (root / "Makefile").exists() else "makefile")
        text = safe_read(makefile)
        if "\ntest:" in "\n" + text or "\ntest :" in "\n" + text:
            commands.append({"kind": "test", "command": "make test", "reason": "Makefile test target"})
        if "\nlint:" in "\n" + text or "\nlint :" in "\n" + text:
            commands.append({"kind": "lint", "command": "make lint", "reason": "Makefile lint target"})
    if (root / "CMakeLists.txt").exists() and (root / "build").exists():
        commands.append({"kind": "build", "command": "cmake --build build", "reason": "CMake build directory present"})
    # Deduplicate while preserving order.
    seen = set()
    out = []
    for cmd in commands:
        if cmd["command"] not in seen:
            seen.add(cmd["command"])
            out.append(cmd)
    return out, scan


def run_shell(command: str, cwd: Path, timeout: int) -> dict[str, Any]:
    proc = subprocess.run(command, cwd=str(cwd), shell=True, text=True, capture_output=True, timeout=timeout)
    output = ((proc.stdout or "") + (proc.stderr or ""))[-12000:]
    return {"command": command, "exit_code": proc.returncode, "status": "pass" if proc.returncode == 0 else "fail", "output_tail": output}


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        f"# Ultracode verification: {path.parent.name}\n\n",
        f"Generated: {report['generated_at_utc']}\n\n",
        f"Executed: `{report['executed']}`\n\n",
        "## Detected commands\n\n",
    ]
    if not report["commands"]:
        lines.append("No standard verification command detected. Add project-specific checks manually.\n\n")
    for c in report["commands"]:
        lines.append(f"- `{c['command']}` — {c.get('reason','')}\n")
    if report.get("results"):
        lines.append("\n## Results\n\n")
        for r in report["results"]:
            lines.append(f"### `{r['command']}`\n\n")
            lines.append(f"- Status: `{r['status']}`\n")
            lines.append(f"- Exit code: `{r['exit_code']}`\n\n")
            output = r.get("output_tail", "").strip()
            if output:
                lines.append("```text\n" + output[-6000:] + "\n```\n\n")
    path.write_text("".join(lines), encoding="utf-8")


def update_ledger(run_dir: Path, report_md: Path) -> None:
    ledger = run_dir / "ledger.md"
    if not ledger.exists():
        return
    text = ledger.read_text(encoding="utf-8", errors="replace")
    marker = "\n## Verification artifact\n"
    add = f"{marker}\n- Verification report: `{report_md.name}`\n- Verification JSON: `verification.json`\n"
    if marker in text:
        text = text.split(marker)[0].rstrip() + "\n" + add
    else:
        text = text.rstrip() + "\n" + add
    ledger.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect and optionally run verification checks for Ultracode runs.")
    parser.add_argument("--workspace", default=".", help="Repo/workspace root.")
    parser.add_argument("--run-dir", default=None, help="Ultracode run directory to write verification artifacts.")
    parser.add_argument("--execute", action="store_true", help="Actually run detected commands. Default only detects them.")
    parser.add_argument("--timeout", type=int, default=120, help="Per-command timeout in seconds.")
    args = parser.parse_args(argv)
    root = Path(args.workspace).resolve()
    if not root.exists():
        print(f"workspace does not exist: {root}", file=sys.stderr)
        return 2
    commands, scan = detect_commands(root)
    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "workspace": str(root),
        "executed": bool(args.execute),
        "scan": scan,
        "commands": commands,
        "results": [],
    }
    if args.execute:
        for c in commands:
            try:
                report["results"].append(run_shell(c["command"], root, args.timeout))
            except subprocess.TimeoutExpired as exc:
                report["results"].append({"command": c["command"], "exit_code": 124, "status": "timeout", "output_tail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                report["results"].append({"command": c["command"], "exit_code": 127, "status": "error", "output_tail": f"{type(exc).__name__}: {exc}"})

    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        json_path = run_dir / "verification.json"
        md_path = run_dir / "verification.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_markdown(md_path, report)
        update_ledger(run_dir, md_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failures = [r for r in report.get("results", []) if r.get("status") not in {"pass"}]
    return 1 if failures and args.execute else 0


if __name__ == "__main__":
    raise SystemExit(main())
