---
name: ultracode
description: Explicit-only Codex Ultracode workflow. Use this skill only when the user explicitly invokes `$ultracode`; do not activate implicitly for ordinary refactor, audit, verification, migration, or dynamic-workflow wording without the `$ultracode` skill mention.
---

# `$ultracode` — Dynamic Workflow and Adversarial Verification for Codex

This skill provides a Codex-native approximation of Claude Code's Ultracode/dynamic-workflow discipline. It does not claim that Codex has Claude's exact background JavaScript workflow runtime. It uses Codex capabilities that exist today: explicit skill invocation, the active model's own routing pass, subagents, custom agent roles, CSV fan-out, deterministic helper scripts, optional hooks, verification gates, and a durable final ledger.

## Hard activation rule

Activate this skill only when the user explicitly mentions the skill as `$ultracode`.

Do not activate this skill merely because the user says `ultracode`, `dynamic workflow`, `strict-runtime`, `adversarial`, `audit`, `large refactor`, or similar natural-language phrases. Those words may appear in the user's task, but they are not activation grammar.

Once `$ultracode` is present, the user should not need to add any extra mode suffix. Treat the rest of the user's prompt as the task. Do not ask the user to rewrite it as `ultracode strict-runtime`, `ultracode adversarial`, `ultracode verify`, or any similar command. The workflow must choose the required capabilities automatically.

## Core contract

1. The first substantive step is always a routing pass by the current Codex model, not by a subagent.
2. The router decides which actual capabilities are needed for this task: lightweight direct work, repo mapping, documentation mapping, test mapping, implementation workers, verification, adversarial claim checking, edge-case testing, install-flow checking, research, final ledger, or a full multi-agent workflow.
3. Use deterministic scripts only to collect signals and create artifacts; the current model owns the final route decision.
4. Prefer the active Codex model with `model_reasoning_effort = "xhigh"` when available. Continue with the active model if xhigh is unavailable.
5. Keep subagent depth at 1 unless the user explicitly asks for recursive delegation. Recursive fan-out is high-cost and hard to control.
6. Keep concurrent agents at or below 16. For very large task sets, batch with CSV fan-out rather than opening unbounded parallel threads.
7. Use read-only exploration before editing nontrivial repositories.
8. Record route, plan, work items, evidence, verification, adversarial findings, and unresolved risks under `.ultracode/runs/<run-id>/`.
9. Do not delete uncertain or conflicting information. Mark it as `needs-confirmation` in the ledger.
10. For code changes, preserve public behavior unless the user explicitly asks to change behavior.
11. Do not present a clean completion unless verification and the chosen adversarial gate are consistent with that claim.

## Required workflow

### Phase 0 — Current-model routing

Immediately strip the `$ultracode` token from the user prompt and treat the remaining text as the task.

Run the route helper from whichever installed skill location exists:

- `~/.codex/skills/ultracode` (user scope; `$CODEX_HOME/skills/ultracode`)
- `.codex/skills/ultracode` (project scope)

Preferred command:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_route.py \
  --workspace . \
  --task "<task text after removing $ultracode>"
```

Read the generated `routing.md` and `route.json`. Then the current Codex model must make an explicit routing decision before any edits:

- `lightweight`: small task; use one direct pass plus verification.
- `plan-only`: user asked for a plan, no edits.
- `audit`: read-only mapping and review.
- `implementation`: understand, modify, verify.
- `migration`: broad/path-repeated changes with batched workers.
- `refactor`: architecture/doc/test mapping before edits.
- `adversarial-only`: current patch or answer needs falsification.
- `research`: source cross-checking and cited synthesis.
- `full`: multi-phase understand → modify → verify → adversarial gate.

The route must choose capability flags, not rely on user suffixes:

```json
{
  "needs_repo_mapping": true,
  "needs_doc_mapping": true,
  "needs_test_mapping": true,
  "needs_parallel_subagents": true,
  "needs_implementation": false,
  "needs_verification": true,
  "needs_adversarial_gate": true,
  "needs_claim_checking": true,
  "needs_edge_testing": true,
  "needs_install_flow_check": false,
  "needs_final_ledger": true,
  "max_workers": 8
}
```

If the task is clearly trivial even though `$ultracode` was invoked, still keep the routing artifact and do a compact verify/adversarial sanity pass rather than spawning unnecessary agents.

### Phase 1 — Run directory and bootstrap

Create an Ultracode run directory using the bootstrap script. Use the route decision to set `--mode` and `--max-workers`.

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_bootstrap.py \
  --workspace . \
  --task "<task text after removing $ultracode>" \
  --mode auto \
  --max-workers 16
```

Read the generated:

- `run.json`
- `repo_inventory.json`
- `route.json` if present
- `plan.md`
- `work_items.csv`
- `spawn_agents_prompt.md`
- `ledger.md`

