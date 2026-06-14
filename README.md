# `$ultracode` for Codex

This package installs a Codex skill named `ultracode`. It is designed for one explicit invocation form:

```text
$ultracode <your normal prompt>
```

Do not add mode suffixes. The skill performs a current-model routing pass first and then decides whether the task needs lightweight handling, repo mapping, subagents, implementation, verification, adversarial claim checking, edge-case testing, install-flow checking, or a full multi-phase workflow.

This is not Claude Code's proprietary background JavaScript workflow runtime. It is a Codex-native workflow discipline built from:

- explicit Codex skill invocation through `$ultracode`
- current-model routing via `uc_route.py`
- explicit Codex subagents and custom agent profiles
- CSV fan-out work-item tables
- deterministic bootstrap, merge, verification, adversarial verification, state, and install scripts
- optional Codex hooks for exact `$ultracode` prompt context, subagent schema injection, final-ledger/adversarial nudging, and destructive-command blocking
- a durable final ledger under `.ultracode/runs/<run-id>/`

## One-command install

From the unpacked package root:

```bash
bash install_ultracode.sh
```

That command installs, by default:

```text
~/.codex/skills/ultracode                  # skill (Codex reads $CODEX_HOME/skills)
~/.codex/agents/ultracode_*.toml           # custom agents
~/.codex/hooks.json                        # merged hooks (ultracode groups replaced; your other hooks kept)
~/.codex/ultracode-xhigh.config.toml       # standalone profile: codex --profile ultracode-xhigh
```

Everything installs under `.codex/` only (this package is Codex-specific; nothing is written to `.agents/`). Install **overwrites in place without creating `.bak` backups**. It validates the package first, removes old `ultracode-mode` skill folders, and replaces any prior ultracode hook groups while leaving your unrelated hooks untouched.

After installing hooks, open Codex and run:

```text
/hooks
```

Review and trust the newly installed hook definitions. Codex requires hook trust review for non-managed command hooks.

> Verified on Codex 0.136.0: hooks run in the interactive `codex` TUI after `/hooks` trust. They were **not** observed firing under non-interactive `codex exec` (even with `--full-auto --dangerously-bypass-hook-trust`). Treat hooks as an interactive-session guardrail; the skill, routing scripts, custom agents, and profile all work without them. The hook router emits Codex's `hookSpecificOutput` wrapper shape (`permissionDecision`/`permissionDecisionReason` for PreToolUse, `additionalContext` for UserPromptSubmit/SubagentStart, `decision`/`reason` for Stop), matching Codex's `additionalProperties:false` hook-output schema.

For project-local install:

```bash
bash install_ultracode.sh --scope project --project-root /path/to/repo
```

Windows PowerShell:

```powershell
.\install_ultracode.ps1
```

## Invocation

Use this form:

```text
$ultracode 审阅当前项目的代码、文档、配置和目录结构，提出重构方案，不要修改文件
```

Or:

```text
$ultracode Check this migration for hidden compatibility risks, wrong paths, missing tests, and unsupported final claims.
```

The following forms are intentionally not the runtime interface:

```text
ultracode audit: ...
ultracode verify: ...
ultracode adversarial: ...
```

They are not needed. `$ultracode` activates the skill; the route decides the functions.

## Runtime flow

1. Strip the literal `$ultracode` token from the prompt.
2. Run `uc_route.py` to create a routing artifact:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_route.py \
  --workspace . \
  --task "<task text after removing $ultracode>"
```

3. The current Codex model reads `routing.md` and `route.json`, then chooses capability flags:

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

4. Bootstrap durable run artifacts:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_bootstrap.py \
  --workspace . \
  --task "<task text after removing $ultracode>" \
  --mode auto \
  --max-workers 16
```

5. Spawn subagents only if the route justifies them. Use generated `work_items.csv` and `spawn_agents_prompt.md`.
6. Merge evidence:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_merge_results.py \
  --run-dir .ultracode/runs/<run-id>
```

7. Run deterministic verification:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_verify.py \
  --workspace . \
  --run-dir .ultracode/runs/<run-id> \
  --execute
```

8. Run the adversarial gate when routed:

```bash
python3 ~/.codex/skills/ultracode/scripts/uc_adversarial_verify.py \
  --workspace . \
  --run-dir .ultracode/runs/<run-id> \
  --task "<task text after removing $ultracode>" \
  --strict
```

9. Final answer includes route, scope, findings, changes, verification, adversarial gate, unresolved risks, and one next action.

## Adversarial verification

The adversarial layer is for catching the kind of small-detail errors that large coding agents often make: wrong paths, wrong script names, stale README commands, unsupported final claims, test commands that were inferred but not run, skipped tests, unsafe operations, and install/uninstall inconsistencies.

`uc_adversarial_verify.py` writes:

```text
adversarial_verification.json
adversarial_verification.md
adversarial_work_items.csv
adversarial_spawn_prompt.md
```

Dedicated roles:

- `ultracode_claim_checker`: claim-by-claim evidence audit.
- `ultracode_edge_tester`: minimal counterexample and edge-case probes.
- `ultracode_adversary`: exact file/path/flag/API/docs/install-flow falsification.
- `ultracode_verifier`: verification adequacy audit.

Strict rule: do not present a clean completion when the adversarial gate returns `fail`. Fix it, rerun, or explicitly list the unresolved risk.

## Main scripts

