# Prompt snippets for Codex subagents

## Mapper

Spawn an `ultracode_mapper` subagent. It must stay read-only. Its task is to map `<scope>` and return JSON matching `references/worker-output-schema.md`. It must cite files, symbols, and command outputs. It must not propose edits unless asked.

## Implementation worker

Spawn an `ultracode_worker` subagent for `<scope>`. It may edit only the assigned paths. It must preserve public behavior unless explicitly changing behavior. It must return JSON matching `references/worker-output-schema.md`, including changed files and verification suggestions.

## Reviewer

Spawn an `ultracode_reviewer` subagent. It must be adversarial and read-only. It should try to falsify the plan or patch, identify missing tests, behavior regressions, broken docs, security issues, and generated-file mistakes. It must return JSON matching `references/worker-output-schema.md`.

## Verifier

Spawn an `ultracode_verifier` subagent. It must run or inspect the smallest relevant verification commands and report exact command, status, and output summary. It must not edit files unless the parent explicitly asks for a fix loop.

## CSV fan-out instruction template

Review `{path}` for `{objective}` as `{role}`. Stay within the assigned scope. Return a single JSON object with keys: id, agent, status, summary, evidence, findings, changes, verification, recommendations, open_questions. Call `report_agent_job_result` exactly once.