### Phase 2 — Read-only reconnaissance when routed

Use read-only subagents before changing files whenever the route calls for mapping or broad changes. Give each subagent a NON-OVERLAPPING lane so a fan-out of N workers is complementary rather than N near-duplicate audits; each must stay in its lane and cite (not re-derive) another role's territory:

- `ultracode_mapper`: implemented capability via SOURCE CODE only — entry points, data flow, modules, real-vs-vaporware.
- `ultracode_test_mapper`: verification maturity only — test/build/lint/CI commands, eval/QA harnesses, fragile checks. Must not make auth/security claims from app source.
- `ultracode_doc_mapper`: docs/config truth-sources only — AGENTS.md, README, docs/*, config files, generated-file conventions, doc-vs-config drift.
- `ultracode_reviewer`: security, destructive operations, compatibility, data-egress/privacy, IP. This is the role that SCORES the result.

Each subagent must return one structured result. Keep two axes separate: `status` is execution only (did you finish your assigned read?); `verdict` is your assessment (pure mappers use `not-applicable`; only judging roles return `pass`/`concerns`/`fail`). Never put a judgment into `status`.

```json
{
  "id": "<work-item-id>",
  "scope": "<your assigned lane>",
  "status": "ok|blocked|error",
  "verdict": "pass|concerns|fail|not-applicable",
  "summary": "one paragraph",
  "evidence": [
    {"path": "file", "lines": "Lx-Ly or symbol", "claim": "what this proves"}
  ],
  "findings": [
    {"severity": "critical|high|medium|low|info", "claim": "...", "evidence": ["..."]}
  ],
  "recommendations": ["..."],
  "open_questions": ["..."]
}
```

### Phase 3 — Dynamic decomposition

After reconnaissance, rewrite the plan into bounded work packages. Each package should own a small path set or concern. Avoid work items like “fix everything”. Prefer:

- one directory,
- one module boundary,
- one API surface,
- one failing test class,
- one documentation cluster,
- one generated-file or script family.

For many repeated items, use Codex's CSV fan-out pattern with `spawn_agents_on_csv`: `work_items.csv` is the source table, each row becomes one worker job, and each worker must report exactly once with JSON matching the schema in the generated prompt.

### Phase 4 — Parallel work when routed

Spawn subagents explicitly; Codex does not spawn subagents unless asked.

For exploration/audit workers:

- use read-only mode,
- cite files and symbols,
- avoid edits,
- report contradictions.

For implementation workers:

- edit only assigned paths,
- maintain compatibility,
- add or update tests when behavior changes,
- write a concise patch note.

For verification workers:

- rerun relevant checks,
- try to falsify the implementation,
- look for untested edge cases,
- report exact commands and outputs.

### Phase 5 — Synthesis

Merge worker results. Use the merge script when workers wrote result JSON/Markdown into the run directory:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_merge_results.py \
  --run-dir .ultracode/runs/<run-id>
```

Resolve conflicts by evidence quality:

1. Direct code/test evidence beats commentary.
2. Executed command output beats inferred command success.
3. More local AGENTS.md or config beats broader guidance.
4. Newer generated run artifacts beat stale notes.
5. If still uncertain, preserve both views and mark `needs-confirmation`.

### Phase 6 — Implementation when routed

Before editing, state the selected implementation strategy. Apply changes in small patches. Do not mix cleanup with behavioral changes unless the task is explicitly a refactor or cleanup.

When changing code:

- prefer existing project style,
- avoid new dependencies unless necessary,
- update tests near the changed behavior,
- update docs only when behavior or usage changes,
- keep generated files out of source edits unless the repo expects generated files to be committed.

### Phase 7 — Verification and adversarial gate

The verification and adversarial layers review what THIS run did. If the route is read-only (`needs_implementation=false` / `read_only=true` — a pure audit, review, research, or plan), no code changed, so there is nothing to execute-verify and nothing to change-review. Do not manufacture either; pass the read-only signal through and let the gate report cleanly.

Detect checks (change task):

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_verify.py \
  --workspace . \
  --run-dir .ultracode/runs/<run-id>
```

Execute checks only when code changed and the sandbox/permission state allows it:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_verify.py \
  --workspace . \
  --run-dir .ultracode/runs/<run-id> \
  --execute
```

For a read-only audit, pass `--read-only` instead of `--execute`. This records that the project's own test/build/lint are not applicable (no code changed) rather than leaving them as a "detected but not executed" gap:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_verify.py \
  --workspace . \
  --run-dir .ultracode/runs/<run-id> \
  --read-only
```

Run the adversarial gate whenever the route selects verification, implementation, migration, refactor, package generation, install scripts, or final-answer claim checking. Use `--strict` for nontrivial modifications, packaging, installers, or migration tasks.

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_adversarial_verify.py \
  --workspace . \
  --run-dir .ultracode/runs/<run-id> \
  --task "<task text after removing $ultracode>" \
  --strict
```

For a read-only audit, add `--read-only` (the run also reads `run.json`'s `read_only` flag automatically). The deterministic findings then stay advisory instead of hard-failing on code this run did not author.

The adversarial gate is a CHANGE review, not a whole-repo linter. It scopes itself automatically:

- With a real git diff, it scans **only the lines your change added** (including new/untracked files) — pre-existing or untouched code is out of scope and cannot be flagged.
- With a clean/empty diff (read-only audit), it has nothing to change-review: it reports `scope: empty-clean`, finds nothing from repo code, and does not block. Do not read its silence as "no risks" — real risks for a read-only audit come from the reviewer/claim-checker subagents, not the deterministic scan.
- With an explicit `--path`, a no-git fallback, or any read-only run, findings are **advisory** (cannot be attributed to a change this run made) and only `warn`, never hard-fail, in non-strict mode.
- Known blind spot of added-line scoping: a change that only DELETES a safety guard (leaving the now-dangerous line unchanged) is not detected by the deterministic scan — that is what the reviewer/adversary subagents are for.

The adversarial gate writes:

- `adversarial_verification.json`
- `adversarial_verification.md`
- `adversarial_work_items.csv`
- `adversarial_spawn_prompt.md`

Spawn adversarial workers from `adversarial_work_items.csv` for nontrivial changes:

- `ultracode_claim_checker`: checks final claims against files, diffs, commands, and artifacts.
- `ultracode_edge_tester`: designs counterexamples and minimal edge probes.
- `ultracode_adversary`: checks small details, CLI flags, public contracts, docs, generated artifacts, and install flow.
- `ultracode_verifier`: audits whether the verification is sufficient.

Do not suppress critical or high adversarial findings that are tied to your change. Either fix them, rerun verification, or list them as unresolved with exact evidence. Trust the gate's `completion_allowed`: a `warn` on advisory/pre-existing findings is not a blocker. In strict mode, do not present a clean completion if the gate's `completion_allowed` is `false`.

#### When executable verification is not applicable (read-only audit or unsupported environment)

For a read-only task (`needs_implementation=false`, e.g. a pure audit or review where no code changed), or when the verification scripts cannot run in this environment (for example they hang on a slow/large filesystem such as a WSL `9p`/`drvfs` mount, or the sandbox denies writing the run dir), do NOT loop on the scripts. Instead record a durable, explicit justification by writing a non-empty `verification_skip.json` into the run directory:

```bash
cat > .ultracode/runs/<run-id>/verification_skip.json <<'JSON'
{ "skipped": true, "reason": "read-only audit; no code changed", "checked_instead": ["doc/code/test cross-read by 4 read-only subagents", "git status clean"] }
JSON
```

The Stop completion gate accepts a non-empty `verification_skip.json` (or an `ULTRACODE-VERIFICATION-SKIP` marker in `ledger.md`) as satisfying the durable-evidence requirement, so the run can finish cleanly without re-running checks that are not meaningful for the task. Be honest: only skip when execution is genuinely not applicable, and state what you verified instead.

### Phase 8 — Final ledger

Update the run ledger. The final answer must contain:

- `Route`: the current-model route decision and major capability flags.
- `Scope`: what was included and excluded.
- `Core findings`: concise but evidence-grounded.
- `Changes made`: files and intent.
- `Verification`: commands run and result status.
- `Adversarial gate`: pass/warn/fail, findings count, and what was resolved or left open.
- `Unresolved risks`: anything not fully proven.
- `Next action`: exactly one recommended next command or review step when useful.

## Included tools

- `scripts/uc_route.py`: creates a routing artifact; the current model reads it and chooses capabilities.
- `scripts/uc_bootstrap.py`: creates a run directory, repo inventory, dynamic plan, work-item CSV, and starter ledger.
- `scripts/uc_merge_results.py`: merges subagent JSON/Markdown results into a synthesis report.
- `scripts/uc_verify.py`: detects and optionally runs language-specific checks.
- `scripts/uc_adversarial_verify.py`: deterministic adversarial gate; scans diffs, claims, verification gaps, risky patterns, test gaps, and generates adversarial worker CSV/prompts.
- `scripts/uc_hook_router.py`: optional hook router for `$ultracode` prompt context, subagent context, stop-continuation guardrails, adversarial-completion nudging, and destructive-command blocking.
- `scripts/uc_state.py`: optional state helper retained for compatibility; do not rely on it for activation. Activation is `$ultracode`.
- `scripts/uc_check_package.py`: validates the package structure, YAML frontmatter, TOML agent files, hook behavior, and Python scripts.
