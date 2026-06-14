#!/usr/bin/env python3
"""Optional Codex hook router for Ultracode.

This script reads one hook JSON object from stdin and writes hook-compatible JSON to stdout.
It is designed to be safe and dependency-free.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

SHELL_OPERATORS = {"|", "||", "&&", ";", "&", "|&", "\n"}
DANGEROUS_TARGETS = {"/", "~", "$HOME", "${HOME}", "*", "/*"}
DOWNLOADERS = {"curl", "wget", "fetch"}
SHELL_INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
POWER_COMMANDS = {"shutdown", "reboot", "halt", "poweroff", "init"}

# Path-independent dangers checked against the raw command string. These are rare
# enough inside quoted text that the small false-positive risk is acceptable.
RAW_DANGEROUS_PATTERNS = [
    (r"\bmkfs(\.[a-z0-9]+)?\b", "filesystem format (mkfs)"),
    (r"\bdd\b[^\n]*\bof=/dev/", "dd writing directly to a device"),
    (r">\s*/dev/(sd|nvme|hd|vd|disk|mmcblk)", "redirect to a raw disk device"),
    (r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:&?\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(curl|wget|fetch)\b[^\n]*\|\s*(sudo\s+)?(sh|bash|zsh|dash|ksh|python[0-9.]*)\b", "pipe download into a shell interpreter"),
]

ULTRACODE_EXPLICIT_RE = re.compile(r"(?<![\w-])\$ultracode(?![\w-])", re.IGNORECASE)

# Maximum times the Stop hook will block a single unsatisfied completion before
# giving up. This is the escape valve for hosts that do not send stop_hook_active.
MAX_STOP_NUDGES = 1


def emit(obj: dict[str, Any]) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return 0


def additional_context(event: str, text: str) -> dict[str, Any]:
    # Verified against Codex's embedded hook-output JSON schema (0.136): the
    # <event>.command.output wire is additionalProperties:false, so `additionalContext`
    # must sit INSIDE `hookSpecificOutput` with the matching `hookEventName` const —
    # NOT at the top level. A stray top-level key is rejected as invalid hook output.
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        },
    }


def deny_pretool(reason: str) -> dict[str, Any]:
    # PreToolUseHookSpecificOutputWire (additionalProperties:false): permissionDecision
    # ("allow"|"deny"|"ask") and permissionDecisionReason go INSIDE hookSpecificOutput.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def get_command(event: dict[str, Any]) -> str:
    """Extract a shell command from a tool-call event across Codex/Claude shapes.

    Handles tool_input.command as a string OR an argv list, the {cmd} alias, and a
    nested {action:{command}} wrapper.
    """
    tool_input = event.get("tool_input")
    if tool_input is None:
        tool_input = event.get("input") or {}
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return ""
    sources: list[Any] = [tool_input.get("command"), tool_input.get("cmd")]
    action = tool_input.get("action")
    if isinstance(action, dict):
        sources.append(action.get("command"))
    for value in sources:
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            return " ".join(str(v) for v in value)
    return ""


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in SHELL_OPERATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _has_dangerous_target(args: list[str]) -> bool:
    for arg in args:
        a = arg.strip("'\"")
        if a in DANGEROUS_TARGETS or a.startswith("~"):
            return True
        # A top-level absolute path such as / or /etc (exactly one slash).
        if a.startswith("/") and a.rstrip("/").count("/") <= 1:
            return True
    return False


def _flags(args: list[str]) -> tuple[str, set[str]]:
    short = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    longs = {a for a in args if a.startswith("--")}
    return short, longs


def is_dangerous_command(command: str) -> str | None:
    """Return a human reason if the command looks destructive, else None.

    Quote-aware: tokenizes with shlex so `git commit -m "rm -rf /"` is NOT flagged,
    while `rm -rf /`, `rm -fr ~`, `rm -r -f /*`, and `sudo rm --recursive --force /`
    are. rm/chmod/chown are gated on a root/home/glob target to avoid flagging safe
    recursive ops on project paths (e.g. `chmod -R 777 /tmp/mine`).
    """
    if not command or not command.strip():
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()

    for seg in _segments(tokens):
        if not seg:
            continue
        head, rest = seg[0], seg[1:]
        if head == "sudo" and rest:
            head, rest = rest[0], rest[1:]
        base = os.path.basename(head)
        targets = [a for a in rest if not a.startswith("-")]
        short, longs = _flags(rest)
        recursive = "r" in short.lower() or "--recursive" in longs
        force = "f" in short or "--force" in longs
        if base == "rm" and recursive and force and _has_dangerous_target(targets):
            return "recursive force rm on a root/home/glob target"
        if base == "chmod" and ("R" in short or "--recursive" in longs) and _has_dangerous_target(targets):
            return "recursive chmod on a root/home path"
        if base == "chown" and ("R" in short or "--recursive" in longs) and _has_dangerous_target(targets):
            return "recursive chown on a root/home path"
        if base in POWER_COMMANDS:
            return f"system power-state change ({base})"
        if base == "mkfs" or base.startswith("mkfs."):
            return "filesystem format (mkfs)"
        if base == "find" and "-delete" in rest and _has_dangerous_target(targets):
            return "find -delete on a root/home path"

    # Cross-segment: a downloader piped into a shell interpreter.
    segs = _segments(tokens)
    heads = [os.path.basename(s[0]) if s else "" for s in segs]
    if any(h in DOWNLOADERS for h in heads) and any(
        os.path.basename(h) in SHELL_INTERPRETERS or os.path.basename(h).startswith("python") for h in heads
    ):
        return "pipe download into a shell interpreter"

    for pat, reason in RAW_DANGEROUS_PATTERNS:
        if re.search(pat, command, flags=re.IGNORECASE):
            return reason
    return None


def load_state(cwd: str) -> dict[str, Any]:
    paths = []
    if cwd:
        paths.append(Path(cwd) / ".codex" / "ultracode" / "state.json")
    home = os.environ.get("HOME")
    if home:
        paths.append(Path(home) / ".codex" / "ultracode" / "state.json")
    for p in paths:
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def handle_user_prompt(event: dict[str, Any]) -> int:
    prompt = str(event.get("prompt", ""))
    active = bool(ULTRACODE_EXPLICIT_RE.search(prompt))
    if not active:
        return 0
    text = (
        "$ultracode hook context: the skill is explicitly active. First strip the literal `$ultracode` token "
        "from the user prompt, then let the current Codex model perform a routing pass before edits or subagents. "
        "Run `uc_route.py` to collect signals, read `routing.md`/`route.json`, choose the needed capabilities, "
        "then bootstrap `.codex/ultracode/runs/<run-id>/`. Do not require suffixes such as strict-runtime, "
        "adversarial, or verify; route those functions automatically. For nontrivial work, finish with verification, "
        "adversarial gate status, and a final ledger. Do not claim native Claude workflow runtime."
    )
    return emit(additional_context("UserPromptSubmit", text))


def handle_subagent_start(event: dict[str, Any]) -> int:
    agent_type = str(event.get("agent_type", ""))
    if "ultracode" not in agent_type:
        return 0
    text = (
        "Ultracode subagent context: stay within assigned scope; return one structured result with "
        "id, status, summary, evidence, findings, changes, verification, recommendations, open_questions. "
        "Cite files/symbols/commands. Mark uncertainty as needs-confirmation. "
        "If this is an adversarial/claim/edge worker, try to falsify the result and check small details."
    )
    return emit(additional_context("SubagentStart", text))


def handle_pretool(event: dict[str, Any]) -> int:
    # Do not gate on a specific tool_name: Codex's shell tool may be reported as
    # "shell"/"local_shell"/"exec" rather than "Bash". Instead, try to extract a
    # command from any tool call and only act when one is present.
    command = get_command(event)
    if not command:
        return 0
    reason = is_dangerous_command(command)
    if reason:
        return emit(deny_pretool(f"Ultracode safety hook blocked a potentially destructive command: {reason}."))
    return 0


def _event_cwd(event: dict[str, Any]) -> str:
    cwd = event.get("cwd")
    return str(cwd) if cwd else os.getcwd()


def _latest_run_dir(cwd: str) -> Path | None:
    base = Path(cwd) / ".codex" / "ultracode" / "runs"
    if not base.exists():
        return None
    runs = sorted(p for p in base.glob("*") if p.is_dir())
    return runs[-1] if runs else None


def _nonempty(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except Exception:
        return False


def _artifacts_present(run_dir: Path | None) -> bool:
    if not run_dir:
        return False
    return _nonempty(run_dir / "verification.json") and _nonempty(run_dir / "adversarial_verification.json")


def _gate_status(run_dir: Path | None) -> str | None:
    if not run_dir:
        return None
    try:
        data = json.loads((run_dir / "adversarial_verification.json").read_text(encoding="utf-8"))
        gate = data.get("gate") if isinstance(data, dict) else None
        return str(gate.get("status")) if isinstance(gate, dict) else None
    except Exception:
        return None


def _verification_skipped(run_dir: Path | None) -> bool:
    """A read-only task (no code changed) or an environment where the verify scripts
    cannot run can satisfy the completion gate by recording a DURABLE, explicit
    justification — a non-empty verification_skip.json (or an ULTRACODE-VERIFICATION-SKIP
    marker in ledger.md) — instead of executable verification/adversarial artifacts.
    This prevents the gate from forcing such runs back into scripts that would hang."""
    if not run_dir:
        return False
    if _nonempty(run_dir / "verification_skip.json"):
        return True
    try:
        return "ULTRACODE-VERIFICATION-SKIP" in (run_dir / "ledger.md").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False


def _stop_nudge_file(cwd: str) -> Path:
    return Path(cwd) / ".codex" / "ultracode" / "stop_nudges.json"


def _bump_stop_nudges(cwd: str) -> int:
    p = _stop_nudge_file(cwd)
    count = 0
    try:
        if p.exists():
            count = int(json.loads(p.read_text(encoding="utf-8")).get("count", 0))
    except Exception:
        count = 0
    count += 1
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"count": count}), encoding="utf-8")
    except Exception:
        pass
    return count


def _reset_stop_nudges(cwd: str) -> None:
    try:
        p = _stop_nudge_file(cwd)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def handle_stop(event: dict[str, Any]) -> int:
    # Respect the host's own loop guard when it provides one.
    if event.get("stop_hook_active"):
        return emit({"continue": True})
    msg = str(event.get("last_assistant_message", ""))
    if not msg:
        return emit({"continue": True})
    lower = msg.lower()
    claims_done = any(x in lower for x in ["done", "completed", "已完成", "完成了", "完成"])
    mentions_ultracode = any(x in lower for x in ["ultracode", "$ultracode", "adversarial gate", "adversarial_verification", "final ledger", "最终账本"])
    if not (claims_done and mentions_ultracode):
        return emit({"continue": True})

    # Verify the completion claim against DURABLE artifacts, not just wording.
    cwd = _event_cwd(event)
    run_dir = _latest_run_dir(cwd)
    # A read-only / unsupported-environment run may record a durable skip justification.
    if _verification_skipped(run_dir):
        _reset_stop_nudges(cwd)
        return emit({"continue": True})
    if not _artifacts_present(run_dir):
        problem = ("no non-empty verification.json + adversarial_verification.json were found under "
                   ".codex/ultracode/runs/. If this task changed code, run uc_verify.py --execute and "
                   "uc_adversarial_verify.py. If it is read-only (no code changed) or those scripts cannot "
                   "run in this environment, write a non-empty verification_skip.json (with a \"reason\" and "
                   "what was checked instead) into the run dir to record why — that satisfies this gate")
    elif _gate_status(run_dir) == "fail":
        problem = ("the adversarial gate status is `fail`; resolve the failing findings, or record why they "
                   "are accepted/not applicable in verification_skip.json")
    else:
        _reset_stop_nudges(cwd)
        return emit({"continue": True})

    # Escape valve: never block more than MAX_STOP_NUDGES times for one run, even
    # if the host never sets stop_hook_active.
    if _bump_stop_nudges(cwd) > MAX_STOP_NUDGES:
        return emit({"continue": True})
    return emit({
        "decision": "block",
        "reason": (f"Ultracode continuation required: {problem}. Then include the verification and "
                   "adversarial gate status before stopping."),
    })


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw or "{}")
    except Exception as exc:  # noqa: BLE001
        print(f"uc_hook_router: invalid hook input: {exc}", file=sys.stderr)
        return 1
    event_name = str(event.get("hook_event_name", ""))
    if event_name == "UserPromptSubmit":
        return handle_user_prompt(event)
    if event_name == "SubagentStart":
        return handle_subagent_start(event)
    if event_name == "PreToolUse":
        return handle_pretool(event)
    if event_name == "Stop":
        return handle_stop(event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
