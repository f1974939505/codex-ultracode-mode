#!/usr/bin/env python3
"""Validate the $ultracode Codex skill package and run hook smoke checks."""
from __future__ import annotations

import argparse
import json
import py_compile
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

REQUIRED_SKILL_FILES = [
    "SKILL.md",
    "scripts/uc_route.py",
    "scripts/uc_bootstrap.py",
    "scripts/uc_merge_results.py",
    "scripts/uc_verify.py",
    "scripts/uc_adversarial_verify.py",
    "scripts/uc_hook_router.py",
    "scripts/uc_state.py",
    "scripts/uc_check_package.py",
    "references/research-notes.md",
    "references/worker-output-schema.md",
    "references/prompt-snippets.md",
    "references/adversarial-verification.md",
]
REQUIRED_ROOT_FILES = [
    "hooks/hooks.json",
    "profiles/ultracode-xhigh.config.toml",
    "install_ultracode.sh",
    "install_ultracode.ps1",
    "uninstall_ultracode.sh",
]
REQUIRED_AGENT_KEYS = {"name", "description", "developer_instructions"}


def fail(msg: str) -> None:
    raise AssertionError(msg)


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        fail("SKILL.md missing YAML frontmatter delimiter")
    try:
        _, fm, _body = text.split("---", 2)
    except ValueError:
        fail("SKILL.md frontmatter not closed")
    result: dict[str, str] = {}
    for line in fm.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            fail(f"invalid frontmatter line: {line}")
        k, v = line.split(":", 1)
        result[k.strip()] = v.strip().strip('"\'')
    return result


def check_skill(skill_dir: Path) -> list[str]:
    messages: list[str] = []
    for rel in REQUIRED_SKILL_FILES:
        p = skill_dir / rel
        if not p.exists():
            fail(f"missing skill file: {rel}")
    fm = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    if fm.get("name") != "ultracode":
        fail("SKILL.md name must be ultracode")
    desc = fm.get("description", "").lower()
    if "$ultracode" not in desc or "explicit" not in desc:
        fail("SKILL.md description must require explicit $ultracode invocation")
    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    if "strict-runtime:" in body or "$ultracode-mode" in body:
        fail("SKILL.md still contains legacy activation grammar")
    for script in (skill_dir / "scripts").glob("*.py"):
        py_compile.compile(str(script), doraise=True)
    messages.append(f"skill ok: {skill_dir}")
    return messages


def check_agents(agent_dir: Path) -> list[str]:
    messages: list[str] = []
    agents = sorted(agent_dir.glob("*.toml"))
    if not agents:
        fail(f"no TOML agents found in {agent_dir}")
    for p in agents:
        if tomllib is None:
            text = p.read_text(encoding="utf-8")
            for key in REQUIRED_AGENT_KEYS:
                if f"{key} =" not in text:
                    fail(f"{p.name} missing {key}")
            continue
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        missing = REQUIRED_AGENT_KEYS - set(data)
        if missing:
            fail(f"{p.name} missing keys: {sorted(missing)}")
        if not str(data.get("name", "")).startswith("ultracode_"):
            fail(f"{p.name} agent name must start with ultracode_")
    messages.append(f"agents ok: {len(agents)}")
    return messages


def check_agents_in_sync(dir_a: Path, dir_b: Path) -> list[str]:
    """The package ships agents/ and ultracode/agents/ as duplicates; fail if they drift."""
    names_a = {p.name for p in dir_a.glob("*.toml")}
    names_b = {p.name for p in dir_b.glob("*.toml")}
    if names_a != names_b:
        fail(f"agent set drift between {dir_a} and {dir_b}: only-in-A={sorted(names_a - names_b)} only-in-B={sorted(names_b - names_a)}")
    for name in sorted(names_a):
        if (dir_a / name).read_bytes() != (dir_b / name).read_bytes():
            fail(f"agent content drift: {name} differs between {dir_a} and {dir_b}")
    return [f"agents in sync: {len(names_a)}"]


def run_router(router: Path, payload: dict[str, Any]) -> str:
    proc = subprocess.run([sys.executable, str(router)], input=json.dumps(payload), text=True, capture_output=True, timeout=10)
    if proc.returncode != 0:
        fail(f"hook router failed: {proc.stderr}")
    return proc.stdout


