<!-- scaffold: code/standard template-v2.68.0 2026-08-21 -->
# Specification

> **Scope note.** This specifies the **fork** — what CHMG runs this server for
> and what it deliberately does not do. The per-tool API contract is upstream's
> and lives in `docs/reference/TOOLS.md`, the canonical list. Tool signatures
> are not restated here.

## Problem Statement

CHMG receives recurring operational email — weekly property reports from
management partners — as attachments that have to land in local folders to be
usable. Doing this by hand is repetitive and easy to get wrong: the same report
gets saved twice, or a week gets missed, or a mis-click files it somewhere
nobody looks. The task also has to be safe to re-run and safe to reverse,
because the failure mode of "grab everything again" is a folder full of
duplicates.

Underneath that sits a second problem: an AI agent reading a mailbox is reading
attacker-controlled text. Anyone can send Jason an email. If the agent treats
message bodies or PDF payloads as instructions, the mailbox becomes an
injection channel into a tool that can delete mail.

## Features

1. **Attachment retrieval** — recipe-based grabs of attachments matching sender,
   subject, and date criteria into a chosen folder. The primary use case.
2. **Incremental re-run** — running the same recipe again picks up only what is
   new, via deterministic filenames plus an existence check.
3. **Undo** — every run appends to a per-recipe log, so a grab can be reversed.
4. **Untrusted-content marking** — every content-returning tool tags its payload
   so the consuming model treats bodies *and* attachment payloads as data.
5. **General mail operations** — search, read, organize, draft, templates, rules,
   inbox statistics. Inherited from upstream; see `docs/reference/TOOLS.md`.

## Scope

**In scope:**
- Reliable, repeatable, reversible retrieval of recurring email attachments.
- Running exclusively behind ai-governance-proxy in hard mode.
- Staying close to upstream: sync small and often, contribute fixes upstream.
- Fork-local code only where it cannot reasonably live upstream.

**Out of scope:**
- **Becoming a general mail client.** Upstream owns the mail feature surface.
- **Modifying `mail_connector.py`.** Fork behavior lives at the server layer.
- **Gmail-specific workarounds.** Gmail is being migrated to Apple Business
  Enterprise; its AppleScript `-10000` quirks are explicitly not being fixed.
- **A dedup ledger for retrieval.** The filesystem is already the state (ADR-5).
- **Direct server launch.** There is no supported un-proxied path.
- **Publishing project memory.** `_ai-context/` stays out of this public repo.

## Success Criteria

- A weekly report grab runs, saves the right files under predictable names, and
  a second run of the same recipe saves nothing new.
- A grab can be undone from its log without manual file archaeology.
- `make check-all` is green on `main`; the weekly dependency audit is green.
- Fork drift stays small — measured in single-digit commits behind upstream,
  not dozens (see `_ai-context/OPERATIONS.md` for the cadence).
- Fork behavioral modifications stay at two, or shrink as upstream converges.
- No content-returning tool ships without untrusted marking.

## Constraints

- **macOS only** — AppleScript and Mail.app are hard requirements.
- **Python 3.10+**, FastMCP, `osascript`.
- **Performance floor:** each `osascript` call costs 100–300ms minimum; search
  runs 1–5s; bulk operations are capped at 100 items. These are properties of
  the platform, not tuning targets.
- **Public repository** — nothing business-sensitive may be committed.
- **Upstream-first** — the fork does not accumulate features it could contribute.
- **Testing:** any AppleScript change requires integration tests. Unit tests mock
  `_run_applescript()` and are structurally incapable of catching AppleScript bugs.

## Assumptions

- Mail.app stays running and configured with the relevant accounts.
- Apple-hosted IMAP (iCloud, and Apple Business Enterprise after migration)
  authenticates by app password, giving the fast path a reliable route.
- The upstream maintainer stays active and receptive to contributions — the
  observed pattern so far, and what makes upstream-first viable.
- Report senders keep sending attachments in a recognizable, matchable form.
  If a sender changes format, recipes need updating; nothing detects that
  automatically.
- Jason is the only person working in this repo, which is what makes gitignored
  memory acceptable.
