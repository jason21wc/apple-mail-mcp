<!-- scaffold: code/standard template-v2.68.0 2026-08-21 -->
# Architecture

> **Scope note.** This file documents the **fork's** architecture — where the
> fork boundary falls, and the layers CHMG adds around the server. The
> **server's own** internals (connector design, AppleScript emission, IMAP
> dispatch) are documented upstream in `docs/reference/ARCHITECTURE.md`, which
> is the source of truth for them. Nothing is restated here; a copy of an
> upstream doc drifts from it on the next sync.

## Overview

An MCP server that bridges an AI agent and Apple Mail on macOS, reached through
two paths: AppleScript via `osascript` (works for anything Mail.app exposes) and
a direct-IMAP fast path (faster and more reliable for reads, but needs a
per-account app password). CHMG runs it to pull recurring email attachments —
weekly property reports — into local folders, incrementally and undoably.

## System Structure

```
    AI agent (Claude Code / Claude Desktop / Codex / Gemini)
              |
    ai-governance-proxy  (HARD MODE — the only supported entry point)
              |
    apple_mail_fast_mcp.server   FastMCP tool surface  <-- both fork mods live here
              |
      +-------+-------+
      |               |
  imap_connector   mail_connector
  smtp_sender      (osascript -> AppleScript -> Mail.app)
      |               |
   mail host       Mail.app
```

## Component Responsibilities

| Component | Owns | Does NOT own |
|---|---|---|
| `server.py` | The MCP tool surface, response shaping, rate limits, safety gates. **Both fork modifications.** | Anything about how Mail is actually reached. |
| `mail_connector.py` | AppleScript generation and execution. | Tool contracts. **Upstream-owned — the fork does not modify it.** |
| `imap_connector.py` / `smtp_sender.py` | Direct IMAP reads and SMTP submission. | Fallback policy (the connector decides). |
| `security.py` | Input sanitization, AppleScript escaping, path-traversal-safe name validation, audit logging, test-mode safety gate. | Business rules. |
| ai-governance-proxy | Enforcement. Runs *outside* this repo. | Anything in the mail domain. |
| `.claude/skills/attachment-retrieval/` | Recipe-based attachment grabs + append-only undo log. **Fork-only.** | Any server behavior — it is a caller, not a component. |

## The Fork Boundary

The fork is deliberately thin. **Two behavioral modifications, both in
`server.py`:**

1. **`save_attachments(output_filename=...)`** — save a single attachment under
   a caller-chosen, sanitized name. Used by the attachment-retrieval skill to
   produce deterministic filenames, which is what makes an incremental re-run
   possible without a dedup ledger.
2. **`content_is_untrusted` / `security_notice` marking** — applied through a
   single-source `_mark_untrusted()` helper on every content-returning tool
   (`get_messages`, `get_attachment_content`, `search_messages`, `get_thread`).

Everything else the fork once carried has converged into upstream and been
dropped. Fork infrastructure that is not a behavior change: governance
integration, the attachment-retrieval skill, and
`tests/integration/test_fork_extensions_integration.py`.

**Temporary exception (2026-08-22).** PR #54 modifies `imap_connector.py` and `mail_connector.py` to give `get_thread` `account`/`mailbox` hints — a deliberate, owner-approved departure from the connectors-untouched rule, taken because the bug blocked the fork's primary use case. It is upstream-bound; until it lands there the fork carries divergence in two hot files, which raises the cost of every sync. Do not treat it as licence for more connector work.

**Why thin matters:** the fork and upstream churn the same hot files. Drift is
paid for at merge time, superlinearly — one past sync cost 109 conflicts.
Sync small and often; prefer contributing upstream over accumulating fork code.

## Data Flow

An attachment retrieval, end to end:

1. Agent calls a tool; ai-governance-proxy evaluates and permits it.
2. `server.py` sanitizes input, checks rate limits and the test-mode safety gate.
3. The connector tries direct IMAP first, falling back to AppleScript.
4. Bytes come back; `_mark_untrusted()` tags the response as external data.
5. `save_attachments` writes to disk under a validated name.
6. The skill appends to its per-recipe undo log in `~/.apple_mail_mcp/retrieval_runs/`.

## Dependencies

Python 3.10+, FastMCP, `osascript` (system), stdlib `imaplib`/`smtplib`.
Locked in `uv.lock`; audited by a weekly `dependency-audit.yml` workflow that is
expected to stay green — see `_ai-context/OPERATIONS.md`.

## Security Architecture

**Trust boundaries.** Email content is attacker-controlled and crosses into an
LLM's context. Attachment payloads are the fork's primary threat surface —
externally-sent hotel PDFs — which is why fork modification #2 exists: upstream's
`prompt_injection` flag covers message *bodies*, not attachment *payloads*.
The marking is a signal to the consuming model, not enforcement.

**Input handling.** All user input is sanitized twice — `sanitize_input()` then
`escape_applescript_string()` — before reaching AppleScript. Any name used as a
filename stem is regex-validated *before* a path is built; never `Path(user_input)`.

**Credentials.** IMAP/SMTP share one per-account app password, supplied by env
var (`APPLE_MAIL_MCP_IMAP_PASSWORD_<ACCOUNT>`) or Keychain. Never committed.

**Blast radius.** `MAIL_TEST_MODE` + `MAIL_TEST_ACCOUNT` gate destructive
operations to a test account and restrict sends to reserved domains.

**Publication boundary.** This repository is public. `_ai-context/` is
gitignored because project memory carries CHMG business context. Agents read it
from disk; git never sees it.

## Architecture Decisions

**ADR-1 — Run only behind ai-governance-proxy, hard mode.** The server can read
and delete real mail. Enforcement belongs outside the thing being enforced.
*Consequence:* never launch the server directly, in any environment.

**ADR-2 — Keep the fork thin; converge upstream.** *Rationale:* merge cost
scales with divergence in shared hot files. *Consequence:* new capability is
proposed upstream first; fork-local code needs a reason it cannot live upstream.

**ADR-3 — Do not modify `mail_connector.py`.** *Rationale:* it is the file
upstream churns hardest and the one unit tests cannot validate (they mock
`_run_applescript()`). *Consequence:* fork behavior lives at the server layer,
where it is testable and merge-safe.

**ADR-4 — Mark attachment payloads untrusted even though upstream marks bodies.**
*Rationale:* upstream's #225 covers bodies; payloads are this fork's actual
threat surface. *Consequence:* a deliberate divergence, retired once upstream
adopts payload marking.

**ADR-5 — Incremental retrieval by deterministic filename + `exists()`, not a
dedup ledger.** *Rationale:* a ledger is state that can desynchronize from the
filesystem; the filesystem is already the state. *Consequence:* fork
modification #1 exists to make filenames deterministic.

**ADR-6 — `_ai-context/` is gitignored, deviating from the framework default.**
*Rationale:* public repo, private business content. *Consequence:* no team
memory sharing through git — acceptable for a solo fork; revisit if that changes.

**ADR-7 — Derivable facts are never pinned in instruction files.** *Rationale:*
test counts and tool counts drift every PR; a pinned number rots and then
misleads. *Consequence:* instruction files point at `make test` and
`docs/reference/TOOLS.md` instead of quoting them.
