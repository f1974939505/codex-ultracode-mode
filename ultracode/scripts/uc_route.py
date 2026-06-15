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
    ".git", ".hg", ".svn", ".codex/ultracode", ".ultracode", "node_modules", ".venv", "venv",
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


def extra_ecosystem_commands(root: Path) -> list[dict[str, str]]:
    """Detect a verification command for build systems beyond the core set so routing does
    not treat a Gradle/Maven/.NET/PHP/Ruby/plain-pip/Dart/Elixir/Scala/Swift/Deno/Bun repo
    as having no verifiable checks."""
    cmds: list[dict[str, str]] = []

    def has(*names: str) -> bool:
        return any((root / n).exists() for n in names)

    def glob1(pat: str) -> bool:
        try:
            return next(root.glob(pat), None) is not None
        except Exception:
            return False

    if has("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
        gw = "./gradlew" if (root / "gradlew").exists() else "gradle"
        cmds.append({"kind": "test", "command": f"{gw} test", "reason": "Gradle build present"})
    if has("pom.xml"):
        cmds.append({"kind": "test", "command": "mvn -q -B test", "reason": "pom.xml present"})
    if glob1("*.sln") or glob1("*.csproj") or glob1("*.fsproj") or glob1("*.vbproj"):
        cmds.append({"kind": "test", "command": "dotnet test", "reason": ".NET project present"})
    if has("composer.json"):
        cmds.append({"kind": "test", "command": "composer test", "reason": "composer.json present"})
    if has("Gemfile"):
        if has(".rspec") or (root / "spec").is_dir():
            cmds.append({"kind": "test", "command": "bundle exec rspec", "reason": "Ruby/RSpec project"})
        elif has("Rakefile"):
            cmds.append({"kind": "test", "command": "bundle exec rake test", "reason": "Ruby/Rake project"})
    if has("setup.py", "setup.cfg", "requirements.txt") and not has("pyproject.toml", "pytest.ini", "tox.ini"):
        cmds.append({"kind": "test", "command": "python -m pytest -q", "reason": "Python project via setup/requirements"})
    if has("pubspec.yaml"):
        flutter = "flutter" in safe_read(root / "pubspec.yaml")
        cmds.append({"kind": "test", "command": "flutter test" if flutter else "dart test", "reason": "Dart/Flutter project"})
    if has("mix.exs"):
        cmds.append({"kind": "test", "command": "mix test", "reason": "mix.exs present"})
    if has("build.sbt"):
        cmds.append({"kind": "test", "command": "sbt test", "reason": "build.sbt present"})
    if has("Package.swift"):
        cmds.append({"kind": "test", "command": "swift test", "reason": "Package.swift present"})
    if has("deno.json", "deno.jsonc"):
        cmds.append({"kind": "test", "command": "deno test", "reason": "Deno project"})
    if has("bun.lockb", "bunfig.toml"):
        cmds.append({"kind": "test", "command": "bun test", "reason": "Bun project"})
    return cmds


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
    commands.extend(extra_ecosystem_commands(root))
    # Deduplicate by command, preserving order.
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for cmd in commands:
        if cmd["command"] not in seen:
            seen.add(cmd["command"])
            out.append(cmd)
    return out


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
        "audit": ["audit", "review", "assess", "检查", "审查", "审阅", "审计", "评审", "复核", "code review", "安全", "漏洞", "风险"],
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

    text = task.lower()
    plan_only = "plan_only" in hits
    audit = "audit" in hits
    # Demote weak/hypothetical implementation cues: "实现" in "假设…都已经实现" describes a
    # hoped-for STATE, not a command to change code, and bare "改"/"write"/"update" are loose.
    # Only treat the task as implementation when a strong change verb is present, or when it is
    # neither a hypothetical nor an audit/plan request.
    impl_hits = signals["hits"].get("implementation", [])
    strong_impl = {"fix", "implement", "modify", "patch", "修复", "修改", "补丁"}
    has_strong_impl = any(w in impl_hits for w in strong_impl)
    hypothetical = any(m in text for m in [
        "假设", "假定", "已经实现", "已实现", "若实现", "如果实现", "如果都实现", "assuming", "assume ", "expected to be implemented",
    ])
    impl = ("implementation" in hits) and not plan_only and (has_strong_impl or (not hypothetical and not audit))
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
    elif signals["count"] == 0 and code_count >= 8:
        # No intent keyword matched, but there IS real code. The keyword lists are EN/zh-only
        # and finite, so a miss does NOT mean "trivial" — understeering to lightweight here is
        # exactly how a risky non-English/synonym task got the minimal path. Default to a SAFE
        # read-only audit and let the model (the authoritative router) decide from the prompt.
        execution_mode = "audit"
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

    # read_only is the single downstream signal that THIS run will not change code: no
    # implementation/migration/refactor was routed. Consumers (uc_bootstrap run.json,
    # uc_verify --read-only, the Stop hook) use it so a no-change audit is never forced
    # through change-oriented verification/adversarial gating. It is independent of
    # whether a pre-existing diff exists — the adversarial gate reviews any diff regardless.
    read_only = not (impl or migration or refactor)
    capabilities = {
        "needs_repo_mapping": needs_parallel or execution_mode in {"audit", "full", "refactor", "migration"},
        "needs_doc_mapping": doc_count > 0 or config_count > 0 or plan_only or refactor or package_generation,
        "needs_test_mapping": bool(commands) or impl or migration or refactor or verification,
        "needs_parallel_subagents": needs_parallel,
        "needs_implementation": impl or migration or refactor,
        "read_only": read_only,
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

    signal_count = signals["count"]
    return {
        "execution_mode": execution_mode,
        "capabilities": capabilities,
        # The deterministic keyword classifier is a HINT, not the decision. The active model
        # is the authoritative router: it must classify intent from the full prompt itself
        # (any language, synonyms, multi-part asks) and override execution_mode/capabilities,
        # and must NOT downgrade to lightweight just because no keyword matched.
        "route_authority": "active-model",
        "route_confidence": "low" if signal_count == 0 else ("medium" if signal_count <= 1 else "high"),
        "signal_coverage": "keyword hint, EN/zh substrings only — not authoritative; other languages/synonyms will not match",
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
        "## Routing authority",
        "",
        f"- route_authority: `{route.get('route_authority', 'active-model')}` — **you (the active model) are the router.**",
        f"- route_confidence (keyword heuristic): `{route.get('route_confidence', 'unknown')}`",
        f"- signal_coverage: {route.get('signal_coverage', '')}",
        "- Classify the task's intent from the FULL prompt yourself — any language, synonyms, or multi-part asks. The keyword `execution_mode` above is only a hint; override it freely. In particular, do NOT fall back to `lightweight` just because no keyword matched a real codebase; prefer at least a read-only `audit`.",
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
    parser.add_argument("--out-dir", default=None, help="Optional output directory. Default: .ultracode/runs/<route-id>.")
    parser.add_argument("--max-files", type=int, default=25000, help="Maximum files to scan.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    root = detect_root(workspace)
    files = list_files(root, args.max_files)
    commands = detect_commands(root)
    git = git_info(root)
    route = infer_route(args.task, files, root, commands, git)
    route_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-route")
    out_dir, err = prepare_run_dir(root, args.out_dir, route_id)
    if err:
        print(json.dumps(err, ensure_ascii=False))
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
