# Set up a repo's per-repo credentials for task containers: its chosen harness auth, and — for a
# GitHub repo — a GH_TOKEN. Run by the session service in a host tmux session (no
# container); ShellRunner sources the repo's env-file first (so a configured credential shows up as
# an env var) and exports PANOPTICON_ENV_FILE (its path), PANOPTICON_GIT_URL (the repo's remote, used
# to detect a GitHub forge below), and PANOPTICON_REPO_NAME (the repo's label, for the summary).
#
# Whatever route the operator takes, the script converges on a bulleted summary + a prompt to press
# Enter, which completes the task and returns them to the dashboard.

# The fallback text lives outside the expansion: an apostrophe inside a double-quoted
# ${var:-word} is read as a quote character by bash 3.2 (macOS /bin/sh) and unbalances
# the whole script.
env_file="${PANOPTICON_ENV_FILE:-}"
[ -n "$env_file" ] || env_file="the repo's env-file"
repo_name="${PANOPTICON_REPO_NAME:-this repo}"
repo_url="${PANOPTICON_GIT_URL:-}"
repo_label=$(repo_source_label "$repo_url")
repo_id=""
default_harness=claude
credential_dir=""
if ! load_repo_auth_context; then
    echo "warning: couldn't read the repo's harness setting; falling back to claude setup" >&2
fi
credential_path=""
[ -n "$credential_dir" ] && credential_path="$PANOPTICON_SECRETS_DIR/$credential_dir"

# How to get back to the dashboard: detach from this tmux session. Detect the prefix + detach key
# from the running server (the operator may have rebound them), falling back to the tmux defaults.
prefix=$(tmux show-options -gv prefix 2>/dev/null)
[ -n "$prefix" ] || prefix="C-b"
detach=$(tmux list-keys -T prefix 2>/dev/null | awk '$NF == "detach-client" { print $(NF - 1); exit }')
[ -n "$detach" ] || detach="d"
dashboard_hint="To return to the dashboard without finishing, detach: press $prefix then $detach (you can resume this task any time from the dashboard)."

# Open by explaining what this does and that the operator stays in control — task containers get
# per-repo tokens from the env-file (not the operator's own session), and they can opt out entirely.
echo "Setting up '$repo_name'. Task containers use per-repo credentials from this repo's secrets —"
echo "auth for the $default_harness harness, and a GH_TOKEN for GitHub repos — so the agent runs on"
echo "its own tokens, not your personal session. You can skip this and set up secrets yourself."
echo

# Show how to get back to the dashboard, up front before any prompts.
echo "$dashboard_hint"
echo

# Work out what's already set up and what this repo needs. "Configured" means the repo's env-file
# or credential directory carries auth — a host-only environment variable or native login is not
# visible to task containers.
harness_configured=0
case "$default_harness" in
    claude)
        if env_file_has_var CLAUDE_CODE_OAUTH_TOKEN "${PANOPTICON_ENV_FILE:-}" \
            || env_file_has_var ANTHROPIC_API_KEY "${PANOPTICON_ENV_FILE:-}"; then
            harness_configured=1
        fi
        ;;
    codex)
        codex_repo_auth_configured "${PANOPTICON_ENV_FILE:-}" "$credential_path" \
            && harness_configured=1
        ;;
    pi)
        pi_repo_auth_configured "${PANOPTICON_ENV_FILE:-}" "$credential_path" \
            && harness_configured=1
        ;;
esac
gh_needed=0
gh_configured=0
if is_github_url "$repo_url"; then
    gh_needed=1
    env_file_has_var GH_TOKEN "${PANOPTICON_ENV_FILE:-}" && gh_configured=1
fi

# What we know about the repo, and what its setup entails — two bulleted lists up front.
echo "This repo:"
echo "  • Name: $repo_name"
echo "  • Source: $repo_label"
echo
echo "To set up:"
if [ "$harness_configured" -eq 1 ]; then
    echo "  • $default_harness auth — already configured for task containers"
else
    echo "  • $default_harness auth — needed"
