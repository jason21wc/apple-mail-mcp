#!/bin/bash
# Harness for scripts/git-hooks/pre-push — pins the gate's decision matrix
# without touching the network, Mail.app, or the real test suite.
#
# Builds a throwaway git repo with a connector file, simulates a push via
# the hook's stdin contract, and stubs the unit/smoke commands through the
# PRE_PUSH_UNIT_CMD / PRE_PUSH_SMOKE_CMD / PRE_PUSH_ASSUME_MAIL seams.
#
# Run: ./scripts/test_pre_push_hook.sh   (wired directly into
# `make check-all` and available as `make test-hooks`).
set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/scripts/git-hooks/pre-push"
PASS=0
FAIL=0

run_case() {
    local name="$1" expect="$2" stdin_line="$3"
    shift 3
    # Remaining args are VAR=value pairs for the hook environment.
    local out status
    out="$(cd "$WORK" && env "$@" bash "$HOOK" <<<"$stdin_line" 2>&1)"
    status=$?
    if { [ "$expect" = "allow" ] && [ "$status" -eq 0 ]; } || \
       { [ "$expect" = "block" ] && [ "$status" -ne 0 ]; }; then
        echo "PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $name (expected $expect, exit=$status)"
        echo "$out" | sed 's/^/    /'
        FAIL=$((FAIL + 1))
    fi
}

# ---- fixture repo -----------------------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git -C "$WORK" init -q -b main
git -C "$WORK" config user.email t@t && git -C "$WORK" config user.name t
mkdir -p "$WORK/src/apple_mail_mcp"
echo "x = 1" > "$WORK/src/apple_mail_mcp/mail_connector.py"
echo "readme" > "$WORK/README.md"
git -C "$WORK" add -A && git -C "$WORK" commit -qm base
BASE_SHA="$(git -C "$WORK" rev-parse HEAD)"

# Commit 1: non-connector change.
echo "more" >> "$WORK/README.md"
git -C "$WORK" add -A && git -C "$WORK" commit -qm docs
DOCS_SHA="$(git -C "$WORK" rev-parse HEAD)"

# Commit 2: connector change.
echo "x = 2" > "$WORK/src/apple_mail_mcp/mail_connector.py"
git -C "$WORK" add -A && git -C "$WORK" commit -qm connector
CONN_SHA="$(git -C "$WORK" rev-parse HEAD)"

UNIT_OK="true"
UNIT_BAD="false"
SMOKE_PASS="echo '7 passed in 24.00s'"
SMOKE_FAIL="sh -c 'echo 1 failed in 3s; exit 1'"
SMOKE_SKIPALL="echo '7 skipped in 0.11s'"
# The likeliest real shape on a partly-configured machine — and the one
# most sensitive to regex drift (comma right after the count).
SMOKE_MIXED="echo '6 passed, 1 skipped in 30.00s'"

# ---- decision matrix ---------------------------------------------------------
run_case "unit tests fail -> block (no smoke consulted)" block \
    "refs/heads/b $DOCS_SHA refs/heads/b $BASE_SHA" \
    PRE_PUSH_UNIT_CMD="$UNIT_BAD" PRE_PUSH_SMOKE_CMD="$SMOKE_FAIL"

run_case "non-connector push -> allow, smoke not required" allow \
    "refs/heads/b $DOCS_SHA refs/heads/b $BASE_SHA" \
    PRE_PUSH_UNIT_CMD="$UNIT_OK" PRE_PUSH_SMOKE_CMD="$SMOKE_FAIL"

run_case "connector + smoke passes -> allow" allow \
    "refs/heads/b $CONN_SHA refs/heads/b $BASE_SHA" \
    PRE_PUSH_UNIT_CMD="$UNIT_OK" PRE_PUSH_SMOKE_CMD="$SMOKE_PASS"

run_case "connector + mixed passed/skipped summary -> allow" allow \
    "refs/heads/b $CONN_SHA refs/heads/b $BASE_SHA" \
    PRE_PUSH_UNIT_CMD="$UNIT_OK" PRE_PUSH_SMOKE_CMD="$SMOKE_MIXED" \
    PRE_PUSH_ASSUME_MAIL=1

run_case "connector + smoke FAILS -> block" block \
    "refs/heads/b $CONN_SHA refs/heads/b $BASE_SHA" \
    PRE_PUSH_UNIT_CMD="$UNIT_OK" PRE_PUSH_SMOKE_CMD="$SMOKE_FAIL"

run_case "connector + all-skip + Mail.app present -> BLOCK (vacuous)" block \
    "refs/heads/b $CONN_SHA refs/heads/b $BASE_SHA" \
    PRE_PUSH_UNIT_CMD="$UNIT_OK" PRE_PUSH_SMOKE_CMD="$SMOKE_SKIPALL" \
    PRE_PUSH_ASSUME_MAIL=1

run_case "connector + all-skip + NO Mail.app -> allow with notice" allow \
    "refs/heads/b $CONN_SHA refs/heads/b $BASE_SHA" \
    PRE_PUSH_UNIT_CMD="$UNIT_OK" PRE_PUSH_SMOKE_CMD="$SMOKE_SKIPALL" \
    PRE_PUSH_ASSUME_MAIL=0

run_case "connector + all-skip + SMOKE_SKIP_OK=1 -> allow (explicit)" allow \
    "refs/heads/b $CONN_SHA refs/heads/b $BASE_SHA" \
    PRE_PUSH_UNIT_CMD="$UNIT_OK" PRE_PUSH_SMOKE_CMD="$SMOKE_SKIPALL" \
    PRE_PUSH_ASSUME_MAIL=1 SMOKE_SKIP_OK=1

run_case "branch deletion -> allow, nothing to test" allow \
    "refs/heads/b 0000000000000000000000000000000000000000 refs/heads/b $CONN_SHA" \
    PRE_PUSH_UNIT_CMD="$UNIT_OK" PRE_PUSH_SMOKE_CMD="$SMOKE_FAIL"

echo ""
echo "pre-push hook harness: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
