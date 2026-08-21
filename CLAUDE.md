<!-- scaffold: code/standard template-v2.68.0 2026-08-21 -->
# apple-mail

Also read AGENTS.md for project context.
@AGENTS.md

The shared project body — memory-file pointers, session-start protocol, and
governance guidance — is imported from `AGENTS.md` above. Keep only
Claude-Code-specific mechanics below; do not duplicate the body here.

## Governance — ENFORCED BY HOOK (Claude Code)

On Claude Code a PreToolUse hook BLOCKS Bash/Edit/Write until the required
governance tools are called — structural, not advisory. S-Series (safety) stop
rules and MCP-requirement enforcement live here, never in the imported body. On
other hosts this degrades to advisory.

## Plan Mode

Use plan mode for architecture-bearing or multi-file work; get a contrarian
review before leaving plan mode.

## Subagents & Skills

- `.claude/agents/` — installed subagents
- `.claude/skills/` — invoke via `/skill-name`
