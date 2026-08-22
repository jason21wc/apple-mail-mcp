<!-- scaffold: code/standard template-v2.68.0 2026-08-21 -->
# apple-mail

**Description:** CHMG fork of the Apple Mail MCP server — an MCP bridge between AI agents and Apple Mail via AppleScript + a direct-IMAP fast path, on macOS.
**Framework:** AI Coding Methods (current version)
**Mode:** Standard

This is the shared body, read natively by Codex and imported by `CLAUDE.md`
and `GEMINI.md`. It is a **pointer file**, not a copy: the detailed project
rules live in `.claude/CLAUDE.md` and the reference docs live in `docs/`.
Nothing here restates them, because a copy drifts from what it copied.

## Read This First

- **`.claude/CLAUDE.md`** — the project rules: commands, API surface, core
  principles, AppleScript gotchas, testing requirements, branch convention.
  Upstream owns this file; the fork adds only the header block. **Read it
  before making any change.**
- **`ARCHITECTURE.md`** (root) — the *fork's* architecture: where the fork
  boundary falls, the governance and memory layers, trust boundaries.
- **`SPECIFICATION.md`** (root) — what the fork is for and what it is not for.
- **`docs/reference/TOOLS.md`** — the canonical tool list.
- **`docs/reference/ARCHITECTURE.md`** — the *server's* internals (upstream-owned).

Derivable facts — test counts, coverage, tool counts, line counts — are not
pinned in any instruction file. Run `make test` / `make coverage` and read
`docs/reference/TOOLS.md`. Pinning them is how they rot.

## Memory Files

Project memory lives in `_ai-context/`. **This project deviates from the
framework default: `_ai-context/` is gitignored, not committed.** This
repository is PUBLIC and the memory carries CHMG business context (senders,
account structure, credential env-var patterns, migration plans). Every agent
still reads these files from disk, so cross-agent memory works fully; only
publication is withheld. The loaders below ARE committed.

- `_ai-context/SESSION-STATE.md` — current position, quick reference, next actions
- `_ai-context/PROJECT-MEMORY.md` — decisions, constraints, gotchas
- `_ai-context/LEARNING-LOG.md` — active lessons
- `_ai-context/BACKLOG.md` — deferred work that finishes
- `_ai-context/OPERATIONS.md` — recurring commitments that never finish: cadences, tripwires, standing authorizations, metrics

The host tool's own built-in memory is separate — leave it to the host.

## Session Start

1. Read `_ai-context/SESSION-STATE.md` — current position, next actions
2. Read `_ai-context/PROJECT-MEMORY.md` — decisions, constraints, gotchas
3. Read `_ai-context/LEARNING-LOG.md` — active lessons
4. If present, check `_ai-context/OPERATIONS.md` for cadences now due and tripwires whose condition has become true
5. Run `make test` — establish a known-good baseline before changing anything

## Governance

Guidance for any host with the ai-governance MCP server connected (the
*enforcement* mechanism, where one exists, lives in the platform overlay such as
CLAUDE.md — not here):
- `evaluate_governance(planned_action="...")` — before any non-read action
- `query_project(query="...")` — before creating or modifying code/content
- `search_references(query="...")` — before implementing a pattern, to reuse proven precedent from the shared Reference Library
- `capture_reference(...)` — after solving a non-obvious, reusable problem, to bank the lesson in the shared, central Reference Library

The MCP server itself runs ONLY behind ai-governance-proxy in hard mode, per
`claude_desktop_config.json`. Never launch it directly.

## Key Commands

```bash
make test                  # Unit tests (~5s, mocked AppleScript)
make test-integration      # Real Mail.app tests (requires MAIL_TEST_ACCOUNT)
make test-e2e              # End-to-end MCP tool tests
make check-all             # Everything: lint, typecheck, test, complexity, version-sync, parity
make lint / format / typecheck / coverage
```

Unit tests mock `_run_applescript()` and **cannot** catch AppleScript bugs. If
you touched AppleScript, integration tests must cover it before merge.

## Project Structure

```
src/apple_mail_fast_mcp/
  mail_connector.py    AppleScript client (upstream-owned; the fork does not modify it)
  imap_connector.py    Direct-IMAP fast path
  smtp_sender.py       SMTP submission for send_now
  server.py            FastMCP server — where BOTH fork modifications live
  security.py          Input validation, audit logging, test-mode safety gate
  utils.py             Escaping, parsing, validation
.claude/skills/        Project skills, incl. attachment-retrieval (fork-only)
_ai-context/           Project memory (gitignored — see above)
docs/reference/        Canonical API + architecture docs
```

## Upstream

Fork of `s-morgan-jeffries/apple-mail-fast-mcp`. The fork is deliberately
**thin** — two behavioral modifications in `server.py`, plus one
temporary, upstream-bound connector exception (PR #54; see `ARCHITECTURE.md`). Sync **small and
often**; letting it drift is what turned one past sync into a 109-conflict
ordeal. See `ARCHITECTURE.md` for the fork boundary and `_ai-context/OPERATIONS.md`
for the sync cadence.
