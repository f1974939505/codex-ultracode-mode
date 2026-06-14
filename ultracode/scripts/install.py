#!/usr/bin/env python3
"""Install the `$ultracode` Codex skill package.

Codex-only layout (everything under .codex/, never .agents/). Overwrites in place;
no `.bak` backups are created. Default user install used by install_ultracode.sh:
  ~/.codex/skills/ultracode               # skill (Codex reads $CODEX_HOME/skills)
  ~/.codex/agents/ultracode_*.toml        # custom agent profiles
  ~/.codex/hooks.json                     # merged hooks (ultracode groups replaced; others kept)
  ~/.codex/ultracode-xhigh.config.toml    # standalone profile (select via --profile ultracode-xhigh)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OLD_SKILL_NAME = "ultracode-mode"
NEW_SKILL_NAME = "ultracode"


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_or_remove(path: Path, dry_run: bool, force: bool = True) -> None:
    # Install overwrites in place; no `.bak` backups are created. The `force`
    # parameter is retained for call-site compatibility and is ignored.
    if not path.exists() and not path.is_symlink():
        return
    print(f"overwrite: {path}")
    if dry_run:
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def copytree_replace(src: Path, dst: Path, dry_run: bool, force: bool) -> None:
    if not src.exists():
        raise FileNotFoundError(src)
    backup_or_remove(dst, dry_run, force)
    print(f"copy: {src} -> {dst}")
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Do not ship compiled bytecode caches into the user's install.
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))


def copy_files(src_dir: Path, dst_dir: Path, pattern: str, dry_run: bool, force: bool) -> None:
    if not src_dir.exists():
        return
    if not dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(src_dir.glob(pattern)):
        dst = dst_dir / src.name
        backup_or_remove(dst, dry_run, force)
        print(f"copy: {src} -> {dst}")
        if not dry_run:
            shutil.copy2(src, dst)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Existing {path} is not valid JSON. Fix or move it before installing hooks. Error: {exc}") from exc


def render_hooks(src: Path, skill_dest: Path) -> dict[str, Any]:
    text = src.read_text(encoding="utf-8")
    command = f"python3 \"{skill_dest / 'scripts' / 'uc_hook_router.py'}\""
    # Accept both the packaged default and legacy v2 paths as replaceable placeholders.
    placeholders = [
        'python3 \\\"$HOME/.agents/skills/ultracode/scripts/uc_hook_router.py\\\"',
        'python3 "$HOME/.agents/skills/ultracode/scripts/uc_hook_router.py"',
        'python3 \\\"$HOME/.codex/skills/ultracode/scripts/uc_hook_router.py\\\"',
        'python3 "$HOME/.codex/skills/ultracode/scripts/uc_hook_router.py"',
        'python3 \\\"$HOME/.codex/skills/ultracode-mode/scripts/uc_hook_router.py\\\"',
        'python3 "$HOME/.codex/skills/ultracode-mode/scripts/uc_hook_router.py"',
    ]
    for ph in placeholders:
        text = text.replace(ph, command.replace('"', '\\"') if '\\\"' in ph else command)
    return json.loads(text)


def is_legacy_ultracode_hook(group: Any) -> bool:
    s = json.dumps(group, sort_keys=True)
    return f"/skills/{OLD_SKILL_NAME}/scripts/uc_hook_router.py" in s or f"\\/skills\\/{OLD_SKILL_NAME}\\/scripts\\/uc_hook_router.py" in s


def merge_hooks(existing: dict[str, Any], incoming: dict[str, Any], prune_legacy: bool) -> dict[str, Any]:
    existing_hooks = existing.setdefault("hooks", {})
    if prune_legacy:
        for event, groups in list(existing_hooks.items()):
            if isinstance(groups, list):
                existing_hooks[event] = [g for g in groups if not is_legacy_ultracode_hook(g)]
    for event, groups in incoming.get("hooks", {}).items():
        existing_groups = existing_hooks.setdefault(event, [])
        existing_serial = {json.dumps(g, sort_keys=True) for g in existing_groups}
        for group in groups:
            s = json.dumps(group, sort_keys=True)
            if s not in existing_serial:
                existing_groups.append(group)
                existing_serial.add(s)
    return existing


def install_hooks(package_root: Path, hooks_dest: Path, skill_dest: Path, dry_run: bool, force: bool = True, prune_legacy: bool = True) -> None:
    # Always remove any prior ultracode hook groups (current + legacy) before adding
    # the fresh ones, so re-install is idempotent and never duplicates. Unrelated
    # user hooks are preserved. No backup file is written (overwrite in place).
    incoming = render_hooks(package_root / "hooks" / "hooks.json", skill_dest)
    existing = load_json(hooks_dest)
    hooks = existing.get("hooks")
    if isinstance(hooks, dict):
        for event, groups in list(hooks.items()):
            if isinstance(groups, list):
                kept = [g for g in groups if not references_ultracode_hook(g)]
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
    merged = merge_hooks(existing, incoming, prune_legacy=False)
    print(f"write hooks: {hooks_dest}")
    if not dry_run:
        hooks_dest.parent.mkdir(parents=True, exist_ok=True)
        hooks_dest.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def archive_old_skill_names(skill_locations: list[Path], dry_run: bool, force: bool) -> None:
    for loc in skill_locations:
        old = loc.parent / OLD_SKILL_NAME
        if old.exists():
            backup_or_remove(old, dry_run, force)


def references_ultracode_hook(group: Any) -> bool:
    """True if a hook group invokes either the current or legacy ultracode hook router."""
    s = json.dumps(group, sort_keys=True)
    for name in (NEW_SKILL_NAME, OLD_SKILL_NAME):
        needle = f"/skills/{name}/scripts/uc_hook_router.py"
        if needle in s or needle.replace("/", "\\/") in s:
            return True
    return False


def prune_ultracode_hooks(hooks_dest: Path, dry_run: bool) -> int:
    """Remove all ultracode hook groups from hooks.json; drop now-empty events. Returns count removed."""
    if not hooks_dest.exists():
        return 0
    data = load_json(hooks_dest)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups if not references_ultracode_hook(g)]
        removed += len(groups) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    print(f"prune hooks: {removed} ultracode group(s) in {hooks_dest}")
    if removed and not dry_run:
        hooks_dest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return removed


def remove_path(path: Path, dry_run: bool) -> None:
    if not path.exists() and not path.is_symlink():
        return
    print(f"remove: {path}")
    if dry_run:
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def run_uninstall(scope: str, project_root: str, dry_run: bool) -> int:
    if scope == "user":
        agents_skill_base = Path.home() / ".agents" / "skills"
        codex_base = Path.home() / ".codex"
    else:
        project = Path(project_root).resolve()
        agents_skill_base = project / ".agents" / "skills"
        codex_base = project / ".codex"

    targets = [
        agents_skill_base / NEW_SKILL_NAME,
        codex_base / "skills" / NEW_SKILL_NAME,
        codex_base / "ultracode-xhigh.config.toml",          # current profile location
        codex_base / "profiles" / "ultracode-xhigh.config.toml",  # legacy profile location
    ]
    agents_dir = codex_base / "agents"
    if agents_dir.exists():
        targets.extend(sorted(agents_dir.glob("ultracode_*.toml")))

    for target in targets:
        remove_path(target, dry_run)
    prune_ultracode_hooks(codex_base / "hooks.json", dry_run)

    print("uninstall complete")
    print(f"scope: {scope} ({codex_base})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install $ultracode for Codex.")
    parser.add_argument("--package-root", default=None, help="Root of this package. Default: parent of ultracode/.")
    parser.add_argument("--scope", choices=["user", "project"], default="user", help="Install to user or project locations.")
    parser.add_argument("--project-root", default=".", help="Project root for --scope project.")
    parser.add_argument("--with-hooks", action="store_true", help="Install/merge hooks.json.")
    parser.add_argument("--with-agents", action="store_true", help="Install ultracode custom agent TOML files.")
    parser.add_argument("--with-profile", action="store_true", help="Copy optional ultracode-xhigh config profile snippet.")
    parser.add_argument("--mirror-codex-skill", action="store_true", help="(deprecated, no-op) the skill now installs to .codex/skills directly.")
    parser.add_argument("--archive-old-name", action="store_true", help="Remove previous ultracode-mode skill directories.")
    parser.add_argument("--prune-legacy-hooks", action="store_true", help="(deprecated, no-op) ultracode hook groups are always replaced on install.")
    parser.add_argument("--force", action="store_true", help="(deprecated, no-op) install always overwrites in place without backups.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files.")
    parser.add_argument("--uninstall", action="store_true", help="Remove installed skill, agents, profile, and ultracode hook groups for the chosen scope.")
    args = parser.parse_args(argv)

    if args.uninstall:
        return run_uninstall(args.scope, args.project_root, args.dry_run)

    script_path = Path(__file__).resolve()
    package_root = Path(args.package_root).resolve() if args.package_root else script_path.parents[2]
    skill_src = package_root / NEW_SKILL_NAME
    if not skill_src.exists():
        print(f"Cannot find {NEW_SKILL_NAME}/ under package root: {package_root}", file=sys.stderr)
        return 2

    # Codex-only layout: everything lives under .codex/ (no .agents/).
    if args.scope == "user":
        codex_base = Path.home() / ".codex"
    else:
        codex_base = Path(args.project_root).resolve() / ".codex"

    skill_dest = codex_base / "skills" / NEW_SKILL_NAME

    if args.archive_old_name:
        archive_old_skill_names([skill_dest], args.dry_run, args.force)

    copytree_replace(skill_src, skill_dest, args.dry_run, args.force)

    if args.with_agents:
        copy_files(package_root / "agents", codex_base / "agents", "ultracode_*.toml", args.dry_run, args.force)

    if args.with_hooks:
        install_hooks(package_root, codex_base / "hooks.json", skill_dest, args.dry_run)

    if args.with_profile:
        # Profiles must be standalone "<name>.config.toml" files at the CODEX_HOME
        # root (Codex >= 0.134); a profiles/ subdir is NOT discovered by --profile.
        copy_files(package_root / "profiles", codex_base, "*.config.toml", args.dry_run, args.force)

    print("install complete")
    print(f"skill: {skill_dest}")
    if args.with_profile:
        profile_dest = codex_base / "ultracode-xhigh.config.toml"
        print(f"profile: {profile_dest}")
        if args.scope == "project":
            print("Note: --profile resolves from $CODEX_HOME (default ~/.codex), not a project .codex/.")
            print(f"      To use it here, run with CODEX_HOME={codex_base} or copy it into your real $CODEX_HOME.")
        else:
            print("Select it with: codex --profile ultracode-xhigh")
    if args.with_hooks:
        print("Open Codex and run /hooks to review/trust the new hook definitions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
