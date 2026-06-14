#!/usr/bin/env python3
"""Bootstrap an Ultracode-style Codex workflow run.

Creates a durable run directory with:
- repo_inventory.json
- work_items.csv
- plan.md
- spawn_agents_prompt.md
- ledger.md

This script is intentionally dependency-free and does not modify source files
outside the selected run directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", ".codex/ultracode", ".ultracode", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    "target", ".tox", ".idea", ".vscode", ".next", ".turbo", "coverage", ".cache",
}

LANG_BY_SUFFIX = {
    ".py": "python", ".ipynb": "python-notebook", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "c/c++", ".cpp": "c++", ".cc": "c++", ".hpp": "c++",
    ".f90": "fortran", ".f95": "fortran", ".f03": "fortran", ".f08": "fortran", ".f": "fortran",
    ".md": "markdown", ".rst": "restructuredtext", ".tex": "tex", ".bib": "bibtex",
    ".yml": "yaml", ".yaml": "yaml", ".json": "json", ".toml": "toml",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
    ".sql": "sql", ".html": "html", ".css": "css", ".scss": "scss",
}

ROLE_PRIORITY = ["doc_mapper", "test_mapper", "core_mapper", "risk_reviewer", "area_mapper", "planner", "verifier"]


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except Exception as exc:  # noqa: BLE001
        return 127, f"{type(exc).__name__}: {exc}"


def is_excluded(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    parts = rel.parts
    for i in range(1, len(parts) + 1):
        joined = "/".join(parts[:i])
        if joined in EXCLUDE_DIRS or parts[i - 1] in EXCLUDE_DIRS:
            return True
    return False


def safe_read(path: Path, max_bytes: int = 16384) -> str:
    try:
        data = path.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def detect_project_root(workspace: Path) -> Path:
    code, out = run_cmd(["git", "rev-parse", "--show-toplevel"], workspace)
    if code == 0 and out:
        return Path(out.splitlines()[0]).resolve()
    return workspace.resolve()


def list_files(root: Path, max_files: int = 20000) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dpath = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not is_excluded(dpath / d, root)]
        if is_excluded(dpath, root):
            continue
        for name in filenames:
            f = dpath / name
            if is_excluded(f, root):
                continue
            files.append(f)
            if len(files) >= max_files:
                return files
    return files


def detect_commands(root: Path) -> list[dict[str, str]]:
    commands: list[dict[str, str]] = []
    if (root / "package.json").exists():
        pkg_text = safe_read(root / "package.json", 65536)
        try:
            pkg = json.loads(pkg_text)
            scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
            for name in ["test", "lint", "typecheck", "build"]:
                if isinstance(scripts, dict) and name in scripts:
                    commands.append({"kind": name, "command": f"npm run {name}", "reason": f"package.json scripts.{name}"})
        except json.JSONDecodeError:
            commands.append({"kind": "node", "command": "npm test", "reason": "package.json exists but could not parse scripts"})
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tox.ini").exists():
        commands.append({"kind": "test", "command": "python -m pytest -q", "reason": "Python test config present"})
    if (root / "Cargo.toml").exists():
        commands.append({"kind": "test", "command": "cargo test", "reason": "Cargo.toml present"})
        commands.append({"kind": "lint", "command": "cargo clippy --all-targets -- -D warnings", "reason": "Rust project"})
    if (root / "go.mod").exists():
        commands.append({"kind": "test", "command": "go test ./...", "reason": "go.mod present"})
    if (root / "Makefile").exists() or (root / "makefile").exists():
        makefile = root / ("Makefile" if (root / "Makefile").exists() else "makefile")
        text = safe_read(makefile, 65536)
        if re.search(r"^test\s*:", text, re.M):
            commands.append({"kind": "test", "command": "make test", "reason": "Makefile test target"})
        if re.search(r"^lint\s*:", text, re.M):
            commands.append({"kind": "lint", "command": "make lint", "reason": "Makefile lint target"})
    if (root / "CMakeLists.txt").exists():
        commands.append({"kind": "build", "command": "cmake --build build", "reason": "CMakeLists.txt present; assumes configured build dir"})
    return commands


def detect_docs(root: Path) -> list[str]:
    candidates = [
        "AGENTS.md", "AGENTS.override.md", "README.md", "README.rst", "CONTRIBUTING.md",
        "CLAUDE.md", "CODEX.md", "docs/README.md", "docs/index.md", "pyproject.toml", "package.json",
    ]
    return [p for p in candidates if (root / p).exists()]


def git_status(root: Path) -> dict[str, Any]:
    code, out = run_cmd(["git", "status", "--short"], root)
    if code != 0:
        return {"available": False, "status_short": out[:4000]}
    code_branch, branch = run_cmd(["git", "branch", "--show-current"], root)
    return {"available": True, "branch": branch if code_branch == 0 else "", "status_short": out[:4000]}


def task_complexity(task: str, file_count: int, mode: str) -> dict[str, Any]:
    text = task.lower()
    signals = []
    keywords = {
        "large-scope": ["all", "entire", "repo", "repository", "codebase", "全项目", "整个", "所有", "目录结构", "重构", "审阅当前项目"],
        "migration": ["migrate", "migration", "port", "upgrade", "迁移", "升级"],
        "audit": ["audit", "review", "security", "bug sweep", "审查", "检查", "安全"],
        "refactor": ["refactor", "cleanup", "restructure", "重构", "整理", "清理"],
        "verification": ["verify", "test", "prove", "验证", "测试", "复核"],
        "ultracode": ["dynamic workflow", "$ultracode"],
    }
    for signal, words in keywords.items():
        if any(w in text for w in words):
            signals.append(signal)
    score = len(signals)
    if file_count > 50:
        score += 1
        signals.append("repo-size>50")
    if file_count > 300:
        score += 1
        signals.append("repo-size>300")
    explicit = mode != "auto" or "$ultracode" in text
    recommended = explicit or score >= 2
    return {"score": score, "signals": sorted(set(signals)), "recommended": recommended}


def top_level_groups(root: Path, files: list[Path], max_groups: int) -> list[dict[str, Any]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        rel = f.relative_to(root)
        top = rel.parts[0] if len(rel.parts) > 1 else "."
        if top.startswith(".") and top not in {".", ".codex"}:
            continue
        groups[top].append(f)
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:max_groups]
    result = []
    for name, gfiles in ranked:
        suffixes = Counter([p.suffix.lower() or "<none>" for p in gfiles])
        langs = Counter(LANG_BY_SUFFIX.get(s, "other") for s, _ in suffixes.items())
        result.append({"name": name, "file_count": len(gfiles), "languages": dict(langs.most_common(5))})
    return result


def create_work_items(groups: list[dict[str, Any]], mode: str, max_workers: int) -> list[dict[str, str]]:
    items: list[dict[str, str]] = [
        {
            "id": "docs-constraints",
            "role": "ultracode_doc_mapper",
            "path": "AGENTS.md README* docs/ config files",
            "objective": "LANE: docs/config truth-sources only. Map instructions, constraints, generated-file rules, conflict-resolution priorities, and doc-vs-doc / doc-vs-config contradictions. Do NOT audit source-code behavior, security, or tests (other roles own those).",
            "expected_output": "JSON worker report; verdict=not-applicable.",
        },
        {
            "id": "test-build-map",
            "role": "ultracode_test_mapper",
            "path": "test/build/CI files",
            "objective": "LANE: verification maturity only. Identify reliable verification commands, CI, fixtures, eval/QA harnesses, and fragile/expensive checks. Do NOT read API/app source to make auth/security claims or restate the architecture verdict (defer to reviewer/mapper).",
            "expected_output": "JSON worker report; verdict=not-applicable.",
        },
        {
            "id": "architecture-map",
            "role": "ultracode_mapper",
            "path": "repo root and primary source dirs",
            "objective": "LANE: implemented capability via source code only. Map entry points, core modules, data/control flow, hidden coupling, real-vs-vaporware. Do NOT re-derive doc contradictions (doc_mapper) or security posture (reviewer).",
            "expected_output": "JSON worker report; verdict=not-applicable.",
        },
        {
            "id": "risk-review",
            "role": "ultracode_reviewer",
            "path": "security/compliance/data-egress/IP surface",
            "objective": "LANE: security, compliance, data-egress/privacy, IP, destructive-operation risk, and falsifying claims. This role SCORES: set verdict=pass|concerns|fail (status stays ok). Do NOT re-map architecture/docs already covered; cite them.",
            "expected_output": "JSON worker report with severity-ranked findings; verdict carries the judgment.",
        },
        {
            "id": "adversarial-claim-check",
            "role": "ultracode_claim_checker",
            "path": "final claims, ledger, patch notes, changed files",
            "objective": "Check whether every material claim is supported by concrete file, diff, command, or artifact evidence. Focus on small path/name/flag mistakes.",
            "expected_output": "JSON worker report classifying claims as supported, unsupported, contradicted, or needs-confirmation.",
        },
        {
            "id": "adversarial-edge-review",
            "role": "ultracode_edge_tester",
            "path": "task-relevant changed behavior and verification commands",
            "objective": "Design minimal counterexamples and edge-case probes that could falsify the implementation or installation flow.",
            "expected_output": "JSON worker report with probes, results or exact skipped-command reasons, and missing tests.",
        },
    ]
    remaining = max(0, max_workers - len(items) - 2)
    for group in groups[:remaining]:
        name = group["name"]
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "root"
        items.append({
            "id": f"area-{safe}",
            "role": "ultracode_mapper",
            "path": name,
            "objective": f"Inspect the `{name}` area for task-relevant files, constraints, duplicated logic, and refactor/audit targets.",
            "expected_output": "JSON worker report with path-specific evidence.",
        })
    items.append({
        "id": "synthesis-plan",
        "role": "ultracode_planner",
        "path": "run artifacts and worker results",
        "objective": "Merge evidence into a bounded implementation or audit plan. Preserve conflicts as needs-confirmation.",
        "expected_output": "JSON or Markdown plan with ordered work packages.",
    })
    items.append({
        "id": "verification-review",
        "role": "ultracode_verifier",
        "path": "changed files and verification commands",
        "objective": "After implementation, verify behavior and run an adversarial review pass.",
        "expected_output": "JSON worker report with commands, status, and remaining risks.",
    })
    return items


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "role", "path", "objective", "expected_output"])
        writer.writeheader()
        writer.writerows(rows)


def write_plan(run_dir: Path, task: str, mode: str, inventory: dict[str, Any], items: list[dict[str, str]], complexity: dict[str, Any]) -> None:
    plan = []
    plan.append(f"# Ultracode run plan: {run_dir.name}\n")
    plan.append(f"Task: {task}\n")
    plan.append(f"Mode: `{mode}`\n")
    plan.append(f"Recommended dynamic workflow: `{complexity['recommended']}`; signals: {', '.join(complexity['signals']) or 'none'}\n")
    plan.append("## Repository snapshot\n")
    plan.append(f"- Root: `{inventory['root']}`\n")
    plan.append(f"- Files scanned: {inventory['file_count']}\n")
    plan.append(f"- Dominant languages: {json.dumps(inventory['languages'], ensure_ascii=False)}\n")
    plan.append(f"- Detected docs/configs: {', '.join(inventory['docs']) or 'none'}\n")
    plan.append("## Phases\n")
    phases = [
        "0. Bootstrap run artifacts and record scope.",
        "1. Spawn read-only mapper/doc/test/risk subagents.",
        "2. Merge reconnaissance and decompose bounded work packages.",
        "3. Run implementation or audit workers, depending on mode.",
        "4. Synthesize results and resolve conflicts by evidence quality.",
        "5. Run deterministic verification and adversarial claim/edge review.",
        "6. Run uc_adversarial_verify.py, resolve or ledger all high-risk findings.",
        "7. Update final ledger and answer with scope, changes, verification, adversarial gate, and risks.",
    ]
    plan.extend(f"- {p}\n" for p in phases)
    plan.append("## Initial work items\n")
    for item in items:
        plan.append(f"- `{item['id']}` / `{item['role']}` / `{item['path']}`: {item['objective']}\n")
    (run_dir / "plan.md").write_text("".join(plan), encoding="utf-8")


def load_agent_instructions() -> dict[str, str]:
    """Read developer_instructions from the bundled agent TOMLs (sibling of scripts/).

    Used to inline role discipline into the spawn prompt so that when Codex cannot
    invoke a named custom agent (e.g. tool-backed sessions fall back to a generic
    worker), the worker still receives the role's instructions.
    """
    agents_dir = Path(__file__).resolve().parent.parent / "agents"
    out: dict[str, str] = {}
    if not agents_dir.exists():
        return out
    try:
        import tomllib  # Python 3.11+
    except Exception:  # noqa: BLE001
        tomllib = None  # type: ignore[assignment]
    for p in sorted(agents_dir.glob("ultracode_*.toml")):
        try:
            text = p.read_text(encoding="utf-8")
            if tomllib is not None:
                data = tomllib.loads(text)
                name = str(data.get("name", p.stem))
                instr = str(data.get("developer_instructions", "")).strip()
            else:
                name = p.stem
                m = re.search(r'developer_instructions\s*=\s*"""(.*?)"""', text, re.S)
                instr = m.group(1).strip() if m else ""
            if instr:
                out[name] = instr
        except Exception:  # noqa: BLE001
            continue
    return out


def write_spawn_prompt(run_dir: Path, items: list[dict[str, str]]) -> None:
    lines = [
        "# Spawn agents prompt\n\n",
        "Use Codex subagents explicitly. For small sets, spawn one subagent per high-value work item. For many similar rows, call `spawn_agents_on_csv` with `work_items.csv` (requires the experimental `enable_fanout` feature; if it is unavailable, spawn workers individually).\n\n",
        "Concurrency note: Codex caps concurrent subagents at `agents.max_threads` (default 6). The `ultracode-xhigh` profile raises this to 16; without that profile active, keep concurrent workers <= 6 or run the CSV in batches.\n\n",
        "Named-agent fallback: prefer invoking the role agent by name (e.g. `ultracode_mapper`). If your Codex surface cannot invoke a named custom agent, spawn a generic worker and PASTE the matching role instructions from the section below into its prompt so the role discipline is preserved.\n\n",
        "Each worker must return one JSON object with keys: id, agent, scope, status, verdict, summary, evidence, findings, changes, verification, recommendations, open_questions.\n\n",
        "Two axes — keep them separate: `status` = execution only (ok | blocked | error: did you finish your assigned read?). `verdict` = your assessment of what you reviewed (pass | concerns | fail | not-applicable). Pure mappers/evidence-gatherers use verdict = not-applicable; only judging roles (reviewer, claim_checker, edge_tester, verifier, adversary) return pass/concerns/fail. Never encode a judgment in `status` (a completed-but-negative review is status=ok, verdict=fail).\n\n",
        "Lane discipline: each worker stays strictly inside its assigned `path`/role lane and must NOT re-derive another role's findings — cite the owning role instead. This keeps a fan-out of N workers complementary rather than N near-duplicate audits.\n\n",
        "CSV path: `work_items.csv` inside this run directory.\n\n",
        "Suggested instruction template for CSV fan-out:\n\n",
        "```text\n",
        "Review `{path}` for `{objective}` as `{role}`. Stay within the assigned scope. If the role name contains adversarial, claim, or edge, try to falsify the result rather than approve it. Return a single JSON object with keys: id, agent, scope, status, verdict, summary, evidence, findings, changes, verification, recommendations, open_questions. Call report_agent_job_result exactly once.\n",
        "```\n\n",
        "## Work item summary\n",
    ]
    for item in items:
        lines.append(f"- {item['id']}: role={item['role']} path={item['path']}\n")

    instructions = load_agent_instructions()
    roles_in_use = list(dict.fromkeys(item["role"] for item in items))
    inlined = [r for r in roles_in_use if r in instructions]
    if inlined:
        lines.append("\n## Role fallback instructions (inline when a named agent cannot be invoked)\n\n")
        for role in inlined:
            lines.append(f"### {role}\n\n")
            lines.append("```text\n" + instructions[role].strip() + "\n```\n\n")
    (run_dir / "spawn_agents_prompt.md").write_text("".join(lines), encoding="utf-8")


def write_ledger(run_dir: Path, task: str) -> None:
    text = f"""# Ultracode final ledger: {run_dir.name}

