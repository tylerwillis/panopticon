# Helpers for the setup-repo workflow's script, kept in a sourceable file (no side effects at load)
# so they can be unit-tested in isolation. The ShellRunner runs `shell_script()` = this lib +
# setup_repo.sh concatenated, so these functions are defined before the interactive flow calls
# them. POSIX sh; needs `grep`, `sed`, `mktemp`.

# True when the host's `script` accepts `-c '<command>' <file>` to run a command
# non-interactively (util-linux's does; so does BusyBox's, common on Alpine — neither of which is
# BSD script). BSD `script` (macOS and other *BSD hosts) has no `-c`: an unrecognized option prints
# usage to stderr and exits nonzero. Probed by capability, not by name/`--version` (util-linux is
# only one of the `-c`-capable implementations, so a name check misfiles BusyBox as BSD-shaped and
# sends it through the wrong, incompatible invocation form). `true` is a harmless, always-present
# command for the probe; the session goes to /dev/null since we only care about the exit status.
script_supports_dash_c() {
    script -q -c true /dev/null >/dev/null 2>&1
}

# True when $1 is shaped like a genuine Claude OAuth token: the fixed `sk-ant-oat01-` prefix
# followed by one or more token characters, and nothing else (anchored full-string match, not a
# substring search). Used to reject a captured or pasted value that isn't the token itself.
is_oauth_token_shaped() {
    printf '%s' "$1" | grep -qE '^sk-ant-oat01-[A-Za-z0-9_-]+$'
}

# Print the last Claude OAuth token (sk-ant-oat01-…) found in capture file $1, or nothing. Robust to
# surrounding ANSI colour codes: the token's character class never overlaps an escape sequence, so a
# plain grep of the contiguous run works without stripping the escapes first. (Persistence is
# independently guarded by store_env_token's own shape check, so this doesn't re-validate its own
# match against the same pattern.)
extract_oauth_token() {
    grep -oaE 'sk-ant-oat01-[A-Za-z0-9_-]+' "$1" 2>/dev/null | tail -n 1
}

# Run `claude setup-token` captured through a pty via `script`, picking the invocation form that
# matches the host's `script` (script_supports_dash_c): the command via `-c '<command>' <file>`
# when supported, else the file followed by the command as trailing positional words (BSD). `-e`
# returns the wrapped command's own exit status rather than `script`'s.
#
# Prints the extracted token (if any) and returns 0 when the command itself ran to completion —
# whether or not a token could be extracted from what it captured. Returns nonzero only when the
# command itself failed or was cancelled (nothing was minted or shown to the operator). Callers
# must read those as two distinct outcomes: a nonzero return means nothing happened; a zero return
# with empty stdout means the command ran (the operator saw its output) but no token could be
# recovered from the capture.
capture_claude_setup_token() {
    _cst_log=$(mktemp "${TMPDIR:-/tmp}/panopticon-setup-token.XXXXXX") || return 1
    if script_supports_dash_c; then
        script -q -e -c 'claude setup-token' "$_cst_log"
    else
        script -q -e "$_cst_log" claude setup-token
    fi
    _cst_ran_ok=$?
    [ "$_cst_ran_ok" -eq 0 ] && extract_oauth_token "$_cst_log"
    rm -f "$_cst_log"
    return "$_cst_ran_ok"
}

