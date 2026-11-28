#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# The release secrets: the one-time steps only the maintainer can do, so that release runs on
# GitHub can sign both apps (ADR 0009; docs/DEVELOPMENT.md → Release secrets). Run it in your
# own terminal. It never prints a secret, writes one into the repository or puts one on a
# command line, and nobody else (Claude included) ever sees one.
#
#   scripts/release-secrets.sh              what's set up and what's left (no secrets)
#   scripts/release-secrets.sh key          on any computer: the release environment and the release key
#   scripts/release-secrets.sh key --new    a new release key in place of the one on GitHub (only if
#                                           every copy of it is lost: docs/DEVELOPMENT.md → Release secrets)
#   scripts/release-secrets.sh certificate  on the Mac: the Apple Development certificate
#   scripts/release-secrets.sh selftest     make, sign and check with a throwaway key (no GitHub)
#
# Made with the /wizard skill. Its library, above the "STAGES" marker, is trimmed to what this
# script uses: nothing here writes a .env file or a repository-level secret.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────
# Wizard library (the /wizard skill's, trimmed): stages, progress and confirmations.
# ──────────────────────────────────────────────────────────────────────────

if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
  BOLD=$(tput bold); DIM=$(tput dim); RESET=$(tput sgr0)
  BLUE=$(tput setaf 4); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3); RED=$(tput setaf 1)
else
  BOLD=""; DIM=""; RESET=""; BLUE=""; GREEN=""; YELLOW=""; RED=""
fi

# Author sets this at the top of the stages section.
TOTAL_STAGES=0

_STAGE_INDEX=0
WRITTEN_SECRET=() # secret NAMEs set this run
SKIPPED=()        # things we couldn't do (e.g. gh missing)

# _clear wipes the terminal so only the current step is on screen. No-op when
# output isn't a terminal, so piped logs stay readable.
_clear() {
  [[ -t 1 ]] || return 0
  if command -v tput >/dev/null 2>&1; then tput clear; else printf '\033[2J\033[3J\033[H'; fi
}

# banner "Title" shows the opening frame: what this wizard does.
banner() {
  _clear
  printf '\n%s%s  %s%s\n' "$BOLD" "$BLUE" "$1" "$RESET"
  printf '%s  %s stages%s\n\n' "$DIM" "$TOTAL_STAGES" "$RESET"
  printf '%s  It says what to do at each step, and asks before anything goes to GitHub.\n' "$DIM"
  printf '  Stop any time with Ctrl-C: it clears the clipboard and leaves no secret on disk.%s\n' "$RESET"
  pause "Ready to start?"
}

# stage "Name" clears the screen, then announces a stage and shows progress.
# Clearing keeps only the current step on screen.
stage() {
  _clear
  _STAGE_INDEX=$((_STAGE_INDEX + 1))
  printf '\n%s%s▸ Stage %s/%s · %s%s\n' \
    "$BOLD" "$BLUE" "$_STAGE_INDEX" "$TOTAL_STAGES" "$1" "$RESET"
}

# say "..." prints a plain instruction line.
say()  { printf '  %s\n' "$1"; }
# step "..." is an action the person takes.
step() { printf '  %s•%s %s\n' "$BLUE" "$RESET" "$1"; }
note() { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }
warn() { printf '  %s⚠ %s%s\n' "$YELLOW" "$1" "$RESET"; }

# pause "msg" waits for the human to confirm they've done the manual part.
pause() {
  printf '  %s%s%s ' "$DIM" "${1:-Press Enter to continue}" "$RESET"
  read -r _ || true
}

# confirm "question" is a y/N gate; returns success on yes.
confirm() {
  local reply=""
  printf '  %s? %s [y/N] ' "$YELLOW" "$1"
  read -r reply || true
  [[ "$reply" =~ ^[Yy] ]]
}

# ask_secret KEY "Prompt" reads a value into $KEY without showing it.
ask_secret() {
  local key="$1" prompt="$2" input
  printf '  %s%s%s ' "$BOLD" "$prompt" "$RESET"
  read -rs input || true
  printf '\n'
  printf -v "$key" '%s' "$input"
}