## Task

{task}

## Scope

- Included: TBD
- Excluded: TBD

## Evidence summary

TBD after subagent reconnaissance and synthesis.

## Changes made

TBD.

## Verification

TBD.

## Unresolved risks

- TBD

## Final status

`in-progress`
"""
    (run_dir / "ledger.md").write_text(text, encoding="utf-8")


def make_run_id(task: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(task.encode("utf-8")).hexdigest()[:8]
    return f"{now}-{digest}"


def prepare_run_dir(root: Path, out_dir_arg: str | None, run_id: str) -> tuple[Path | None, dict | None]:
    """Resolve + create the run directory and make it private and discoverable.

    Default is <root>/.ultracode/runs/<id> — inside the writable workspace, NOT under
    <root>/.codex which Codex makes read-only in workspace-write sandboxes. The dir is
    chmod 0700 (run artifacts can hold private/business data and must not be world-
    readable on shared hosts). A <root>/.ultracode/.gitignore ('*') keeps artifacts out
    of git, and <root>/.ultracode/last_run_dir records the path so the Stop hook can
    find the run even when --out-dir points elsewhere. Returns (run_dir, None) or
    (None, error_dict) when the target is not writable.
    """
    run_dir = Path(out_dir_arg).resolve() if out_dir_arg else root / ".ultracode" / "runs" / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        probe = run_dir / ".uc_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return None, {
            "ok": False,
            "error": "run-dir-not-writable",
            "path": str(run_dir),
            "hint": ("Run dir not writable. Codex makes the project's .codex/ read-only under the "
                     "workspace-write sandbox, but the rest of the project is writable. Pass --out-dir "
                     "to a writable project path NOT under .codex (e.g. --out-dir .ultracode/runs/<id>); "
                     "if no project path is writable, use a private temp dir from `mktemp -d` (mode 0700) "
                     "rather than a fixed world-readable /tmp path on shared hosts."),
            "detail": f"{type(exc).__name__}: {exc}",
        }
    try:
        os.chmod(run_dir, 0o700)
    except OSError:
        pass
    try:
        base = root / ".ultracode"
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
        gi = base / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        (base / "last_run_dir").write_text(str(run_dir) + "\n", encoding="utf-8")
    except OSError:
        pass
    return run_dir, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Ultracode dynamic workflow artifacts for Codex.")
    parser.add_argument("--workspace", default=".", help="Repository/workspace path.")
    parser.add_argument("--task", required=True, help="User task to plan.")
    parser.add_argument("--mode", default="auto", choices=["auto", "audit", "plan-only", "implementation", "migration", "refactor", "bugfix", "research"], help="Workflow mode.")
    parser.add_argument("--max-workers", type=int, default=16, help="Maximum initial work items / worker cap.")
    parser.add_argument("--out-dir", default=None, help="Optional output directory for run artifacts. Defaults to .ultracode/runs/<run-id> under repo root.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if not workspace.exists():
        print(f"workspace does not exist: {workspace}", file=sys.stderr)
        return 2
    root = detect_project_root(workspace)
    files = list_files(root)
    lang_counts = Counter(LANG_BY_SUFFIX.get(f.suffix.lower(), "other") for f in files)
    groups = top_level_groups(root, files, max_groups=max(1, args.max_workers))
    docs = detect_docs(root)
    commands = detect_commands(root)
    complexity = task_complexity(args.task, len(files), args.mode)
    inventory = {
        "root": str(root),
        "workspace": str(workspace),
        "file_count": len(files),
        "languages": dict(lang_counts.most_common(12)),
        "top_level_groups": groups,
        "docs": docs,
        "detected_commands": commands,
        "git": git_status(root),
    }
    run_id = make_run_id(args.task)
    run_dir, err = prepare_run_dir(root, args.out_dir, run_id)
    if err:
        print(json.dumps(err, ensure_ascii=False))
        return 3
    (run_dir / "results").mkdir(exist_ok=True)

    items = create_work_items(groups, args.mode, max(4, args.max_workers))
    run_meta = {
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task": args.task,
        "mode": args.mode,
        "max_workers": args.max_workers,
        "complexity": complexity,
        "inventory_file": "repo_inventory.json",
        "work_items_file": "work_items.csv",
        "plan_file": "plan.md",
        "ledger_file": "ledger.md",
    }

    (run_dir / "run.json").write_text(json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "repo_inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(run_dir / "work_items.csv", items)
    write_plan(run_dir, args.task, args.mode, inventory, items, complexity)
    write_spawn_prompt(run_dir, items)
    write_ledger(run_dir, args.task)

    print(json.dumps({"ok": True, "run_dir": str(run_dir), "run_id": run_id, "recommended": complexity["recommended"], "work_items": len(items)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