fi
if [ "$gh_needed" -eq 1 ]; then
    if [ "$gh_configured" -eq 1 ]; then
        echo "  • GH_TOKEN — already in $env_file"
    else
        echo "  • GH_TOKEN — needed (GitHub repo)"
    fi
else
    echo "  • GH_TOKEN — not needed (not a GitHub repo)"
fi
echo

# The closing summary is a bullet per step; each step appends its outcome here.
summary=""
add_summary() {
    if [ -z "$summary" ]; then
        summary="  • $1"
    else
        summary="$summary
  • $1"
    fi
}

# Write token $2 for var $1 into the env-file, echoing + summarizing the outcome. $3 is the source
# label, $4 the credential label for the summary. Goes through store_env_token, so any existing value
# is commented out and replaced. The shared write step for every path (adopt / paste / mint).
store_token() {
    if [ -n "$2" ] && [ -n "${PANOPTICON_ENV_FILE:-}" ] \
        && store_env_token "$1" "$2" "$PANOPTICON_ENV_FILE"; then
        echo
        echo "Wrote $1 to $env_file (any previous one was commented out)."
        add_summary "$4: wrote $1 to $env_file from $3 (any previous one was commented out)."
    else
        echo
        echo "Couldn't write $1. Add it to $env_file yourself."
        add_summary "$4: couldn't write it — add $1 to $env_file yourself."
    fi
}

# Mint a Claude token with `claude setup-token` and store it — the leaf used when the operator has no
# token to adopt or paste. On success, capture the minted token and write it into the env-file; fall
# back to on-screen copy instructions when it can't be captured. extract_oauth_token / store_env_token
# come from setup_repo_lib.sh (prepended by shell_script()).
mint_claude_token() {
    echo
    echo "Running 'claude setup-token' — follow the prompts to mint a token."
    echo
    umask 077
    _ct_ok=1
    _ct_token=""
    if command -v script >/dev/null 2>&1; then
        # Wrap the OAuth flow in a pty (`script`) so its interactive prompts still work, while teeing
        # the session to a private log capture_claude_setup_token reads the minted token back from.
        # See setup_repo_lib.sh for the util-linux/BusyBox vs BSD invocation split and its
        # ran-vs-extracted return contract: nonzero means the command itself failed or was
        # cancelled (nothing minted); zero with empty output means it ran fine but nothing
        # sk-ant-oat01-shaped could be pulled out of the capture.
        _ct_token=$(capture_claude_setup_token) || _ct_ok=0
    else
        # No `script` to capture with: run it directly (the operator still sees the token on screen).
        claude setup-token || _ct_ok=0
    fi

    if [ "$_ct_ok" -eq 0 ]; then
        add_summary "Claude credential: 'claude setup-token' failed or was cancelled — nothing set up."
    elif [ -n "$_ct_token" ] && [ -n "${PANOPTICON_ENV_FILE:-}" ] \
        && store_env_token CLAUDE_CODE_OAUTH_TOKEN "$_ct_token" "$PANOPTICON_ENV_FILE"; then
        echo
        echo "Wrote the new token to $env_file as CLAUDE_CODE_OAUTH_TOKEN (any previous one was commented out)."
        add_summary "Claude credential: minted a new token and wrote it to $env_file (any previous one was commented out)."
    else
        # The command ran to completion — the operator saw its output — but we couldn't recover a
        # validly-shaped token from the capture, or there's no env-file. Guide the copy instead of
        # ever reporting success with a bad value.
        echo
        echo "Couldn't reliably capture the minted token. Copy the token shown above into $env_file as:"
        echo "    CLAUDE_CODE_OAUTH_TOKEN=<token>"
        add_summary "Claude credential: minted a new token but capture failed — copy it into $env_file as CLAUDE_CODE_OAUTH_TOKEN."
    fi
}