# Store a freshly minted value $2 for env var $1 into env-file $3, preserving history:
#   * comment out any existing *active* `<VAR>=…` line (kept as a record, not lost),
#   * drop any placeholder *comment* stub (`# <VAR> =`, or a `<…>` placeholder),
#   * append the new active line (on its own line even if the file lacked a trailing newline).
# Other lines (a different var, blanks, unrelated comments) are left untouched. Atomic replace,
# private perms (the file holds a live credential). Returns nonzero if it can't be written. Shared by
# every token the setup flow writes (CLAUDE_CODE_OAUTH_TOKEN, GH_TOKEN, …). $1 must be a plain env
# var name (`[A-Za-z_][A-Za-z0-9_]*`) — it's interpolated verbatim into the sed/grep patterns, where
# it carries no regex metacharacters.
store_env_token() {
    _set_var=$1
    _set_token=$2
    _set_file=$3
    [ -n "$_set_file" ] || return 1
    # A Claude OAuth token has a known, checkable shape — refuse to ever persist one that doesn't
    # match it (rather than silently writing corrupted capture output as if it were a real token).
    if [ "$_set_var" = "CLAUDE_CODE_OAUTH_TOKEN" ] && ! is_oauth_token_shaped "$_set_token"; then
        return 1
    fi
    umask 077
    mkdir -p "$(dirname "$_set_file")" || return 1
    _set_tmp=$(mktemp "$_set_file.XXXXXX") || return 1
    if [ -f "$_set_file" ]; then
        # 1) comment out an active assignment — a leading '#' means the line no longer starts with
        #    the bare var name, so an already-commented real value is left as-is; then 2) drop
        #    placeholder comment stubs (an empty or `<…>` value).
        sed -E "s/^([[:space:]]*)${_set_var}=/\\1# ${_set_var}=/" "$_set_file" \
            | grep -vE "^[[:space:]]*#[[:space:]]*${_set_var}[[:space:]]*=[[:space:]]*(<[^>]*>)?[[:space:]]*\$" \
            > "$_set_tmp" || true # grep exits 1 when it filters every line — that's fine
    fi
    # Ensure the appended line stands alone even if the kept content didn't end in a newline.
    if [ -s "$_set_tmp" ] && [ -n "$(tail -c 1 "$_set_tmp")" ]; then
        printf '\n' >> "$_set_tmp" || {
            rm -f "$_set_tmp"
            return 1
        }
    fi
    printf '%s=%s\n' "$_set_var" "$_set_token" >> "$_set_tmp" || {
        rm -f "$_set_tmp"
        return 1
    }
    mv "$_set_tmp" "$_set_file" || {
        rm -f "$_set_tmp"
        return 1
    }
    chmod 600 "$_set_file" 2>/dev/null || true
}

# Back-compat wrapper: store a Claude OAuth token. The shared implementation lives in
# store_env_token; this keeps the Claude call site (and its tests) reading clearly.
store_oauth_token() {
    store_env_token CLAUDE_CODE_OAUTH_TOKEN "$1" "$2"
}

