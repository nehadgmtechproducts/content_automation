#!/usr/bin/env bash
# Refuse to commit a secret. Scans what is about to be committed (the staged changes) for API keys, bot tokens and
# private keys, and for a real .env file. Install once per clone:   git config core.hooksPath .githooks
# Run by hand on everything tracked:                                  scripts/check-secrets.sh --all
# Key prefixes must start a word (a slug like "trump-musk-ukraine-..." contains "sk-" and is not a key).
# Placeholders such as "nvapi-test" or "nvapi-..." are fine: real keys are long, and these patterns need that length.
set -uo pipefail

PATTERNS='nvapi-[A-Za-z0-9_-]{20,}|[0-9]{8,10}:[A-Za-z0-9_-]{34,}|(^|[^A-Za-z0-9])(sk-[A-Za-z0-9_-]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[abprs]-[A-Za-z0-9-]{20,})|-----BEGIN [A-Z ]*PRIVATE KEY-----|(FAL_KEY|[A-Z_]*API_KEY|[A-Z_]*_TOKEN|[A-Z_]*SECRET|[A-Z_]*PASSWORD)[ ]*=[ ]*[A-Za-z0-9:_/+=-]{20,}'

if [ "${1:-}" = "--all" ]; then
  hits="$(git grep -n -I -E "$PATTERNS" -- . ':!*.lock' ':!package-lock.json' 2>/dev/null)"
  files="$(git ls-files | grep -E '(^|/)\.env($|\.)' | grep -vE '\.(example|sample|template)$')"
else
  hits="$(git diff --cached -U0 --no-color | grep -E '^\+[^+]' | grep -E "$PATTERNS")"
  files="$(git diff --cached --name-only --diff-filter=AM | grep -E '(^|/)\.env($|\.)' | grep -vE '\.(example|sample|template)$')"
fi

status=0
if [ -n "$files" ]; then
  echo "REFUSED: a real .env file is about to be committed:" >&2; echo "$files" | sed 's/^/  /' >&2; status=1
fi
if [ -n "$hits" ]; then
  echo "REFUSED: this looks like a secret (value hidden):" >&2
  echo "$hits" | sed -E 's/(=[ ]*)[A-Za-z0-9:_\/+=-]{6}[A-Za-z0-9:_\/+=-]*/\1<hidden>/; s/(nvapi-|sk-|ghp_|xox[abprs]-)[A-Za-z0-9_-]+/\1<hidden>/g; s/[0-9]{8,10}:[A-Za-z0-9_-]{34,}/<telegram-token-hidden>/g' | cut -c1-140 | sed 's/^/  /' >&2
  status=1
fi
if [ "$status" -ne 0 ]; then
  echo "Put real values in the git-ignored .env only. If this is a false alarm, shorten or alter the value." >&2
fi
exit "$status"
