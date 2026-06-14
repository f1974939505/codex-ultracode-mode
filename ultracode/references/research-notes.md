# Research notes: Claude Code Ultracode to Codex port

Date: 2026-06-14

## Claude Code behavior being ported

Claude Code dynamic workflows are not just ordinary prompt instructions. Official Anthropic documentation describes a workflow as a JavaScript script that Claude writes for the task and a separate runtime executes. The script coordinates subagents, while intermediate results live in script variables rather than in the chat context. Official docs also state that workflows are suitable for codebase audits, large migrations, and cross-checked research.

Ultracode is an effort setting in Claude Code. It combines xhigh reasoning effort with automatic workflow orchestration. With ultracode enabled, Claude decides when a task warrants a workflow; one request can become several workflows, such as understand → change → verify. It is session-scoped and resets in a new session.

The workflow runtime has important limits: no mid-run user input except permission prompts, no direct filesystem or shell access from the workflow script itself, up to 16 concurrent agents, and up to 1,000 total agents per run.

## Codex building blocks available

Codex skills are directories with a required SKILL.md file and optional scripts, references, assets, and agents. Codex loads the full skill only when it selects or is explicitly invoked with `$skill-name`.

Codex subagents can be spawned explicitly and are suitable for highly parallel tasks such as codebase exploration and multi-step feature implementation. Current Codex releases enable subagent workflows by default. Codex only spawns subagents when explicitly asked.

Codex supports custom agents through TOML files under `~/.codex/agents/` or `.codex/agents/`. Each custom agent requires `name`, `description`, and `developer_instructions`; optional fields can include model, model_reasoning_effort, sandbox_mode, and skill config.

Codex supports experimental CSV fan-out via `spawn_agents_on_csv`, where one CSV row becomes one worker job and Codex exports combined results.

Codex hooks can inject deterministic scripts into lifecycle events such as UserPromptSubmit, SubagentStart, SubagentStop, PreToolUse, PostToolUse, and Stop. Hook scripts read JSON from stdin and can return JSON that injects context or blocks supported tool calls.

## Porting conclusion

The correct Codex port is an emulation, not a clone of Claude's workflow runtime. This package implements the portable semantics:

1. exact explicit activation with `$ultracode`,
2. current-model routing before any edit or subagent spawn,
3. dynamic plan generation,
4. durable run artifacts,
5. read-only reconnaissance,
6. parallel subagent/fan-out work,
7. synthesis and adversarial review,
8. deterministic guardrails through hooks,
9. final evidence ledger.

This approximates Claude's dynamic-workflow discipline using Codex-native primitives without pretending that Codex has the same background JS workflow runner.

## Sources checked

- Anthropic Claude Code dynamic workflows documentation: https://code.claude.com/docs/en/workflows
- Anthropic announcement for dynamic workflows: https://claude.com/blog/introducing-dynamic-workflows-in-claude-code
- Anthropic model configuration and effort notes: https://code.claude.com/docs/en/model-config
- OpenAI Codex skills documentation: https://developers.openai.com/codex/skills
- OpenAI Codex subagents documentation: https://developers.openai.com/codex/subagents
- OpenAI Codex hooks documentation: https://developers.openai.com/codex/hooks
- OpenAI Codex configuration reference: https://developers.openai.com/codex/config-reference