# Set up the Claude credential: offer to adopt a token from the operator's own environment (fast path
# for an already-authenticated operator — masked for consent, default-No), else let them paste one,
# else mint a fresh one with `claude setup-token`. Only offers to adopt a var that isn't already the
# env-file's own (so replacing a configured token goes straight to paste/mint).
setup_claude_token() {
    _sct_var=""
    _sct_val=""
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] \
        && ! env_file_has_var CLAUDE_CODE_OAUTH_TOKEN "${PANOPTICON_ENV_FILE:-}"; then
        _sct_var=CLAUDE_CODE_OAUTH_TOKEN
        _sct_val=$CLAUDE_CODE_OAUTH_TOKEN
    elif [ -n "${ANTHROPIC_API_KEY:-}" ] \
        && ! env_file_has_var ANTHROPIC_API_KEY "${PANOPTICON_ENV_FILE:-}"; then
        _sct_var=ANTHROPIC_API_KEY
        _sct_val=$ANTHROPIC_API_KEY
    fi
    if [ -n "$_sct_val" ]; then
        echo "A $_sct_var is set in your environment (ending $(mask_last4 "$_sct_val"))."
        printf 'Add it to %s for task containers to use? [y/N] ' "$env_file"
        read answer
        case "$answer" in
            [Yy]*)
                store_token "$_sct_var" "$_sct_val" "your environment" "Claude credential"
                return
                ;;
        esac
    fi
    echo
    echo "Paste a Claude token to store it (a CLAUDE_CODE_OAUTH_TOKEN or an ANTHROPIC_API_KEY),"
    echo "or press Enter to mint one with 'claude setup-token'."
    printf '> '
    read pasted
    if [ -n "$pasted" ]; then
        _pv=CLAUDE_CODE_OAUTH_TOKEN
        case "$pasted" in sk-ant-api*) _pv=ANTHROPIC_API_KEY ;; esac
        store_token "$_pv" "$pasted" "the token you pasted" "Claude credential"
    else
        mint_claude_token
    fi
}

# Full Claude dispatch: preserve the existing replace/adopt/paste/setup-token flow unchanged.
setup_claude_auth() {
    if [ "$harness_configured" -eq 1 ]; then
        echo "A Claude credential is already set in $env_file."
        echo
        printf 'Replace it? [y/N] '
        read answer
        case "$answer" in
            [Yy]*) setup_claude_token ;;
            *) add_summary "Claude credential: kept the existing one in $env_file." ;;
        esac
    else
        setup_claude_token
    fi
}

# Full Codex dispatch. Repo env credentials or a shared credential-dir auth.json need no work;
# otherwise run Codex's browser login, then copy the native auth file into the repo's shared dir.
setup_codex_auth() {
    if [ -n "${CODEX_API_KEY:-}" ] || [ -n "${OPENAI_API_KEY:-}" ] \
        || [ -n "${CODEX_ACCESS_TOKEN:-}" ] \
        || { [ -n "$credential_path" ] && [ -f "$credential_path/auth.json" ]; }; then
        echo "Codex credentials already satisfy the harness auth check; no login is needed."
        add_summary "Codex auth: already configured; skipped login."
        return
    fi
    if ! command -v codex >/dev/null 2>&1; then
        echo "The codex CLI is not installed. Install it, then resume this setup task."
        add_summary "Codex auth: codex CLI not installed — login was not run."
        return
    fi
    echo
    echo "Running 'codex login' — complete the browser flow."
    if ! codex login; then
        add_summary "Codex auth: 'codex login' failed or was cancelled."
        return
    fi
    if [ ! -f "$HOME/.codex/auth.json" ]; then
        echo "Codex login finished, but $HOME/.codex/auth.json was not found."
        add_summary "Codex auth: login finished but auth.json was not found; nothing copied."
        return
    fi
    if [ -z "$credential_dir" ]; then
        credential_dir=openai.d
        credential_path="$PANOPTICON_SECRETS_DIR/$credential_dir"
    fi
    umask 077
    if mkdir -p "$credential_path" \
        && cp "$HOME/.codex/auth.json" "$credential_path/auth.json" \
        && chmod 600 "$credential_path/auth.json" \
        && set_repo_credential_dir "$credential_dir"; then
        echo "Stored Codex auth in the repo's private credential directory ($credential_dir)."
        add_summary "Codex auth: logged in and stored auth.json in $credential_dir."
    else
        echo "Couldn't store Codex auth in the repo credential directory. Token contents were not printed."
        add_summary "Codex auth: login succeeded, but auth.json could not be stored for the repo."
    fi
}