| Script | Purpose |
|---|---|
| `uc_route.py` | Creates `route.json` and `routing.md` so the current Codex model can choose capabilities before edits/subagents. |
| `uc_bootstrap.py` | Creates `.ultracode/runs/<run-id>/` with inventory, plan, work-item CSV, spawn prompt, and ledger. |
| `uc_merge_results.py` | Merges subagent JSON/Markdown results into `synthesis.md` and `claims.csv`. |
| `uc_verify.py` | Detects and optionally runs verification commands for Python, Node, Rust, Go, Make, and CMake projects. |
| `uc_adversarial_verify.py` | Scans diffs, claims, verification gaps, risky patterns, test gaps; creates adversarial gate and worker CSV. |
| `uc_hook_router.py` | Optional hook router for exact `$ultracode` activation context, subagent schema, destructive-command blocking, and final-ledger nudging. |
| `uc_state.py` | Compatibility state helper; activation remains `$ultracode`. |
| `uc_check_package.py` | Validates package structure, SKILL.md frontmatter, TOML agents, hook JSON, Python syntax, route script, and hook smoke behavior. |
| `install.py` | Installs the skill, hooks, custom agents, and optional profile under `.codex/` (overwrite, no backups); cleans legacy `ultracode-mode` names. `--uninstall` removes them. |

## Custom agents included

- `ultracode_mapper`
- `ultracode_doc_mapper`
- `ultracode_test_mapper`
- `ultracode_planner`
- `ultracode_worker`
- `ultracode_reviewer`
- `ultracode_verifier`
- `ultracode_adversary`
- `ultracode_claim_checker`
- `ultracode_edge_tester`

The mapper/reviewer/adversary/claim-checker roles default to read-only discipline. The edge tester may write only temporary probes under `.ultracode/runs/<run-id>/adversarial/` or a temp directory unless explicitly assigned to patch source files.

## Claude Code Ultracode feature parity map

| Claude Code dynamic workflow / Ultracode behavior | `$ultracode` Codex mapping | Status |
|---|---|---|
| Keyword or `/effort ultracode` causes Claude to decide when workflow is warranted | Explicit skill invocation `$ultracode`; current Codex model performs route before action | Partial equivalent |
| Claude writes a JavaScript workflow script for the task | `uc_route.py` + `uc_bootstrap.py` generate route, plan, CSV work items, and prompts; Codex still orchestrates turn-by-turn | Approximation |
| Runtime executes in background while session stays responsive | Codex subagents run explicitly; no separate background JS runtime | Not native |
| Intermediate results live in script variables rather than conversation context | Results are kept in run artifacts under `.ultracode/runs/<run-id>/` and merged | Partial equivalent |
| Dozens to hundreds of agents, max 16 concurrent and 1000 total in Claude runtime | Skill caps recommended concurrent workers at <=16 and uses CSV fan-out for batches | Approximation |
| Workflow can adversarially cross-check findings before reporting | Deterministic adversarial gate + claim/edge/adversary/verifier agents | Strong equivalent for this use case |
| `/workflows` progress UI, pause/resume/restart/save | Not available in Codex; artifacts are inspectable and rerunnable via scripts | Not native |
| Saved workflow command | Skill package plus generated artifacts; no native saved `/workflow` command | Partial/manual |
| Deep research cross-checking and claim filtering | Route can select research/cross-checking, but web/source access depends on the Codex environment | Environment-dependent |

## Local smoke test

From the package root:

```bash
python3 tests/run_smoke_tests.py
```

The smoke test validates the package, verifies explicit `$ultracode` hook activation, rejects bare `ultracode` hook activation, bootstraps a sample repo run, merges fake worker/adversarial results, runs Python syntax verification, runs the adversarial gate, checks destructive-command blocking, and checks that the one-command installers are present.

Test the installer separately without writing files:

```bash
bash install_ultracode.sh --dry-run
```

## Run artifacts and privacy

Run artifacts (plan, work items, inventory, ledger, synthesis, verification/adversarial reports) are written to `<project>/.ultracode/runs/<run-id>/`. This is **not** under `.codex/`: Codex's `workspace-write` sandbox makes the project's `.codex/` directory read-only, so writing runs there fails (EROFS) — the rest of the workspace is writable. The run directory is created `chmod 0700` (owner-only) so artifacts that can contain private/business data are not world-readable on shared hosts, and `<project>/.ultracode/.gitignore` (`*`) keeps them out of git. `uc_route`/`uc_bootstrap` also write `<project>/.ultracode/last_run_dir` so the Stop hook can find the run even when `--out-dir` redirects it elsewhere. If no project path is writable, pass `--out-dir "$(mktemp -d)"` (mode 0700) rather than a fixed world-readable `/tmp/<name>` path.

## Limitations

- Codex currently requires explicit subagent requests; this skill cannot create a native Claude-style background JavaScript workflow runtime.
- Hook trust still requires `/hooks` review after installation, and hooks fire only in interactive `codex` sessions — not under `codex exec` (verified on 0.136.0). Use the skill/scripts/agents/profile for non-interactive runs; rely on hooks (destructive-command deny, `$ultracode` context, Stop gate) only in interactive mode.
- Profiles are standalone `$CODEX_HOME/<name>.config.toml` files selected with `--profile`; the legacy `[profiles.<name>]` table inside `config.toml` is rejected by Codex (>= 0.134) and must not be used.
- The CSV fan-out (`spawn_agents_on_csv`) depends on Codex's experimental `enable_fanout` feature (off by default) and on `agents.max_threads` (default 6; the `ultracode-xhigh` profile raises it to 16). Without the feature, spawn workers individually.
- Exact model effort availability depends on the Codex model/session configuration.
- Adversarial verification improves detail checking but cannot prove correctness without meaningful tests, build commands, and evidence-bearing project structure.
