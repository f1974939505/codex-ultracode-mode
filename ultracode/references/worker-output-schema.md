# Ultracode worker output schema

Each subagent or CSV fan-out worker should report exactly one result object.

```json
{
  "id": "required work item id",
  "agent": "mapper|planner|worker|reviewer|verifier|docs",
  "status": "ok|blocked|needs-confirmation|failed",
  "summary": "concise summary",
  "evidence": [
    {
      "path": "file path or command name",
      "lines": "line range, symbol, or command output anchor",
      "claim": "what this evidence supports"
    }
  ],
  "findings": [
    {
      "severity": "critical|high|medium|low|info",
      "claim": "finding claim",
      "evidence": ["evidence references"]
    }
  ],
  "changes": [
    {
      "path": "file path",
      "intent": "why it changed",
      "risk": "compatibility or behavior risk"
    }
  ],
  "verification": [
    {
      "command": "command run or proposed",
      "status": "pass|fail|skipped|not-run",
      "output_summary": "short output summary"
    }
  ],
  "recommendations": ["actionable recommendation"],
  "open_questions": ["uncertainty that must not be deleted"]
}
```

Use `needs-confirmation` when two files, docs, or commands disagree and no stronger evidence resolves the conflict.
