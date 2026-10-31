#!/bin/bash
# SessionStart hook (wired up in .claude/settings.json). Runs on every session start and resume.
# Whatever this prints is added to Claude's context, so keep it short and actionable.
# Cloud sessions only: exits immediately on a laptop.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/..}" || exit 0

echo "== Ross here.now session check =="

# 1. Did this session start in the right environment? (marker written by the setup script)
if [ -f /etc/ross-herenow-env ]; then
  # shellcheck disable=SC1091
  . /etc/ross-herenow-env
  echo "Environment: Business+Tech (setup $setup_version); browser for smoke tests: $chromium"
else
  echo "WARNING: the Ross environment setup did not run in this session, so smoke tests will be SKIPPED"
  echo "and publishing may be blocked. Tell the user, in plain words: start a new session and pick the"
  echo "\"Business+Tech\" environment (environment picker under the message box, before the first message)."
fi

# 2. Project dependencies (only when missing or the lockfile changed, so resumes stay fast)
if [ -f package.json ]; then
  if [ ! -d node_modules ] || { [ -f package-lock.json ] && [ package-lock.json -nt node_modules/.package-lock.json ]; }; then
    if [ -f package-lock.json ]; then cmd=(npm ci); else cmd=(npm install); fi
    if "${cmd[@]}" --no-audit --no-fund > /tmp/npm-install.log 2>&1; then
      echo "Dependencies: installed (${cmd[*]})"
    else
      echo "WARNING: '${cmd[*]}' failed; see /tmp/npm-install.log before building."
    fi
  else
    echo "Dependencies: already installed"
  fi
fi

# 3. Can this session publish? A blocked host returns 403 from the sandbox proxy, not here.now.
code=$(curl -sS -o /tmp/herenow-probe.txt -w '%{http_code}' --max-time 10 https://here.now/docs 2>/dev/null || true)
if [ "$code" = "200" ]; then
  echo "here.now: reachable"
elif grep -qi 'allow' /tmp/herenow-probe.txt 2>/dev/null; then
  echo "WARNING: here.now is blocked by this environment's network settings, so publishing will fail."
  echo "Tell the user to start a new session in the \"Business+Tech\" environment or contact their instructor."
else
  echo "WARNING: here.now did not answer normally (HTTP ${code:-none}). Publishing may fail."
fi

# 4. Preview mode / sign-in (the credentials file survives an idle pause, not a new session)
signed_in=no
if [ -n "${HERENOW_API_KEY:-}" ] || [ "${HERENOW_AUTH_VIA_PROXY:-}" = "1" ] || [ -s "$HOME/.herenow/credentials" ]; then
  signed_in=yes
fi
if [ -n "${HERENOW_WORKSPACE:-}" ] && [ "$signed_in" = no ]; then
  echo "here.now: NOT signed in. Previews publish into the ${HERENOW_WORKSPACE} workspace, which needs sign-in."
  echo "Before the first publish, follow \"Signing in to here.now\" in CLAUDE.md (ask the student for their here.now email)."
elif [ -n "${HERENOW_WORKSPACE:-}" ]; then
  echo "Previews: published into the ${HERENOW_WORKSPACE} here.now workspace, last 7 days."
elif [ "$signed_in" = yes ]; then
  echo "Previews: saved to the student's here.now account, last 7 days, Site Data and proxy routes work."
else
  echo "Previews: anonymous, last 24 hours, Site Data and proxy routes do NOT work on them."
fi
exit 0
