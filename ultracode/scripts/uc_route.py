#!/usr/bin/env python3
"""Create a first-pass routing artifact for `$ultracode` tasks.

This script intentionally does not replace the model's judgement. It gathers
workspace/task signals and writes route.json + routing.md so the active Codex
model can make the first workflow decision before any edit or subagent spawn.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", ".codex/ultracode", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    "target", ".tox", ".idea", ".vscode", ".next", ".turbo", "coverage", ".cache",
}
CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".kts", ".c", ".h", ".cpp", ".cc", ".hpp", ".f", ".f90", ".f95",
    ".f03", ".f08", ".sh", ".bash", ".ps1", ".sql",
}
DOC_SUFFIXES = {".md", ".rst", ".txt", ".tex", ".bib"}
CONFIG_NAMES = {
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Makefile", "makefile",
    "CMakeLists.txt", "pytest.ini", "tox.ini", "ruff.toml", "tsconfig.json", "AGENTS.md",
    "README.md", "CLAUDE.md", "CODEX.md", "hooks.json",
}


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 5) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return 127, f"{type(exc).__name__}: {exc}"


def detect_root(workspace: Path) -> Path:
    code, out = run_cmd(["git", "rev-parse", "--show-toplevel"], workspace)
    if code == 0 and out:
        return Path(out.splitlines()[0]).resolve()
    return workspace.resolve()


def is_excluded(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    for i, part in enumerate(rel.parts, start=1):
        joined = "/".join(rel.parts[:i])
        if joined in EXCLUDE_DIRS or part in EXCLUDE_DIRS:
            return True
    return False


def list_files(root: Path, max_files: int = 25000) -> list[Path]:
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


def safe_read(path: Path, max_bytes: int = 65536) -> str:
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except Exception:
        return ""


def detect_commands(root: Path) -> list[dict[str, str]]:
    commands: list[dict[str, str]] = []
    package_json = root / "package.json"
    if package_json.exists():
        try:
            pkg = json.loads(safe_read(package_json))
            scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
            if isinstance(scripts, dict):
                for name in ["test", "lint", "typecheck", "build", "format"]:
                    if name in scripts:
                        commands.append({"kind": name, "command": f"npm run {name}", "reason": f"package.json scripts.{name}"})
        except json.JSONDecodeError:
            commands.append({"kind": "node", "command": "npm test", "reason": "package.json present but scripts could not be parsed"})
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tox.ini").exists():
        commands.append({"kind": "test", "command": "python -m pytest -q", "reason": "Python project/test config present"})
    elif any(p.suffix == ".py" for p in list_files(root, 200)):
        commands.append({"kind": "syntax", "command": "python -m py_compile $(git ls-files '*.py')", "reason": "Python files present"})
    if (root / "Cargo.toml").exists():
        commands.append({"kind": "test", "command": "cargo test", "reason": "Cargo.toml present"})
        commands.append({"kind": "lint", "command": "cargo clippy --all-targets -- -D warnings", "reason": "Rust project"})
    if (root / "go.mod").exists():
        commands.append({"kind": "test", "command": "go test ./...", "reason": "go.mod present"})
    makefile = root / "Makefile"
    if not makefile.exists():
        makefile = root / "makefile"
    if makefile.exists():
        text = safe_read(makefile)
        if re.search(r"^test\s*:", text, re.M):
            commands.append({"kind": "test", "command": "make test", "reason": "Makefile test target"})
        if re.search(r"^lint\s*:", text, re.M):
            commands.append({"kind": "lint", "command": "make lint", "reason": "Makefile lint target"})
    if (root / "CMakeLists.txt").exists():
        commands.append({"kind": "build", "command": "cmake --build build", "reason": "CMakeLists.txt present; assumes configured build dir"})
    return commands


def git_info(root: Path) -> dict[str, Any]:
    code, status = run_cmd(["git", "status", "--short"], root)
    if code != 0:
        return {"available": False, "status_short": status[:4000]}
    code_branch, branch = run_cmd(["git", "branch", "--show-current"], root)
    code_diff, diff = run_cmd(["git", "diff", "--name-only"], root)
    return {
        "available": True,
        "branch": branch if code_branch == 0 else "",
        "status_short": status[:4000],
        "diff_files": diff.splitlines() if code_diff == 0 and diff else [],
    }


def task_signals(task: str) -> dict[str, Any]:
    text = task.lower()
    signal_map = {
        "plan_only": ["plan only", "plan-only", "不要修改", "不修改", "只给方案", "提出方案", "plan mode"],
        "audit": ["audit", "review", "检查", "审阅", "复核", "code review", "安全", "漏洞", "风险"],
        "implementation": ["fix", "implement", "modify", "change", "patch", "write", "update", "修复", "修改", "实现", "改", "补丁"],
        "migration": ["migration", "migrate", "port", "upgrade", "批量", "迁移", "升级", "500 files"],
        "refactor": ["refactor", "restructure", "cleanup", "organize", "重构", "整理", "目录结构", "清理"],
        "verification": ["verify", "test", "validate", "smoke", "验证", "测试", "复核", "确保", "核对"],
        "adversarial": ["adversarial", "falsify", "claim", "edge", "对抗", "找茬", "反例", "边界", "细节", "小细节"],
        "install_flow": ["install", "installer", "uninstall", "one-click", "setup", "安装", "卸载", "一键"],
        "package_generation": ["zip", "package", "skill", "hook", "script", "打包", "技能", "脚本"],
        "research": ["research", "调研", "搜索", "官方", "文档", "cite", "引用", "资料"],
        "large_scope": ["entire repo", "whole repo", "codebase", "repository", "所有", "整个", "全项目", "全仓库"],
    }
    hits: dict[str, list[str]] = {}
    for name, words in signal_map.items():
        found = [w for w in words if w in text]
        if found:
            hits[name] = found
    return {"hits": hits, "count": len(hits)}


def infer_route(task: str, files: list[Path], root: Path, commands: list[dict[str, str]], git: dict[str, Any]) -> dict[str, Any]:
    signals = task_signals(task)
    suffixes = Counter(p.suffix.lower() or "<none>" for p in files)
    code_count = sum(1 for p in files if p.suffix.lower() in CODE_SUFFIXES)
    doc_count = sum(1 for p in files if p.suffix.lower() in DOC_SUFFIXES or p.name in {"README.md", "AGENTS.md"})
    config_count = sum(1 for p in files if p.name in CONFIG_NAMES)
    changed_count = len(git.get("diff_files", [])) if git.get("available") else 0
    large_repo = len(files) > 300 or code_count > 120
    medium_repo = len(files) > 60 or code_count > 30
    hits = signals["hits"]

    plan_only = "plan_only" in hits
    audit = "audit" in hits
    impl = "implementation" in hits and not plan_only
    migration = "migration" in hits
    refactor = "refactor" in hits
    verification = "verification" in hits or impl or migration or refactor or changed_count > 0
    adversarial = "adversarial" in hits or verification or "package_generation" in hits or "install_flow" in hits
    research = "research" in hits and not impl
    install_flow = "install_flow" in hits
    package_generation = "package_generation" in hits
    large_scope = "large_scope" in hits or large_repo

    if plan_only:
        execution_mode = "plan-only"
    elif research and not (impl or migration or refactor):
        execution_mode = "research"
    elif adversarial and not (impl or migration or refactor or audit) and changed_count > 0:
        execution_mode = "adversarial-only"
    elif migration:
        execution_mode = "migration"
    elif refactor:
        execution_mode = "refactor"
    elif impl:
        execution_mode = "implementation"
    elif audit:
        execution_mode = "audit"
    elif medium_repo or changed_count > 3:
        execution_mode = "full"
    else:
        execution_mode = "lightweight"

    needs_parallel = execution_mode in {"audit", "migration", "refactor", "full", "research"} or large_scope or large_repo
    max_workers = 4
    if large_repo or migration:
        max_workers = 12
    elif needs_parallel or medium_repo:
        max_workers = 8
    if install_flow or package_generation:
        max_workers = max(max_workers, 8)

    subagents: list[str] = []
    if needs_parallel or execution_mode in {"audit", "full", "refactor", "migration"}:
        subagents.extend(["ultracode_mapper", "ultracode_test_mapper", "ultracode_doc_mapper", "ultracode_reviewer"])
    if impl or migration or refactor:
        subagents.append("ultracode_worker")
    if verification:
        subagents.append("ultracode_verifier")
    if adversarial:
        subagents.extend(["ultracode_claim_checker", "ultracode_edge_tester", "ultracode_adversary"])
    subagents = sorted(set(subagents), key=subagents.index)

    capabilities = {
        "needs_repo_mapping": needs_parallel or execution_mode in {"audit", "full", "refactor", "migration"},
        "needs_doc_mapping": doc_count > 0 or config_count > 0 or plan_only or refactor or package_generation,
        "needs_test_mapping": bool(commands) or impl or migration or refactor or verification,
        "needs_parallel_subagents": needs_parallel,
        "needs_implementation": impl or migration or refactor,
        "needs_verification": verification,
        "needs_adversarial_gate": adversarial,
        "needs_claim_checking": adversarial,
        "needs_edge_testing": adversarial and (impl or migration or refactor or install_flow or package_generation or changed_count > 0),
        "needs_install_flow_check": install_flow,
        "needs_research": research,
        "needs_final_ledger": True,
        "plan_only": plan_only,
        "max_workers": min(max_workers, 16),
        "recommended_subagents": subagents,
    }
    if execution_mode == "lightweight":
        capabilities["needs_parallel_subagents"] = False
        capabilities["recommended_subagents"] = [a for a in subagents if a in {"ultracode_verifier", "ultracode_claim_checker", "ultracode_adversary"}]

    return {
        "execution_mode": execution_mode,
        "capabilities": capabilities,
        "signals": signals,
        "workspace_stats": {
            "file_count": len(files),
            "code_file_count": code_count,
            "doc_file_count": doc_count,
            "config_file_count": config_count,
            "top_suffixes": dict(suffixes.most_common(12)),
            "large_repo": large_repo,
            "medium_repo": medium_repo,
        },
        "verification_commands": commands,
        "git": git,
    }


def write_markdown(route: dict[str, Any], out: Path) -> None:
    caps = route["capabilities"]
    lines = [
        "# Ultracode Route",
        "",
        "> This is a heuristic artifact. The active Codex model must read it and make the final route decision before editing or spawning subagents.",
        "",
        f"- execution_mode: `{route['execution_mode']}`",
        f"- max_workers: `{caps['max_workers']}`",
        f"- recommended_subagents: `{', '.join(caps['recommended_subagents']) or 'none'}`",
        "",
        "## Capability flags",
        "",
    ]
    for key, value in caps.items():
        if key == "recommended_subagents":
            continue
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Signals", ""])
    hits = route["signals"]["hits"]
    if hits:
        for key, vals in hits.items():
            lines.append(f"- `{key}`: {', '.join(map(repr, vals))}")
    else:
        lines.append("- No strong keyword signals detected; choose a lightweight route unless the repository evidence says otherwise.")
    lines.extend(["", "## Workspace stats", ""])
    for key, value in route["workspace_stats"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Verification candidates", ""])
    if route["verification_commands"]:
        for cmd in route["verification_commands"]:
            lines.append(f"- `{cmd['command']}` — {cmd['reason']}")
    else:
        lines.append("- No obvious project-level verification command detected.")
    lines.extend(["", "## Current-model decision checklist", ""])
    lines.extend([
        "1. Confirm or override `execution_mode`.",
        "2. Confirm whether subagents are justified; do not spawn them for trivial tasks.",
        "3. Confirm whether edits are allowed by the user's prompt.",
        "4. Confirm the minimum verification and adversarial gate needed before final answer.",
        "5. Continue to `uc_bootstrap.py` using the selected mode and max worker count.",
    ])
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route a $ultracode task before workflow execution.")
    parser.add_argument("--workspace", default=".", help="Workspace/repo path.")
    parser.add_argument("--task", required=True, help="User task after removing the $ultracode token.")
    parser.add_argument("--out-dir", default=None, help="Optional output directory. Default: .codex/ultracode/runs/<route-id>.")
    parser.add_argument("--max-files", type=int, default=25000, help="Maximum files to scan.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    root = detect_root(workspace)
    files = list_files(root, args.max_files)
    commands = detect_commands(root)
    git = git_info(root)
    route = infer_route(args.task, files, root, commands, git)
    route_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-route")
    out_dir = Path(args.out_dir).resolve() if args.out_dir else root / ".codex" / "ultracode" / "runs" / route_id
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        _probe = out_dir / ".uc_write_probe"
        _probe.write_text("ok", encoding="utf-8")
        _probe.unlink()
    except OSError as exc:
        print(json.dumps({
            "ok": False,
            "error": "run-dir-not-writable",
            "path": str(out_dir),
            "hint": "The run directory is not writable (often a read-only sandbox). Re-run with escalated/approved file permissions, or pass a writable --out-dir.",
            "detail": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False))
        return 3
    route.update({
        "ok": True,
        "route_id": route_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(root),
        "task": args.task,
        "out_dir": str(out_dir),
        "activation": "$ultracode",
        "note": "Heuristic only; the active Codex model owns the final route decision.",
    })
    (out_dir / "route.json").write_text(json.dumps(route, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(route, out_dir / "routing.md")
    print(json.dumps({"ok": True, "route_id": route_id, "out_dir": str(out_dir), "execution_mode": route["execution_mode"], "capabilities": route["capabilities"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
