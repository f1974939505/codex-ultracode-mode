#!/usr/bin/env python3
"""Merge Ultracode worker results into synthesis artifacts."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def as_list(value: Any) -> list[Any]:
    """Coerce a worker field to a list. A bare string (e.g. findings="none") must
    NOT be iterated character-by-character, so wrap scalars in a single-item list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_result(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        data = load_json(path)
        if isinstance(data, dict):
            data.setdefault("source_file", str(path))
            return data
        if isinstance(data, list):
            return {"id": path.stem, "status": "ok", "summary": f"{len(data)} JSON list items", "items": data, "source_file": str(path)}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "id": path.stem,
        "status": "ok",
        "summary": text[:1000].strip(),
        "markdown": text,
        "source_file": str(path),
    }


def read_work_items(run_dir: Path) -> dict[str, dict[str, str]]:
    path = run_dir / "work_items.csv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        # Skip rows lacking an id (hand-edited/truncated CSVs) instead of KeyError.
        return {row["id"]: row for row in csv.DictReader(f) if row.get("id")}


def collect_results(run_dir: Path) -> list[dict[str, Any]]:
    results_dir = run_dir / "results"
    files: list[Path] = []
    if results_dir.exists():
        files.extend(sorted(p for p in results_dir.iterdir() if p.is_file() and p.suffix.lower() in {".json", ".md", ".txt"}))
    for pattern in ["*_result.json", "*_result.md", "worker-*.json", "agent-*.json"]:
        files.extend(sorted(run_dir.glob(pattern)))
    seen = set()
    out = []
    for p in files:
        if p.resolve() in seen:
            continue
        seen.add(p.resolve())
        out.append(normalize_result(p))
    return out


def flatten_findings(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for result in results:
        source = result.get("source_file", result.get("id", "unknown"))
        for f in as_list(result.get("findings")):
            if isinstance(f, dict):
                item = dict(f)
            else:
                item = {"severity": "info", "claim": str(f)}
            item["source_result"] = source
            item["work_item_id"] = result.get("id", "unknown")
            findings.append(item)
    return findings


def write_claims_csv(run_dir: Path, findings: list[dict[str, Any]]) -> None:
    path = run_dir / "claims.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["work_item_id", "severity", "claim", "evidence", "source_result"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for finding in findings:
            writer.writerow({
                "work_item_id": finding.get("work_item_id", ""),
                "severity": finding.get("severity", "info"),
                "claim": finding.get("claim", ""),
                "evidence": json.dumps(finding.get("evidence", []), ensure_ascii=False),
                "source_result": finding.get("source_result", ""),
            })


def synthesize(run_dir: Path, results: list[dict[str, Any]], work_items: dict[str, dict[str, str]]) -> str:
    findings = flatten_findings(results)
    status_counts = Counter(str(r.get("status", "unknown")) for r in results)
    severity_counts = Counter(str(f.get("severity", "info")) for f in findings)
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_item[str(result.get("id", "unknown"))].append(result)

    lines = [
        f"# Ultracode synthesis: {run_dir.name}\n\n",
        f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n",
        "## Result coverage\n\n",
        f"- Worker result files merged: {len(results)}\n",
        f"- Status counts: {dict(status_counts)}\n",
        f"- Finding severity counts: {dict(severity_counts)}\n",
        f"- Work items expected: {len(work_items)}\n\n",
    ]
    missing = [wid for wid in work_items if wid not in by_item]
    if missing:
        lines.append("## Missing work-item results\n\n")
        for wid in missing:
            wi = work_items[wid]
            lines.append(f"- `{wid}` / `{wi.get('role','')}` / `{wi.get('path','')}`\n")
        lines.append("\n")

    lines.append("## Worker summaries\n\n")
    for result in results:
        wid = result.get("id", "unknown")
        lines.append(f"### {wid}\n\n")
        lines.append(f"- Status: `{result.get('status', 'unknown')}`\n")
        if result.get("agent"):
            lines.append(f"- Agent: `{result.get('agent')}`\n")
        lines.append(f"- Source: `{result.get('source_file', '')}`\n")
        summary = str(result.get("summary", "")).strip()
        if summary:
            lines.append(f"\n{summary}\n\n")
        open_questions = as_list(result.get("open_questions"))
        if open_questions:
            lines.append("Open questions:\n")
            for q in open_questions:
                lines.append(f"- {q}\n")
            lines.append("\n")

    if findings:
        lines.append("## Consolidated findings\n\n")
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        for f in sorted(findings, key=lambda x: (severity_order.get(str(x.get("severity", "info")), 9), str(x.get("claim", "")))):
            claim = str(f.get("claim", "")).strip()
            if not claim:
                continue
            lines.append(f"- **{f.get('severity', 'info')}** `{f.get('work_item_id','unknown')}`: {claim}\n")
            evidence = f.get("evidence") or []
            if evidence:
                if isinstance(evidence, list):
                    lines.append(f"  - Evidence: {', '.join(map(str, evidence[:5]))}\n")
                else:
                    lines.append(f"  - Evidence: {evidence}\n")
        lines.append("\n")

    lines.append("## Conflict and uncertainty policy\n\n")
    lines.append("- Direct code/test evidence beats commentary.\n")
    lines.append("- Executed command output beats inferred success.\n")
    lines.append("- More local AGENTS.md/config guidance beats broader guidance.\n")
    lines.append("- Unresolved contradictions must remain as `needs-confirmation`; do not delete them.\n")
    return "".join(lines)


def update_ledger(run_dir: Path, synthesis_path: Path) -> None:
    ledger = run_dir / "ledger.md"
    existing = ledger.read_text(encoding="utf-8", errors="replace") if ledger.exists() else f"# Ultracode final ledger: {run_dir.name}\n"
    marker = "\n## Synthesis artifact\n"
    add = f"{marker}\n- Synthesis report: `{synthesis_path.name}`\n- Claims table: `claims.csv`\n"
    if marker in existing:
        existing = existing.split(marker)[0].rstrip() + "\n" + add
    else:
        existing = existing.rstrip() + "\n" + add
    ledger.write_text(existing, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge Ultracode subagent results.")
    parser.add_argument("--run-dir", required=True, help="Ultracode run directory.")
    args = parser.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        print(f"run dir does not exist: {run_dir}", file=sys.stderr)
        return 2
    work_items = read_work_items(run_dir)
    results = collect_results(run_dir)
    findings = flatten_findings(results)
    write_claims_csv(run_dir, findings)
    synthesis = synthesize(run_dir, results, work_items)
    synthesis_path = run_dir / "synthesis.md"
    synthesis_path.write_text(synthesis, encoding="utf-8")
    update_ledger(run_dir, synthesis_path)
    print(json.dumps({"ok": True, "run_dir": str(run_dir), "results": len(results), "findings": len(findings), "synthesis": str(synthesis_path)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
