# S00 — Prepare evidence, credentials, and prerequisite
#
# SOURCE this, do not execute it:   source ./s00-cutover.sh
# Sourcing is required because S01–S09 reuse the variables it exports.
#
# Deliberate deviation from the runbook: no `set -Eeuo pipefail`.
# The runbook's fail-stop is correct for a script, but in an interactive shell
# it closes the window and destroys EVIDENCE_DIR along with every export.
# Each check below still stops the procedure on failure — it just reports and
# returns instead of killing your terminal.

export APP_ROOT="${APP_ROOT:-${${(%):-%N}:A:h}}"
export SERVICE_URL="${SERVICE_URL:-http://127.0.0.1:8000}"
export PANOPTICON_CONFIG="${PANOPTICON_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/panopticon}"
export AUTH_FILE_NAME=task-service-auth.json
export AUTH_PATH="$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME"
export PANOPTICON_SERVICE_AUTH_FILE="$AUTH_FILE_NAME"
export PWA_ORIGIN="${PWA_ORIGIN:-https://phone.example:443}"
export PHONE_BOARD_URL="${PHONE_BOARD_URL:-https://phone.example:443}"
export CANARY_TASK_ID="${CANARY_TASK_ID:-replace-with-nonterminal-task-id}"
export KNOWN_TASK_NAME="${KNOWN_TASK_NAME:-replace-with-task-name-visible-on-phone-board}"
export ISSUE_202_COMMIT="${ISSUE_202_COMMIT:-replace-with-closing-commit}"
export OLD_RUNNER_ID=local

_s00_fail() { print -u2 "S00 FAILED: $1"; print -u2 "Fix it, then re-source this file."; return 1; }

# Refuse to run against example values. Supply these exports in the operator's
# private shell before sourcing this file; do not record live values here.
if [ "$PWA_ORIGIN" = https://phone.example:443 ]; then
  _s00_fail "set PWA_ORIGIN before sourcing"
  return 1
fi
if [ "$PHONE_BOARD_URL" = https://phone.example:443 ]; then
  _s00_fail "set PHONE_BOARD_URL before sourcing"
  return 1
fi
if [ "$ISSUE_202_COMMIT" = replace-with-closing-commit ]; then
  _s00_fail "set ISSUE_202_COMMIT before sourcing"
  return 1
fi
if [ "$CANARY_TASK_ID" = replace-with-nonterminal-task-id ]; then
  _s00_fail "set CANARY_TASK_ID before sourcing"
  return 1
fi
if [ "$KNOWN_TASK_NAME" = replace-with-task-name-visible-on-phone-board ]; then
  _s00_fail "set KNOWN_TASK_NAME before sourcing"
  return 1
fi

export DEPLOY_REV="$(git -C "$APP_ROOT" rev-parse HEAD)"
export NEW_RUNNER_ID="cutover-$(date +%s)"
export EVIDENCE_DIR="$(mktemp -d)"
: > "$EVIDENCE_DIR/followups.md"
: > "$EVIDENCE_DIR/gates.txt"

# The escape fix must be an ancestor of what we are deploying.
git -C "$APP_ROOT" merge-base --is-ancestor "$ISSUE_202_COMMIT" "$DEPLOY_REV" \
  || _s00_fail "issue-202 commit is not an ancestor of DEPLOY_REV"

# ...and it must have a green CI run. Full 40-char SHA required: `gh run list
# --commit` silently returns empty for an abbreviated SHA.
gh run list --repo tylerwillis/panopticon --commit "$ISSUE_202_COMMIT" \
    --workflow ci.yml --status success --limit 1 --json databaseId --jq 'length == 1' \
    | grep --fixed-strings true > /dev/null \
  || _s00_fail "no successful ci.yml run recorded for $ISSUE_202_COMMIT"

uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook \
    inspect-credential-file "$AUTH_PATH" \
  || _s00_fail "credential file failed inspection"

# Read the write token from the credential file rather than the environment.
# The runbook uses `environment_token()`, which returns None in a fresh shell —
# the running host process has those variables, an operator terminal does not.
# That produced `Authorization: Bearer None`, a 401, and a closed window.
export PRE_CUTOVER_WRITE_TOKEN="$(python3 -c "import json; print(json.load(open('$AUTH_PATH'))['write'][0])")"
[ -n "$PRE_CUTOVER_WRITE_TOKEN" ] || _s00_fail "could not read a write token from $AUTH_PATH"

curl --silent --show-error --fail \
     --header "Authorization: Bearer $PRE_CUTOVER_WRITE_TOKEN" \
     "$SERVICE_URL/tasks/$CANARY_TASK_ID" --output "$EVIDENCE_DIR/S00-canary.json" \
  || _s00_fail "could not fetch the canary task"

curl --silent --show-error --fail \
     --header "Authorization: Bearer $PRE_CUTOVER_WRITE_TOKEN" \
     "$SERVICE_URL/runners" --output "$EVIDENCE_DIR/S00-runners.json" \
  || _s00_fail "could not fetch runners"

# S00 Check block
[ -d "$EVIDENCE_DIR" ] || _s00_fail "EVIDENCE_DIR missing"
[ "$NEW_RUNNER_ID" != "$OLD_RUNNER_ID" ] || _s00_fail "runner IDs identical"

print "S00 OK"
print "  EVIDENCE_DIR = $EVIDENCE_DIR      <-- write this down"
print "  DEPLOY_REV   = $DEPLOY_REV"
print "  NEW_RUNNER_ID= $NEW_RUNNER_ID"
