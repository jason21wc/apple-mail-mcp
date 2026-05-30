#!/bin/bash
# PreToolUse hook for Bash commands
# Checks: branch protection, tag creation enforcement

# Read JSON input from stdin
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command')

# ===================================================
# Build a "cleaned" view of the command used for detection:
#   1. Heredoc bodies are stripped, so text inside a heredoc (e.g. a
#      commit message that mentions "git commit") is never mistaken for
#      a real command invocation.
#   2. Command separators (&& || ;) are normalized to newlines, so a
#      command like `cd repo && git commit` is detected even though
#      `git commit` is not at the very start of the line.
# Detection then anchors with `^` against this cleaned, one-command-per-
# line text.
# ===================================================
strip_heredocs() {
    awk '
    {
        if (in_heredoc) {
            line = $0
            sub(/^[ \t]+/, "", line)
            if (line == delim) in_heredoc = 0
            next
        }
        if (match($0, /<<-?[ \t]*[\047\042]?[A-Za-z_][A-Za-z0-9_]*[\047\042]?/)) {
            d = substr($0, RSTART, RLENGTH)
            gsub(/<<-?[ \t]*|[\047\042]/, "", d)
            delim = d
            in_heredoc = 1
        }
        print
    }
    '
}

CLEANED=$(printf '%s' "$COMMAND" | strip_heredocs | awk '{gsub(/&&|\|\||;/, "\n"); print}')

# This hook polices ONLY the repo it lives in (apple-mail). Commits/tags
# in other repositories are out of its scope.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)
HOOK_REPO=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)

# Returns 0 only if the effective target directory resolves into HOOK_REPO.
targets_this_repo() {
    local dir="$1" root
    root=$(git -C "${dir:-.}" rev-parse --show-toplevel 2>/dev/null)
    [ -n "$root" ] && [ -n "$HOOK_REPO" ] && [ "$root" = "$HOOK_REPO" ]
}

# Returns 0 if the cleaned command actually runs a command matching the
# given regex (after optional leading whitespace).
command_runs() {
    printf '%s\n' "$CLEANED" | grep -qE "^[[:space:]]*$1"
}

# If the command leads with `cd <dir>`, that directory is the repo the
# commit/tag actually targets. Echoes the dir, or empty for the hook cwd.
target_repo_dir() {
    local dir=""
    if [[ "$COMMAND" =~ ^[[:space:]]*cd[[:space:]]+\"([^\"]+)\" ]]; then
        dir="${BASH_REMATCH[1]}"
    elif [[ "$COMMAND" =~ ^[[:space:]]*cd[[:space:]]+\'([^\']+)\' ]]; then
        dir="${BASH_REMATCH[1]}"
    elif [[ "$COMMAND" =~ ^[[:space:]]*cd[[:space:]]+([^[:space:];\&\|]+) ]]; then
        dir="${BASH_REMATCH[1]}"
    fi
    printf '%s' "$dir"
}

current_branch() {
    local dir="$1"
    if [ -n "$dir" ]; then
        git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null
    else
        git rev-parse --abbrev-ref HEAD 2>/dev/null
    fi
}

# ===================================================
# CHECK: Prevent commits to main branch
# ===================================================
check_no_commits_to_main() {
    if ! command_runs 'git[[:space:]]+commit([[:space:]]|$)'; then
        return 0
    fi

    local dir branch
    dir=$(target_repo_dir)

    # Only police commits into this repo, not other repositories.
    targets_this_repo "$dir" || return 0

    branch=$(current_branch "$dir")

    # Allow release branches
    if [[ "$branch" =~ ^release/ ]]; then
        return 0
    fi

    if [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
        if ! printf '%s\n' "$CLEANED" | grep -qiE "hotfix|emergency"; then
            local where="${dir:-current repo}"
            echo "Cannot commit directly to $branch in $where. Create a feature branch first." >&2
            return 2
        fi
    fi

    return 0
}

# ===================================================
# CHECK: Enforce wrapper script for tag creation
# ===================================================
check_tag_creation_workflow() {
    if ! command_runs 'git[[:space:]]+tag([[:space:]]|$)'; then
        return 0
    fi

    # The create_tag.sh wrapper is apple-mail's; don't enforce it elsewhere.
    targets_this_repo "$(target_repo_dir)" || return 0

    echo "Use ./scripts/create_tag.sh <tag-name> instead of direct git tag commands." >&2
    return 2
}

# ===================================================
# Run all checks
# ===================================================
CHECKS=(
    check_no_commits_to_main
    check_tag_creation_workflow
)

for check in "${CHECKS[@]}"; do
    $check
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        exit $EXIT_CODE
    fi
done

exit 0
