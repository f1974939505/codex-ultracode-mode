#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "ultracode"
SAMPLE = ROOT / "tests" / "sample_repo"


def run(cmd, expect: int | None = 0, **kwargs):
    print("$", " ".join(map(str, cmd)), flush=True)
    kwargs.setdefault("timeout", 90)
    proc = subprocess.run(cmd, text=True, capture_output=True, **kwargs)
    print(proc.stdout, flush=True)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, flush=True)
    if expect is not None and proc.returncode != expect:
        raise SystemExit(proc.returncode)
    return proc


def main() -> int:
    run([sys.executable, str(SKILL / "scripts" / "uc_check_package.py"), "--package-root", str(ROOT)])
    with tempfile.TemporaryDirectory(prefix="uc-smoke-") as td:
        route_dir = Path(td) / "route"
        proc_route = run([
            sys.executable, str(SKILL / "scripts" / "uc_route.py"),
            "--workspace", str(SAMPLE),
            "--task", "audit this repo and produce a final ledger",
            "--out-dir", str(route_dir),
        ])
        route_data = json.loads(proc_route.stdout)
        assert route_data["ok"] and (route_dir / "route.json").exists()
        assert "$ultracode" not in (route_dir / "route.json").read_text(encoding="utf-8") or "activation" in (route_dir / "route.json").read_text(encoding="utf-8")

        run_dir = Path(td) / "run"
        proc = run([
            sys.executable, str(SKILL / "scripts" / "uc_bootstrap.py"),
            "--workspace", str(SAMPLE),
            "--task", "audit this repo and produce final ledger",
            "--mode", "auto",
            "--max-workers", "8",
            "--out-dir", str(run_dir),
        ])
        data = json.loads(proc.stdout)
        assert data["ok"] and (run_dir / "work_items.csv").exists()
        assert "adversarial-claim-check" in (run_dir / "work_items.csv").read_text(encoding="utf-8")
        results = run_dir / "results"
        results.mkdir(exist_ok=True)
        shutil.copy2(ROOT / "tests" / "fixtures" / "fake_worker_result.json", results / "architecture-map.json")
        shutil.copy2(ROOT / "tests" / "fixtures" / "fake_adversarial_result.json", results / "adversarial-claim-check.json")
        run([sys.executable, str(SKILL / "scripts" / "uc_merge_results.py"), "--run-dir", str(run_dir)])
        assert (run_dir / "synthesis.md").exists()
        run([sys.executable, str(SKILL / "scripts" / "uc_verify.py"), "--workspace", str(SAMPLE), "--run-dir", str(run_dir), "--execute", "--timeout", "30"])
        assert (run_dir / "verification.json").exists()
        run([sys.executable, str(SKILL / "scripts" / "uc_adversarial_verify.py"), "--workspace", str(SAMPLE), "--run-dir", str(run_dir), "--task", "sample audit", "--strict", "--timeout", "30"], expect=None)
        assert (run_dir / "adversarial_verification.json").exists()
        adv = json.loads((run_dir / "adversarial_verification.json").read_text(encoding="utf-8"))
        assert adv["gate"]["status"] in {"pass", "warn"}
        assert (run_dir / "adversarial_work_items.csv").exists()
        assert (ROOT / "install_ultracode.sh").exists()

        router = SKILL / "scripts" / "uc_hook_router.py"
        hook_payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
        proc2 = subprocess.run([sys.executable, str(router)], input=hook_payload, text=True, capture_output=True, timeout=10)
        print(proc2.stdout, flush=True)
        assert proc2.returncode == 0 and "permissionDecision" in proc2.stdout

        explicit_payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "$ultracode audit repo", "cwd": str(ROOT)})
        proc3 = subprocess.run([sys.executable, str(router)], input=explicit_payload, text=True, capture_output=True, timeout=10)
        print(proc3.stdout, flush=True)
        assert "additionalContext" in proc3.stdout and "routing pass" in proc3.stdout

        bare_payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "ultracode audit repo", "cwd": str(ROOT)})
        proc4 = subprocess.run([sys.executable, str(router)], input=bare_payload, text=True, capture_output=True, timeout=10)
        print(proc4.stdout, flush=True)
        assert proc4.stdout.strip() == ""
    print("SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
