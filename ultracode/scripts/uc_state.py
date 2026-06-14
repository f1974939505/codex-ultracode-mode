#!/usr/bin/env python3
"""Toggle or inspect Ultracode session/workspace state for hooks.

This is optional. Codex skill invocation works without state. State is useful when
the user says "ultracode on" and wants future substantive prompts to receive the
Ultracode hook context automatically.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def state_path(workspace: Path, global_state: bool) -> Path:
    if global_state:
        return Path.home() / ".codex" / "ultracode" / "state.json"
    # Project state lives under .ultracode (writable), not .codex (read-only in the
    # Codex workspace-write sandbox).
    return workspace.resolve() / ".ultracode" / "state.json"


def load(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toggle Ultracode hook state.")
    parser.add_argument("action", choices=["on", "off", "status"], help="State action.")
    parser.add_argument("--workspace", default=".", help="Workspace root for local state.")
    parser.add_argument("--global-state", action="store_true", help="Use ~/.codex/ultracode/state.json instead of workspace state.")
    args = parser.parse_args(argv)
    path = state_path(Path(args.workspace), args.global_state)
    data = load(path)
    if args.action in {"on", "off"}:
        data["ultracode_on"] = args.action == "on"
        data["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        data["scope"] = "global" if args.global_state else "workspace"
        save(path, data)
    out = {"ok": True, "path": str(path), "state": load(path)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