def check_hooks(root: Path, skill_dir: Path) -> list[str]:
    messages: list[str] = []
    hooks_json = root / "hooks" / "hooks.json"
    data = json.loads(hooks_json.read_text(encoding="utf-8"))
    if "hooks" not in data or not isinstance(data["hooks"], dict):
        fail("hooks/hooks.json must contain hooks object")
    for event in ["UserPromptSubmit", "SubagentStart", "PreToolUse", "Stop"]:
        if event not in data["hooks"]:
            fail(f"hooks/hooks.json missing event {event}")
    router = skill_dir / "scripts" / "uc_hook_router.py"

    out = run_router(router, {"hook_event_name": "UserPromptSubmit", "prompt": "$ultracode audit repo", "cwd": str(root)})
    if "additionalContext" not in out or "routing pass" not in out:
        fail(f"$ultracode prompt did not inject routing context: {out}")

    out2 = run_router(router, {"hook_event_name": "UserPromptSubmit", "prompt": "ultracode audit repo", "cwd": str(root)})
    if out2.strip():
        fail(f"bare ultracode prompt should not activate hook: {out2}")

    out3 = run_router(router, {"hook_event_name": "UserPromptSubmit", "prompt": "$ultracode-mode audit repo", "cwd": str(root)})
    if out3.strip():
        fail(f"legacy $ultracode-mode should not activate hook: {out3}")

    cases: list[tuple[str, dict[str, Any], str]] = [
        ("dangerous command", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, "permissionDecision"),
        ("dangerous command (shell tool, argv list)", {"hook_event_name": "PreToolUse", "tool_name": "shell", "tool_input": {"command": ["rm", "-rf", "/"]}}, "permissionDecision"),
        ("subagent", {"hook_event_name": "SubagentStart", "agent_type": "ultracode_mapper"}, "additionalContext"),
    ]
    for label, payload, needle in cases:
        out = run_router(router, payload)
        if needle not in out:
            fail(f"hook router missing {needle} for {label}: {out}")

    # A safe command inside a quoted string must NOT be blocked (false-positive guard).
    safe_out = run_router(router, {"hook_event_name": "PreToolUse", "tool_name": "shell", "tool_input": {"command": 'git commit -m "remove rm -rf / from docs"'}})
    if "permissionDecision" in safe_out:
        fail(f"hook router false-positive on quoted text: {safe_out}")

    # Stop gate: a completion claim WITHOUT durable artifacts must be blocked on the
    # first attempt, and the escape valve must release on the next (no infinite loop).
    import tempfile
    with tempfile.TemporaryDirectory(prefix="uc-stop-check-") as td:
        stop_payload = {"hook_event_name": "Stop", "last_assistant_message": "Ultracode task completed; adversarial gate passed.", "cwd": td, "stop_hook_active": False}
        s1 = run_router(router, stop_payload)
        if '"block"' not in s1:
            fail(f"stop gate should block an unbacked completion claim: {s1}")
        s2 = run_router(router, stop_payload)
        if '"block"' in s2:
            fail(f"stop gate escape valve should release on the second attempt: {s2}")
        s3 = run_router(router, {**stop_payload, "stop_hook_active": True})
        if '"block"' in s3:
            fail(f"stop gate must honor host stop_hook_active loop guard: {s3}")
    messages.append("hooks ok")
    return messages


def check_route(skill_dir: Path, root: Path) -> list[str]:
    route_script = skill_dir / "scripts" / "uc_route.py"
    out_dir = root / ".tmp-uc-route-check"
    if out_dir.exists():
        import shutil
        shutil.rmtree(out_dir)
    proc = subprocess.run([
        sys.executable, str(route_script),
        "--workspace", str(root / "tests" / "sample_repo"),
        "--task", "check install script details and adversarial verification",
        "--out-dir", str(out_dir),
    ], text=True, capture_output=True, timeout=30)
    if proc.returncode != 0:
        fail(f"uc_route.py failed: {proc.stderr}")
    data = json.loads(proc.stdout)
    if not data.get("ok") or not (out_dir / "route.json").exists() or not (out_dir / "routing.md").exists():
        fail("uc_route.py did not create expected artifacts")
    import shutil
    shutil.rmtree(out_dir)
    return ["route ok"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate $ultracode Codex skill package.")
    parser.add_argument("--package-root", default=".", help="Package root containing ultracode/, agents/, hooks/.")
    args = parser.parse_args(argv)
    root = Path(args.package_root).resolve()
    skill_dir = root / "ultracode"
    if not root.exists():
        print(f"package root does not exist: {root}", file=sys.stderr)
        return 2
    try:
        messages: list[str] = []
        for rel in REQUIRED_ROOT_FILES:
            if not (root / rel).exists():
                fail(f"missing root file: {rel}")
        messages.extend(check_skill(skill_dir))
        messages.extend(check_agents(root / "agents"))
        messages.extend(check_agents(skill_dir / "agents"))
        messages.extend(check_agents_in_sync(root / "agents", skill_dir / "agents"))
        messages.extend(check_hooks(root, skill_dir))
        messages.extend(check_route(skill_dir, root))
        print(json.dumps({"ok": True, "messages": messages}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