# finish clears, then shows a closing summary: complete only when nothing was skipped.
finish() {
  _clear
  if (( ${#SKIPPED[@]} )); then printf '\n%s%s  ⚠ Not everything is set up%s\n' "$BOLD" "$YELLOW" "$RESET"
  else printf '\n%s%s  ✓ Setup complete%s\n' "$BOLD" "$GREEN" "$RESET"; fi
  if (( ${#WRITTEN_SECRET[@]} )); then note "set ${#WRITTEN_SECRET[@]} GitHub secret(s): ${WRITTEN_SECRET[*]}"; fi
  if (( ${#SKIPPED[@]} )); then
    printf '\n'; warn "still to do:"
    for s in "${SKIPPED[@]}"; do note "  - $s"; done
  fi
  printf '\n'
}

# ──────────────────────────────────────────────────────────────────────────
# STAGES: the release secrets. Each part sets TOTAL_STAGES before its banner.
# ──────────────────────────────────────────────────────────────────────────

REPO="DigitalN/I-Need-A-Budget"
MAINTAINER="DigitalN"   # the release environment's required reviewer
ENVIRONMENT="release"
TEAM="XT4LT7MS6Y"       # installed Mac copies take updates signed by this team only
PUBLIC_KEY_FILE="Application Files/release-key.pub"
KEY_ITEM="I Need A Budget release key"   # the password manager's item
OPENSSL=openssl         # find_openssl picks one that makes Ed25519 keys

cd "$(dirname "$0")/.."

ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
bad()  { printf '  %s✗%s %s\n' "$RED" "$RESET" "$1"; }
fail() { printf '\n  %s✗ %s%s\n\n' "$RED" "$1" "$RESET" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  scripts/release-secrets.sh              what's set up and what's left (no secrets)
  scripts/release-secrets.sh key          on any computer: the release environment and the release key
  scripts/release-secrets.sh key --new    a new release key in place of the one on GitHub (only if every copy is lost)
  scripts/release-secrets.sh certificate  on the Mac: the Apple Development certificate
  scripts/release-secrets.sh selftest     make, sign and check with a throwaway key (no GitHub)
EOF
}

# Whatever happens (Ctrl-C too): no secret left on the clipboard, no .p12 left on disk.
CLIPBOARD_HAS_SECRET=""
EXPORT_DIR=""
SELFTEST_DIR=""
cleanup() {
  clear_clipboard
  if [[ -n "$EXPORT_DIR" && "$EXPORT_DIR" == *inab-certificate.* && -d "$EXPORT_DIR" ]]; then rm -rf -- "$EXPORT_DIR"; fi
  if [[ -n "$SELFTEST_DIR" && "$SELFTEST_DIR" == *inab-selftest.* && -d "$SELFTEST_DIR" ]]; then rm -rf -- "$SELFTEST_DIR"; fi
  EXPORT_DIR=""; SELFTEST_DIR=""
  if [[ -t 0 ]]; then stty echo 2>/dev/null || true; fi   # in case Ctrl-C came during a hidden entry
}

# What was pasted, without the spaces, line ends or bracketed-paste marks a terminal may add.
clean_paste() {
  local s=$1 esc=$'\e'
  s=${s//"${esc}[200~"/}; s=${s//"${esc}[201~"/}; s=${s//[[:space:]]/}
  printf '%s' "$s"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ── GitHub ────────────────────────────────────────────────────────────────

need_gh() {
  command -v gh >/dev/null 2>&1 || fail "This needs the GitHub CLI: https://cli.github.com"
  local login
  login=$(gh api user --jq .login 2>/dev/null) || fail "The GitHub CLI isn't signed in: run gh auth login, then this again."
  [[ "$login" == "$MAINTAINER" ]] || fail "The GitHub CLI is signed in as $login; this needs $MAINTAINER (gh auth switch)."
}

# put_secret NAME VAR [WHAT TO DO]: sets the environment's secret NAME to $VAR's value, sent on
# stdin, never on a command line (printf is the shell's own).
put_secret() {
  printf '%s' "${!2}" | gh secret set "$1" --env "$ENVIRONMENT" --repo "$REPO" >/dev/null 2>&1 ||
    fail "GitHub didn't take $1. Nothing secret was shown. ${3:-Run this again.}"
  WRITTEN_SECRET+=("$1")
  ok "$1 is set in the $ENVIRONMENT environment"
}

# Whether the environment is there. Only GitHub's 404 means it isn't: GitHub not answering stops
# the script, so it's never taken for "not there".
environment_exists() {
  local err
  err=$(gh api "repos/$REPO/environments/$ENVIRONMENT" 2>&1 >/dev/null) && return 0
  [[ "$err" == *"HTTP 404"* ]] && return 1
  fail "Couldn't reach GitHub (${err:-no answer}). Nothing was changed: run this again."
}

# "<name>\t<when it was set>" per secret of the environment (names only: GitHub never gives values).
# Nothing when there's no environment yet (a 404). A list GitHub won't give is a failure with the
# reason, never an empty list: a key on GitHub must never be taken for "not set".
secret_list() {
  local out
  if out=$(gh secret list --env "$ENVIRONMENT" --repo "$REPO" --json name,updatedAt \
             --jq '.[] | "\(.name)\t\(.updatedAt)"' 2>&1); then
    printf '%s\n' "$out"; return 0
  fi
  [[ "$out" == *"HTTP 404"* ]] && return 0
  printf "Couldn't read the %s environment's secrets from GitHub (%s). Nothing was changed: run this again." \
    "$ENVIRONMENT" "${out:-no answer}"
  return 1
}
has_secret() {
  local list
  list=$(secret_list) || fail "$list"
  [[ -n "$(printf '%s\n' "$list" | awk -F'\t' -v n="$1" '$1 == n')" ]]
}

# The environment in one line: reviewers|prevent self-review|admins bypass|custom branches.
environment_line() {
  gh api "repos/$REPO/environments/$ENVIRONMENT" --jq '
    ([.protection_rules[]? | select(.type == "required_reviewers")] | first) as $r
    | [([$r.reviewers[]? | .reviewer.login // .reviewer.slug] | join(",")), ($r.prevent_self_review | tostring),
       (.can_admins_bypass | tostring), (.deployment_branch_policy.custom_branch_policies // false | tostring)]
    | join("|")'
}

# "<id> <branch|tag> <name>" per branch the environment deploys from (only with custom branches).
branch_policies() {
  gh api "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies" \
    --jq '.branch_policies[] | "\(.id) \(.type // "branch") \(.name)"'
}

setup_environment() {
  local id policies pid ptype pname
  id=$(gh api "users/$MAINTAINER" --jq .id) || fail "Couldn't look up $MAINTAINER on GitHub."
  printf '{"reviewers":[{"type":"User","id":%s}],"prevent_self_review":false,"can_admins_bypass":false,"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' "$id" |
    gh api -X PUT "repos/$REPO/environments/$ENVIRONMENT" --input - >/dev/null ||
    fail "GitHub didn't take the $ENVIRONMENT environment's settings."
  policies=$(branch_policies) || fail "Couldn't read which branches the environment deploys from."
  while read -r pid ptype pname; do
    [[ -n "$pid" ]] || continue
    [[ "$ptype" == branch && "$pname" == main ]] && continue
    gh api -X DELETE "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies/$pid" >/dev/null ||
      fail "Couldn't take $ptype $pname off the branches it deploys from."
  done <<EOF
$policies
EOF
  if ! grep -qx '[0-9]* branch main' <<<"$policies"; then
    gh api -X POST "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies" -f name=main -f type=branch >/dev/null ||
      fail "Couldn't limit the environment to main."
  fi
}

# ✓/✗ for each setting; fails when one isn't as it should be.
show_environment() {
  local line reviewers self bypass custom branches="" wrong=0
  line=$(environment_line) || { bad "Couldn't read the $ENVIRONMENT environment"; return 1; }
  IFS='|' read -r reviewers self bypass custom <<<"$line"
  if [[ ",$reviewers," == *",$MAINTAINER,"* ]]; then ok "Required reviewer: $reviewers"
  else bad "$MAINTAINER isn't its required reviewer"; wrong=1; fi
  if [[ "$self" == false ]]; then ok "Self-reviews allowed: you approve the runs Claude starts with your login"
  else bad "Prevent self-reviews is on: you couldn't approve your own runs"; wrong=1; fi
  if [[ "$bypass" == false ]]; then ok "Administrators can't bypass it"
  else bad "Administrators can bypass the Approve"; wrong=1; fi
  if [[ "$custom" == true ]]; then branches=$(branch_policies 2>/dev/null | awk '{print $2 " " $3}' | tr '\n' ';') || branches=""; fi
  if [[ "$branches" == "branch main;" ]]; then ok "Deploys from main only"
  else bad "It should deploy from main only"; wrong=1; fi
  return $wrong
}

# ── The release key ───────────────────────────────────────────────────────
# Formats (docs/DEVELOPMENT.md → Release secrets): RELEASE_KEY is the raw 32-byte Ed25519 private
# key (RFC 8032's seed) in base64, 44 characters; the public key is its raw 32 bytes in base64.

# An OpenSSL that makes Ed25519 keys: 3.x (Git for Windows has one; macOS's own LibreSSL doesn't).
find_openssl() {
  local c
  for c in openssl /opt/homebrew/opt/openssl@3/bin/openssl /usr/local/opt/openssl@3/bin/openssl; do
    command -v "$c" >/dev/null 2>&1 || continue
    if [[ "$("$c" genpkey -algorithm ed25519 -outform DER 2>/dev/null | wc -c | tr -d ' ')" == 48 ]]; then
      OPENSSL=$c; return 0
    fi
  done
  return 1
}

# The DER wrappings (RFC 8410) that take the raw keys through OpenSSL:
# hex 302e020100300506032b657004220420 before the private key, 302a300506032b6570032100 before the public.
private_der_prefix() { printf '\060\056\002\001\000\060\005\006\003\053\145\160\004\042\004\040'; }
public_der_prefix()  { printf '\060\052\060\005\006\003\053\145\160\003\041\000'; }

# public_of: a base64 private key on stdin → its base64 public key (nothing when it isn't one).
public_of() {
  { private_der_prefix; "$OPENSSL" base64 -d -A 2>/dev/null; } |
    "$OPENSSL" pkey -inform DER -pubout -outform DER 2>/dev/null | tail -c 32 | "$OPENSSL" base64 -A 2>/dev/null
}

# Whether $1 is exactly 32 bytes in base64, as both halves of the key are (public_of would take a
# longer one's first 32 bytes, so a pasted key is checked with this first).
is_32_bytes() { local re='^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$'; [[ "$1" =~ $re ]]; }

public_key() {  # the public key in the repository, base64
  [[ -f "$PUBLIC_KEY_FILE" ]] || return 1
  tr -d ' \r\n' < "$PUBLIC_KEY_FILE"
}

python_cmd() {
  if python3 -c '' >/dev/null 2>&1; then echo python3
  elif python -c '' >/dev/null 2>&1; then echo python
  fi
}

# Makes RELEASE_SEED (the private key) and RELEASE_PUBLIC, in memory only. A public key the
# privacy hook would refuse to commit (a digit run like a card number, a word from .git-denylist:
# rare, but random) is thrown away for another.
new_key() {
  local der tries=0 py rc
  py=$(python_cmd)
  [[ -f scripts/hooks/privacy_check.py ]] || py=""
  while :; do
    tries=$((tries + 1))
    [[ $tries -le 20 ]] || fail "Couldn't make a release key the privacy hook would let through."
    der=$("$OPENSSL" genpkey -algorithm ed25519 -outform DER | "$OPENSSL" base64 -A)   # PKCS#8: 16 bytes, then the key
    RELEASE_SEED=$(printf '%s' "$der" | "$OPENSSL" base64 -d -A | tail -c 32 | "$OPENSSL" base64 -A)
    RELEASE_PUBLIC=$(printf '%s' "$der" | "$OPENSSL" base64 -d -A |
      "$OPENSSL" pkey -inform DER -pubout -outform DER | tail -c 32 | "$OPENSSL" base64 -A)
    der=""
    is_32_bytes "$RELEASE_SEED" && is_32_bytes "$RELEASE_PUBLIC" ||
      fail "OpenSSL made a key of the wrong size. Nothing was saved."
    [[ "$(printf '%s' "$RELEASE_SEED" | public_of)" == "$RELEASE_PUBLIC" ]] ||
      fail "The key doesn't match its public half. Nothing was saved."
    [[ -z "$py" ]] && return 0
    rc=0
    printf '%s\n' "$RELEASE_PUBLIC" | "$py" scripts/hooks/privacy_check.py >/dev/null 2>&1 || rc=$?
    [[ $rc -eq 1 ]] || return 0   # 1: it found something; anything else, the hook would say so too
  done
}

# Only for selftest's throwaway keys, which may touch the disk. (Release runs sign on GitHub.)
sign_with() {  # PRIVATE FILE → base64 signature
  { private_der_prefix; printf '%s' "$1" | "$OPENSSL" base64 -d -A; } > "$SELFTEST_DIR/key.der"
  "$OPENSSL" pkeyutl -sign -rawin -keyform DER -inkey "$SELFTEST_DIR/key.der" -in "$2" | "$OPENSSL" base64 -A
  rm -f "$SELFTEST_DIR/key.der"
}
verify_with() {  # PUBLIC FILE SIGNATURE
  { public_der_prefix; printf '%s' "$1" | "$OPENSSL" base64 -d -A; } > "$SELFTEST_DIR/public.der"
  printf '%s' "$3" | "$OPENSSL" base64 -d -A > "$SELFTEST_DIR/sig"
  "$OPENSSL" pkeyutl -verify -rawin -pubin -keyform DER -inkey "$SELFTEST_DIR/public.der" \
    -in "$2" -sigfile "$SELFTEST_DIR/sig" >/dev/null 2>&1
}

# ── The clipboard ─────────────────────────────────────────────────────────

has_clipboard() { command -v pbcopy >/dev/null 2>&1 || command -v clip.exe >/dev/null 2>&1; }
clipboard() {  # stdin → the clipboard
  if command -v pbcopy >/dev/null 2>&1; then pbcopy; else clip.exe; fi
}
clipboard_text() {
  if command -v pbpaste >/dev/null 2>&1; then pbpaste
  elif command -v powershell.exe >/dev/null 2>&1; then powershell.exe -NoProfile -Command Get-Clipboard | tr -d '\r\n'
  fi
}
clear_clipboard() {
  [[ -n "$CLIPBOARD_HAS_SECRET" ]] || return 0
  clipboard </dev/null >/dev/null 2>&1 || true
  if [[ -n "$(clipboard_text 2>/dev/null)" ]]; then printf ' ' | clipboard >/dev/null 2>&1 || true; fi
  CLIPBOARD_HAS_SECRET=""
}
# A setting of Windows' clipboard (HKCU\Software\Microsoft\Clipboard): 0x0, 0x1, or nothing.
clipboard_setting() {
  command -v reg.exe >/dev/null 2>&1 || return 0
  MSYS_NO_PATHCONV=1 reg.exe query 'HKCU\Software\Microsoft\Clipboard' /v "$1" 2>/dev/null |
    awk -v n="$1" '$1 == n {print $3}'
}
# What keeps a copy of what's copied, by its name in Settings → System → Clipboard (nothing when
# nothing does): clipboard history (Win+V), and its sync to other devices, which uploads it.
clipboard_keeps() {
  local kept=""
  if [[ "$(clipboard_setting EnableClipboardHistory)" == 0x1 ]]; then kept="Clipboard history"; fi
  if [[ "$(clipboard_setting EnableCloudClipboard)" == 0x1 &&
        "$(clipboard_setting CloudClipboardAutomaticUpload)" != 0x0 ]]; then
    kept="${kept:+$kept and }Clipboard history across your devices"
  fi
  printf '%s' "$kept"
}
# Before a secret goes on the clipboard: nothing may keep a copy of it.
clipboard_safe() {
  local kept
  while kept=$(clipboard_keeps); [[ -n "$kept" ]]; do
    warn "Windows keeps a copy of everything copied: $kept is on."
    step "Turn it off in Settings → System → Clipboard. You can turn it on again afterwards."
    pause "Press Return once it's off"
  done
}

# Without a clipboard tool: the key on a screen of its own, gone after Return (not in scrollback).
# Only ever on a terminal: piped or redirected (key | tee log), it would be written down.
show_once() {
  [[ -t 1 ]] || fail "There's no clipboard tool here, and the key is only ever shown on a terminal: run this without | or >."
  tput smcup 2>/dev/null || true
  _clear
  printf '\n  The release key. Copy it into the item; it goes when you press Return:\n\n    %s\n\n' "$RELEASE_SEED"
  read -r _ || true
  _clear
  tput rmcup 2>/dev/null || true
}

# ── The Apple Development certificate (macOS's own tools) ─────────────────

MAC_OPENSSL=/usr/bin/openssl   # LibreSSL: reads Keychain Access's .p12 files as they are
TEAM_RE="OU *= *$TEAM([,/ ]|\$)"

# The team's valid Apple Development identity that lasts longest: IDENTITY (its SHA-1),
# IDENTITY_ENDS (when it expires), IDENTITY_CERT (its certificate, not secret).
team_identity() {
  local hashes h cert subject end epoch best=-1
  IDENTITY=""; IDENTITY_ENDS=""; IDENTITY_CERT=""
  hashes=$(security find-identity -v -p codesigning 2>/dev/null | awk '/"Apple Development/ {print $2}') || true
  for h in $hashes; do
    cert=$(security find-certificate -a -c "Apple Development" -Z -p 2>/dev/null |
      awk -v want="$h" '/^SHA-1 hash:/ {hash=$3} /BEGIN CERT/ {if (hash == want) keep=1} keep {print} /END CERT/ {if (keep) exit}') || true
    [[ -n "$cert" ]] || continue
    subject=$(printf '%s\n' "$cert" | "$MAC_OPENSSL" x509 -noout -subject 2>/dev/null) || continue
    [[ "$subject" =~ $TEAM_RE ]] || continue
    end=$(printf '%s\n' "$cert" | "$MAC_OPENSSL" x509 -noout -enddate 2>/dev/null) || continue
    end=${end#notAfter=}
    epoch=$(date -j -f "%b %e %T %Y %Z" "$end" +%s 2>/dev/null) || epoch=0
    if [[ $epoch -gt $best ]]; then best=$epoch; IDENTITY=$h; IDENTITY_ENDS=$end; IDENTITY_CERT=$cert; fi
  done
  [[ -n "$IDENTITY" ]]
}
valid_30_days() { printf '%s\n' "$IDENTITY_CERT" | "$MAC_OPENSSL" x509 -noout -checkend $((30 * 86400)) >/dev/null 2>&1; }

RENEW="Xcode → Settings → Accounts → your Apple ID → Manage Certificates… → + → Apple Development"

# p12 ARGS...: openssl pkcs12 on the export, its password on stdin. OpenSSL 3 needs -legacy for
# Keychain Access's files, so macOS's own LibreSSL goes first.
p12() {
  local o
  printf '%s\n' "$P12_PASSWORD" | "$MAC_OPENSSL" pkcs12 -in "$P12" -passin stdin "$@" 2>&1 && return 0
  for o in /opt/homebrew/opt/openssl@3/bin/openssl /usr/local/opt/openssl@3/bin/openssl; do
    [[ -x "$o" ]] || continue
    printf '%s\n' "$P12_PASSWORD" | "$o" pkcs12 -legacy -in "$P12" -passin stdin "$@" 2>&1 && return 0
  done
  return 1
}

# ── What's done and what's left ───────────────────────────────────────────

status() {
  need_gh
  local secrets s when pub need_key=0 need_mac=0
  printf '\n  %sRelease secrets: the %s environment of %s%s\n\n' "$BOLD" "$ENVIRONMENT" "$REPO" "$RESET"
  if environment_exists; then show_environment || need_key=1
  else bad "There's no $ENVIRONMENT environment on GitHub yet"; need_key=1; fi
  secrets=$(secret_list) || fail "$secrets"
  for s in RELEASE_KEY MAC_CERTIFICATE MAC_CERTIFICATE_PASSWORD; do
    when=$(printf '%s\n' "$secrets" | awk -F'\t' -v n="$s" '$1 == n {print $2}')
    if [[ -n "$when" ]]; then ok "$s is set (${when%%T*})"
    else
      bad "$s isn't set"
      if [[ "$s" == RELEASE_KEY ]]; then need_key=1; else need_mac=1; fi
    fi
  done
  if pub=$(public_key) && is_32_bytes "$pub"; then
    ok "The release key's public half is in $PUBLIC_KEY_FILE"
    if [[ -n "$(git status --porcelain -- "$PUBLIC_KEY_FILE" 2>/dev/null)" ]]; then
      warn "$PUBLIC_KEY_FILE isn't committed yet (Claude commits it)"
    fi
  else
    bad "No public key in $PUBLIC_KEY_FILE"; need_key=1
  fi
  if [[ "$(uname -s)" == Darwin ]]; then
    if ! team_identity; then warn "No valid Apple Development certificate of team $TEAM on this Mac"
    elif valid_30_days; then ok "This Mac's Apple Development certificate (team $TEAM) is valid until $IDENTITY_ENDS"
    else warn "This Mac's Apple Development certificate expires on $IDENTITY_ENDS: renew it ($RENEW)"; fi
  fi
  printf '\n'
  if [[ $need_key -eq 0 && $need_mac -eq 0 ]]; then ok "Everything a release run needs is set up."; printf '\n'; return 0; fi
  say "Still to do:"
  [[ $need_key -eq 0 ]] || step "scripts/release-secrets.sh key           (on any computer)"
  [[ $need_mac -eq 0 ]] || step "scripts/release-secrets.sh certificate   (on the Mac)"
  printf '\n'
}

# ── Part 1, on any computer: the release environment and the release key ──

stage_environment() {
  stage "The release environment on GitHub"
  say "Release runs can reach the secrets only through a GitHub environment, \"$ENVIRONMENT\":"
  step "$MAINTAINER is its required reviewer: every release run waits for your Approve."
  step "Prevent self-reviews is off: Claude starts release runs with your own gh login,"
  say  "  so the Approve is yours to give."
  step "Administrators can't bypass it: no run skips the Approve."
  step "It deploys from main only."
  printf '\n'
  if environment_exists; then note "It's there already; this puts any setting that changed back."; fi
  if confirm "Set up the $ENVIRONMENT environment like this?"; then
    setup_environment
    printf '\n'
    show_environment || warn "Something above isn't as it should be: tell Claude. The rest can go on."
    note "GitHub shows it in the repository's Settings → Environments → $ENVIRONMENT."
  else
    environment_exists || fail "The secrets go into that environment, so this stops here."
    SKIPPED+=("the $ENVIRONMENT environment's settings (scripts/release-secrets.sh key)")
  fi
  pause "Press Return to go on"
}

stage_new_key() {
  stage "Make the release key"
  say "The release key signs every Windows installer; Windows copies take an update only"
  say "when it's signed with it. It's made here, in memory: nothing is written to disk."
  if [[ -n "$NEW_KEY" ]]; then
    printf '\n'
    warn "This makes a new key in place of the one on GitHub. Windows copies refuse a release"
    warn "signed with it: each would need the next release installed by hand once."
    confirm "Every copy of the old key is lost: make a new one?" || fail "Nothing was changed."
  fi
  new_key
  printf '\n'
  ok "Made a new Ed25519 release key"
  note "Its public half, which isn't secret: $RELEASE_PUBLIC"
  pause "Press Return to go on"
}

stage_password_manager() {
  local tries=0 pasted
  stage "Keep a copy in your password manager"
  say "Your password manager keeps the only copy outside GitHub. If both were lost, every"
  say "Windows copy would need a release installed by hand once."
  clipboard_safe
  step "In your password manager, make a new item: \"$KEY_ITEM\"."
  if has_clipboard; then
    pause "Press Return to put the key on the clipboard (it isn't shown)"
    printf '%s' "$RELEASE_SEED" | clipboard
    CLIPBOARD_HAS_SECRET=1
    ok "The key is on the clipboard: 44 characters"
    step "Paste it as the item's password, and save the item."
  else
    pause "Press Return to show the key on a screen of its own, to copy it"
    show_once
    step "Paste it as the item's password, and save the item."
  fi
  pause "Press Return once the item is saved"
  clear_clipboard
  printf '\n'
  say "Now a check that the saved copy is right:"
  step "Copy the password from \"$KEY_ITEM\" in your password manager."
  CLIPBOARD_HAS_SECRET=1
  while :; do
    ask_secret PASTED "Paste it here (it isn't shown), then Return:"
    pasted=$(clean_paste "$PASTED"); PASTED=""
    [[ "$pasted" == "$RELEASE_SEED" ]] && break
    tries=$((tries + 1))
    [[ $tries -lt 3 ]] || fail "That isn't the key. Nothing was put on GitHub: delete the new item, then run this again for a new key."
    warn "That isn't the key. Check the item's password, copy it again and paste it here."
  done
  pasted=""
  clear_clipboard
  ok "Your password manager's copy is right"
  pause "Press Return to go on"
}

stage_key_to_github() {
  stage "Put the key on GitHub"
  say "The key goes into the $ENVIRONMENT environment's secret RELEASE_KEY, which only an"
  say "approved release run can read. Its public half goes into the repository."
  if has_secret RELEASE_KEY; then
    [[ -n "$NEW_KEY" ]] || fail "RELEASE_KEY was put on GitHub while this ran, so it's left as it is. Delete the new item \"$KEY_ITEM\" in your password manager."
    warn "This replaces the RELEASE_KEY on GitHub."
  fi
  printf '\n'
  confirm "Put the key on GitHub?" ||
    fail "Nothing was put on GitHub. Delete the new item \"$KEY_ITEM\" in your password manager; running this again makes a new key."
  put_secret RELEASE_KEY RELEASE_SEED \
    "Delete the new item \"$KEY_ITEM\" in your password manager, then run this again for a new key."
  RELEASE_SEED=""
  printf '%s\n' "$RELEASE_PUBLIC" > "$PUBLIC_KEY_FILE"
  ok "Wrote its public half to $PUBLIC_KEY_FILE"
  printf '\n'
  say "Tell Claude it's done: Claude commits the public half."
  pause "Press Return to finish"
}

# The key was made already: put the password manager's copy back if GitHub lost it.
stage_key_made_already() {
  local pub seed tries=0
  stage "The release key"
  pub=$(public_key)
  is_32_bytes "$pub" || fail "$PUBLIC_KEY_FILE isn't a public key. Ask Claude."
  ok "It was made already: its public half is in $PUBLIC_KEY_FILE"
  if has_secret RELEASE_KEY; then
    ok "RELEASE_KEY is set in the $ENVIRONMENT environment"
    note "Nothing to do. A new key would cut off every Windows copy (docs/DEVELOPMENT.md → Release secrets)."
    pause "Press Return to finish"
    return
  fi
  bad "RELEASE_KEY isn't on GitHub"
  say "This puts back the copy in your password manager, \"$KEY_ITEM\", once it's"
  say "checked against the public half."
  confirm "Put it back now?" || { SKIPPED+=("RELEASE_KEY (scripts/release-secrets.sh key)"); return; }
  clipboard_safe
  step "Copy the password from \"$KEY_ITEM\" in your password manager."
  CLIPBOARD_HAS_SECRET=1
  while :; do
    ask_secret PASTED "Paste it here (it isn't shown), then Return:"
    seed=$(clean_paste "$PASTED"); PASTED=""
    is_32_bytes "$seed" && [[ "$(printf '%s' "$seed" | public_of)" == "$pub" ]] && break
    tries=$((tries + 1))
    [[ $tries -lt 3 ]] || fail "That isn't the release key whose public half is in $PUBLIC_KEY_FILE. Nothing was put on GitHub."
    warn "That isn't the release key whose public half is in $PUBLIC_KEY_FILE. Copy it again."
  done
  clear_clipboard
  RELEASE_SEED=$seed; seed=""
  put_secret RELEASE_KEY RELEASE_SEED
  RELEASE_SEED=""
  pause "Press Return to finish"
}

NEW_KEY=""   # key --new: a new key in place of the one on GitHub
part_key() {
  case "${1:-}" in
    "") ;;
    --new) NEW_KEY=1 ;;
    *) usage; exit 2 ;;
  esac
  need_gh
  find_openssl || fail "This needs OpenSSL 3, which Git for Windows has (on a Mac: brew install openssl@3)."
  has_clipboard || [[ -t 1 ]] ||
    fail "There's no clipboard tool here, and the key is only ever shown on a terminal: run this without | or >."
  # Before any key is made: one on GitHub is only ever replaced on purpose.
  if [[ -z "$NEW_KEY" && ! -f "$PUBLIC_KEY_FILE" ]] && has_secret RELEASE_KEY; then
    fail "RELEASE_KEY is on GitHub but $PUBLIC_KEY_FILE isn't here: get it back with git (git checkout -- \"$PUBLIC_KEY_FILE\"). Nothing was changed. A new key cuts off every Windows copy: only if every copy of this one is lost, run scripts/release-secrets.sh key --new"
  fi
  if [[ -f "$PUBLIC_KEY_FILE" && -z "$NEW_KEY" ]]; then TOTAL_STAGES=2; else TOTAL_STAGES=4; fi
  banner "Release secrets, part 1: the release environment and the release key"
  stage_environment
  if [[ -f "$PUBLIC_KEY_FILE" && -z "$NEW_KEY" ]]; then
    stage_key_made_already
  else
    stage_new_key
    stage_password_manager
    stage_key_to_github
  fi
  finish
  status
}

# ── Part 2, on the Mac: the Apple Development certificate ─────────────────

stage_certificate() {
  stage "Your Apple Development certificate"
  say "Release runs sign the Mac app with your Apple Development certificate: installed Mac"
  say "copies take updates signed by team $TEAM only."
  printf '\n'
  team_identity || fail "No valid Apple Development certificate of team $TEAM in this Mac's keychain. Make one ($RENEW), then run this again."
  valid_30_days || fail "It expires on $IDENTITY_ENDS, within 30 days, and release runs refuse it. Renew it first ($RENEW), then run this again."
  ok "Found it: team $TEAM, valid until $IDENTITY_ENDS"
  if has_secret MAC_CERTIFICATE; then note "There's one on GitHub already: this replaces it (as when renewing)."; fi
  pause "Press Return to go on"
}

export_steps() {
  printf '%s' "$EXPORT_DIR" | pbcopy
  open -a "Keychain Access" 2>/dev/null || warn "Open Keychain Access yourself (Spotlight: Keychain Access)."
  step "In Keychain Access, click login on the left, then My Certificates at the top."
  step "Right-click \"Apple Development: …\", the one that expires on $IDENTITY_ENDS,"
  say  "  and choose Export \"Apple Development: …\"…"
  step "In the Save dialog press ⌘⇧G, then ⌘V (the folder is on the clipboard), then Return."
  step "File Format: Personal Information Exchange (.p12). Click Save."
  pause "When it asks for a password, come back here and press Return"
  printf '%s' "$P12_PASSWORD" | pbcopy
  CLIPBOARD_HAS_SECRET=1
  ok "A password for the .p12 is on the clipboard. It's made up here: you never need it again."
  step "Paste it into Password and into Verify (⌘V in each), then click OK."
  step "If macOS asks for the login keychain's password, that's your Mac's own: type it"
  say  "  there (never here), then click Allow."
  pause "Press Return once Keychain Access has saved it"
  clear_clipboard
}

# Certificate exports saved since the export began (EXPORT_DIR/.started) anywhere but its folder:
# where Keychain Access's Save dialog may have been when ⌘⇧G was missed.
strays() {
  local d
  {
    find "$HOME" -maxdepth 1 -type f \( -iname '*.p12' -o -iname '*.pfx' \) -newer "$EXPORT_DIR/.started" 2>/dev/null || true
    for d in "$HOME/Desktop" "$HOME/Documents" "$HOME/Downloads" "$HOME/Library/Mobile Documents/com~apple~CloudDocs" "$PWD"; do
      [[ -d "$d" ]] || continue
      find "$d" -maxdepth 4 -type f \( -iname '*.p12' -o -iname '*.pfx' \) -newer "$EXPORT_DIR/.started" 2>/dev/null || true
    done
  } | awk -v dir="$EXPORT_DIR/" 'index($0, dir) != 1' | sort -u
}

stage_export() {
  local f n found
  stage "Export it from Keychain Access"
  say "It's exported as a .p12 file into a folder of its own, outside iCloud, which this"
  say "deletes once it's on GitHub (or if you stop)."
  printf '\n'
  EXPORT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/inab-certificate.XXXXXX") || fail "Couldn't make a folder for the export."
  : > "$EXPORT_DIR/.started"
  while :; do
    P12_PASSWORD=$("$MAC_OPENSSL" rand -base64 24)   # a new one each time: a stray copy's goes nowhere
    export_steps
    P12=""; n=0
    for f in "$EXPORT_DIR"/*.p12; do
      if [[ -f "$f" ]]; then P12=$f; n=$((n + 1)); fi
    done
    [[ $n -eq 1 ]] && break
    rm -f "$EXPORT_DIR"/*.p12
    if [[ $n -gt 1 ]]; then warn "There was more than one .p12 in the folder."
    else
      warn "There's no .p12 in the folder: Keychain Access saved it where its Save dialog was."
      found=$(strays) || true
      if [[ -n "$found" ]]; then
        while IFS= read -r f; do note "  $f"; done <<<"$found"
        if confirm "It holds the certificate's private key. Delete it?"; then
          while IFS= read -r f; do rm -f -- "$f"; done <<<"$found"
          ok "Deleted"
        else warn "Delete it yourself, and from iCloud Drive's Recently Deleted if it was there."; fi
      else
        warn "Find the .p12 it saved just now (Finder → Recents) and delete it: it holds the certificate's private key."
      fi
    fi
    confirm "Export it again?" || fail "Nothing was put on GitHub."
  done
  ok "Saved: $(basename "$P12")"
  pause "Press Return to go on"
}

stage_check_p12() {
  local cert subject fp info
  stage "Check the .p12"
  cert=$(p12 -nokeys -clcerts) ||
    fail "Couldn't open the .p12 with its password. Nothing was put on GitHub; run this again."
  subject=$(printf '%s\n' "$cert" | "$MAC_OPENSSL" x509 -noout -subject 2>/dev/null) || fail "There's no certificate in the .p12."
  [[ "$subject" =~ $TEAM_RE ]] || fail "The .p12's certificate isn't team $TEAM's. Nothing was put on GitHub."
  fp=$(printf '%s\n' "$cert" | "$MAC_OPENSSL" x509 -noout -fingerprint -sha1 2>/dev/null)
  fp=$(printf '%s' "${fp#*=}" | tr -d ':' | tr '[:lower:]' '[:upper:]')
  [[ "$fp" == "$IDENTITY" ]] ||
    fail "That's another certificate. Nothing was put on GitHub: run this again and export the one that expires on $IDENTITY_ENDS."
  ok "It's the certificate that expires on $IDENTITY_ENDS, team $TEAM"
  info=$(p12 -info -noout) || info=""
  case "$info" in
    *[Kk]ey*[Bb]ag*) ok "It has the certificate's private key" ;;
    *) fail "It has no private key: export it from My Certificates. Nothing was put on GitHub." ;;
  esac
  pause "Press Return to go on"
}

stage_certificate_to_github() {
  stage "Put it on GitHub"
  say "The .p12 and its password go into the $ENVIRONMENT environment's secrets MAC_CERTIFICATE"
  say "and MAC_CERTIFICATE_PASSWORD, which only an approved release run can read. Then the"
  say ".p12 is deleted."
  printf '\n'
  confirm "Put the certificate on GitHub?" || fail "Nothing was put on GitHub; the .p12 is deleted."
  P12_BASE64=$(base64 -i "$P12" | tr -d '\n')
  put_secret MAC_CERTIFICATE P12_BASE64
  put_secret MAC_CERTIFICATE_PASSWORD P12_PASSWORD
  P12_BASE64=""; P12_PASSWORD=""
  cleanup
  ok "Deleted the .p12 and its folder"
  pause "Press Return to finish"
}

part_certificate() {
  [[ "$(uname -s)" == Darwin ]] || fail "This part runs on the Mac with the certificate in its keychain."
  need_gh
  environment_exists || fail "There's no $ENVIRONMENT environment on GitHub yet: run scripts/release-secrets.sh key first."
  TOTAL_STAGES=4
  banner "Release secrets, part 2: the Apple Development certificate"
  stage_certificate
  stage_export
  stage_check_p12
  stage_certificate_to_github
  finish
  status
}

# ── selftest: the formats, with RFC 8032's test vector and throwaway keys ──

selftest() {
  local sig other
  find_openssl || fail "This needs OpenSSL 3, which Git for Windows has (on a Mac: brew install openssl@3)."
  SELFTEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/inab-selftest.XXXXXX")
  printf '\n  %sThe release key formats, with %s%s\n\n' "$BOLD" "$("$OPENSSL" version)" "$RESET"
  # RFC 8032, section 7.1, TEST 2 (one byte, 0x72: OpenSSL won't sign an empty file): a published
  # key, so the formats are the standard's own.
  local rfc_private="TM0Imyj/ltqdtsNG7BFOD1uKMZ81q6Yk2oz27U+4pvs="
  local rfc_public="PUAXw+hDiVqStwqnTRt+vJyYLM8uxJaMwM1V8Sr0Zgw="
  local rfc_signature="kqAJqfDUyrhyDoILX2QlQKKye1QWUD+Ps3YiI+vbadoIWsHkPhWZbkWPNhPQ8R2MOHsurrQwKu6wDSkWErsMAA=="
  [[ "$(printf '%s' "$rfc_private" | public_of)" == "$rfc_public" ]] || fail "RFC 8032's test key gives another public key."
  printf 'r' > "$SELFTEST_DIR/rfc"
  [[ "$(sign_with "$rfc_private" "$SELFTEST_DIR/rfc")" == "$rfc_signature" ]] || fail "RFC 8032's test signature differs."
  verify_with "$rfc_public" "$SELFTEST_DIR/rfc" "$rfc_signature" || fail "RFC 8032's test signature is refused."
  ok "RFC 8032's test vector: the same public key and signature"
  # Throwaway keys, made the way part 1 makes the real one.
  new_key
  printf 'A made-up installer, version 9.9\n' > "$SELFTEST_DIR/Setup.exe"
  sig=$(sign_with "$RELEASE_SEED" "$SELFTEST_DIR/Setup.exe")
  [[ ${#sig} -eq 88 ]] || fail "The signature isn't 64 bytes."
  verify_with "$RELEASE_PUBLIC" "$SELFTEST_DIR/Setup.exe" "$sig" || fail "A good signature was refused."
  ok "A throwaway key signs a file, and its public half accepts it"
  printf 'changed' >> "$SELFTEST_DIR/Setup.exe"
  ! verify_with "$RELEASE_PUBLIC" "$SELFTEST_DIR/Setup.exe" "$sig" || fail "A changed file was accepted."
  ok "A changed file is refused"
  printf 'A made-up installer, version 9.9\n' > "$SELFTEST_DIR/Setup.exe"
  other=$RELEASE_PUBLIC
  new_key
  ! verify_with "$RELEASE_PUBLIC" "$SELFTEST_DIR/Setup.exe" "$sig" || fail "Another key's public half accepted it."
  verify_with "$other" "$SELFTEST_DIR/Setup.exe" "$sig" || fail "The file put back was refused."
  ok "Another key's public half refuses it"
  RELEASE_SEED=""
  printf '\n'
}

case "${1:-check}" in
  key) part_key "${2:-}" ;;
  certificate) part_certificate ;;
  check) status ;;
  selftest) selftest ;;
  -h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