# Full Pi dispatch. Name every adapter-supported provider variable, then collect the chosen key with
# hidden input and write it through the same private env-file helper as the other token paths.
setup_pi_auth() {
    if pi_repo_auth_configured "${PANOPTICON_ENV_FILE:-}" "$credential_path"; then
        echo "Pi credentials are already configured for task containers; no key is needed."
        add_summary "Pi auth: already configured; kept the existing credential."
        return
    fi
    echo "Pi accepts these provider credential variables:"
    echo "  $PANOPTICON_PI_API_KEY_ENV_VARS"
    printf 'Environment variable to store [ANTHROPIC_API_KEY]: '
    read pi_var
    [ -n "$pi_var" ] || pi_var=ANTHROPIC_API_KEY
    if ! is_pi_api_key_var "$pi_var"; then
        echo "$pi_var is not one of the accepted variables listed above."
        add_summary "Pi auth: no key stored — unrecognized environment variable $pi_var."
        return
    fi
    printf 'Provider API key (input hidden): '
    pi_key=$(read_secret)
    if [ -n "$pi_key" ]; then
        store_token "$pi_var" "$pi_key" "hidden input" "Pi auth"
    else
        add_summary "Pi auth: no key entered — nothing stored."
    fi
}

# Set up the GH_TOKEN for a GitHub repo: adopt one from the operator's environment (masked, default-No)
# if present and not already the env-file's own, else let them paste one, else skip (guide them to add
# it themselves). We don't mint a GitHub token here.
setup_gh_token() {
    if [ -n "${GH_TOKEN:-}" ] && ! env_file_has_var GH_TOKEN "${PANOPTICON_ENV_FILE:-}"; then
        echo "A GH_TOKEN is set in your environment (ending $(mask_last4 "$GH_TOKEN"))."
        echo "Adding it to $env_file lets task containers use 'gh' and push over HTTPS."
        printf 'Add it to %s? [y/N] ' "$env_file"
        read gh_answer
        case "$gh_answer" in
            [Yy]*)
                store_token GH_TOKEN "$GH_TOKEN" "your environment" "GH_TOKEN"
                return
                ;;
        esac
    fi
    echo
    echo "Paste a GitHub token to store it, or press Enter to skip (add GH_TOKEN to $env_file yourself)."
    printf '> '
    read pasted
    if [ -n "$pasted" ]; then
        store_token GH_TOKEN "$pasted" "the token you pasted" "GH_TOKEN"
    else
        add_summary "GH_TOKEN: none set up — add GH_TOKEN to $env_file yourself."
    fi
}

# --- Harness auth ------------------------------------------------------------------------------
if ! dispatch_harness_auth "$default_harness"; then
    echo "No approved setup-repo auth flow exists for the '$default_harness' harness."
    add_summary "$default_harness auth: unsupported by setup-repo; configure it manually."
fi

# --- GH_TOKEN (GitHub repos only) --------------------------------------------------------------
if [ "$gh_needed" -eq 1 ]; then
    echo
    if [ "$gh_configured" -eq 1 ]; then
        echo "A GH_TOKEN is already set in $env_file."
        echo
        printf 'Replace it? [y/N] '
        read answer
        case "$answer" in
            [Yy]*) setup_gh_token ;;
            *) add_summary "GH_TOKEN: kept the existing one in $env_file." ;;
        esac
    else
        setup_gh_token
    fi
fi

# Every route converges here: summarize what happened (a bullet per step), then complete the task on
# Enter (which ends the session and returns the operator to the dashboard; detaching instead — see
# the hint above — leaves it running).
echo
echo "Summary:"
if [ -n "$summary" ]; then
    echo "$summary"
else
    echo "  • Nothing to do — everything was already set up."
fi
echo
printf 'Press Enter to complete this task and return to the dashboard. '
read _
# panopticon_advance is provided by the panopticon shell lib (loaded by the session service).
panopticon_advance || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
