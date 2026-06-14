# Ultracode adversarial verification guide

Adversarial verification is a falsification pass. It is designed for the failure mode where an agent completes the broad task but misses small details: wrong path names, wrong install flags, stale README instructions, false test claims, silent exception handling, or an edge case not covered by tests.

## Required artifacts

A strict Ultracode run should contain:

```text
.ultracode/runs/<run-id>/
├── verification.json
├── verification.md
├── adversarial_verification.json
├── adversarial_verification.md
├── adversarial_work_items.csv
├── adversarial_spawn_prompt.md
├── synthesis.md
├── claims.csv
└── ledger.md
```

## Deterministic gate

Run this after ordinary verification:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_adversarial_verify.py \
  --workspace . \
  --run-dir .ultracode/runs/<run-id> \
  --task "<user task>" \
  --strict
```

Use `--execute --strict` when it is safe to execute project checks inside the same gate.

The gate scans:

- git diff and changed files;
- risky patterns such as broad deletes, silent exception handling, disabled tests, placeholders, weak assertions, pipe-to-shell installs;
- source changes without obvious tests;
- missing or non-executed `verification.json`;
- missing adversarial subagent results in strict mode;
- completion claims in ledger/synthesis/claim files that need evidence.

The gate is intentionally conservative. A `warn` or `fail` does not automatically prove the patch is wrong; it says the final completion claim is not yet sufficiently supported.

## Subagent roles

Use the generated `adversarial_work_items.csv` to spawn workers:

- `ultracode_claim_checker`: extract material claims and classify each as supported, unsupported, contradicted, or needs-confirmation.
- `ultracode_edge_tester`: design minimal counterexamples and run safe probes in a temp/run directory.
- `ultracode_adversary`: check exact filenames, CLI flags, installation flow, docs, public APIs, generated files, and compatibility.
- `ultracode_verifier`: judge whether the verification suite is adequate for the task.

## Final-answer discipline

The final answer must not collapse these statuses:

- `verified`: command or test actually ran and passed;
- `detected`: command exists but was not run;
- `inferred`: likely from code inspection but not executed;
- `needs-confirmation`: evidence is insufficient or contradictory;
- `unresolved`: known issue not fixed.

For small-detail reliability, the final response should include the adversarial gate status and any unresolved high/medium findings.