# True when URL $1 names github.com as its host, in either form the repo's `git_url` is stored:
# HTTPS (`https://github.com/owner/repo.git`) or SSH (`git@github.com:owner/repo.git`). An empty or
# non-GitHub URL (incl. a GitHub Enterprise host) returns nonzero.
is_github_url() {
    case "$1" in
        *github.com/*|*github.com:*) return 0 ;;
        *) return 1 ;;
    esac
}

# A human label for the repo's source, from its git URL $1 — drives the setup flow's opening summary.
# A GitHub remote (the case that wants a GH_TOKEN); a filesystem path / file:// URL, or a bare ref
# with neither a scheme nor an scp-style host, is a local checkout; anything else is a generic remote.
repo_source_label() {
    if [ -z "$1" ]; then
        printf 'unknown'
    elif is_github_url "$1"; then
        printf 'GitHub remote'
    elif printf '%s' "$1" | grep -qE '^(/|\./|\.\./|~|file://)' \
        || ! printf '%s' "$1" | grep -qE '://|@'; then
        printf 'local checkout'
    else
        printf 'remote'
    fi
}

# True when env-file $2 exists and holds an *active* (uncommented) `$1=` assignment. A commented
# (`# $1=…`) line or a missing file returns nonzero — matching store_env_token's notion of active.
env_file_has_var() {
    [ -f "$2" ] && grep -qE "^[[:space:]]*$1=" "$2"
}

# Print a masked tail of secret $1 for a consent prompt — the last 4 characters, e.g. `...a1b2`, so
# the operator can confirm *which* token without it being shown in full. A value of 4 chars or fewer
# collapses to just `...` (nothing safe to reveal).
mask_last4() {
    if [ "${#1}" -gt 4 ]; then
        printf '...%s' "$(printf '%s' "$1" | tail -c 4)"
    else
        printf '...'
    fi
}

# Print JSON object field $2 from stdin. ShellRunner injects the exact Python interpreter running
# panopticon, so this uses a real JSON parser without assuming jq is installed on the host.
json_field() {
    "$PANOPTICON_PYTHON" -c \
        'import json, sys; value = json.load(sys.stdin).get(sys.argv[1]); print("" if value is None else value)' \
        "$1"
}

# Fetch the setup task's repo and set repo_id/default_harness/credential_dir in the current shell.
# The task service URL and task id are injected into every shell workflow by ShellRunner.
load_repo_auth_context() {
    _rac_task=$(curl --silent --show-error --fail \
        "$PANOPTICON_SERVICE_URL/tasks/$PANOPTICON_TASK_ID") || return 1
    repo_id=$(printf '%s' "$_rac_task" | json_field repo_id) || return 1
    [ -n "$repo_id" ] || return 1
    _rac_repo=$(curl --silent --show-error --fail \
        "$PANOPTICON_SERVICE_URL/repos/$repo_id") || return 1
    default_harness=$(printf '%s' "$_rac_repo" | json_field default_harness) || return 1
    credential_dir=$(printf '%s' "$_rac_repo" | json_field credential_dir) || return 1
    [ -n "$default_harness" ] || default_harness=claude
}

# Update the repo's credential_dir reference after setup creates its host-side directory.
set_repo_credential_dir() {
    _srcd_value=$1
    _srcd_json=$(printf '%s' "$_srcd_value" | "$PANOPTICON_PYTHON" -c \
        'import json, sys; print(json.dumps({"credential_dir": sys.stdin.read()}))') || return 1
    curl --silent --show-error --fail --request PATCH \
        "$PANOPTICON_SERVICE_URL/repos/$repo_id" \
        --header 'Content-Type: application/json' --data "$_srcd_json" >/dev/null
}

# True when $1 is one of the pi adapter's API key environment variables. The value is injected by
# SetupRepo.shell_script directly from panopticon.harnesses.pi.API_KEY_ENV_VARS.
is_pi_api_key_var() {
    case " $PANOPTICON_PI_API_KEY_ENV_VARS " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

# Repo-visible auth checks used by the opening summary and the Codex/Pi dispatch. Host-only native
# auth files do not count: task containers receive only the env-file and repo credential directory.
codex_repo_auth_configured() {
    env_file_has_var CODEX_API_KEY "$1" \
        || env_file_has_var OPENAI_API_KEY "$1" \
        || env_file_has_var CODEX_ACCESS_TOKEN "$1" \
        || { [ -n "$2" ] && [ -f "$2/auth.json" ]; }
}

pi_repo_auth_configured() {
    for _prac_var in $PANOPTICON_PI_API_KEY_ENV_VARS; do
        env_file_has_var "$_prac_var" "$1" && return 0
    done
    [ -n "$2" ] && [ -f "$2/auth.json" ]
}

# Read one secret line without echo. macOS /bin/sh is bash 3.2 and supports `read -s`; the stty
# fallback keeps other host /bin/sh implementations usable. The secret itself is the only stdout.
read_secret() {
    if [ -n "${BASH_VERSION:-}" ]; then
        IFS= read -r -s _rs_value
    else
        stty -echo
        IFS= read -r _rs_value
        stty echo
    fi
    printf '\n' >&2
    printf '%s' "$_rs_value"
}

# Route the repo's chosen default harness to its approved host-side auth flow. An adapter with no
# approved onboarding flow returns nonzero; the caller reports it instead of guessing.
dispatch_harness_auth() {
    case "$1" in
        claude) setup_claude_auth ;;
        codex) setup_codex_auth ;;
        pi) setup_pi_auth ;;
        outfitter)
            echo "Outfitter uses Pi credentials; continuing with Pi authentication."
            [ -z "${credential_path:-}" ] || mkdir -p "$credential_path/outfitter/profiles" || return 1
            setup_pi_auth
            ;;
        *) return 1 ;;
    esac
}
