#!/usr/bin/env bash
# Save a job posting URL as a SingleFile-style self-contained HTML archive.
#
# Usage:
#   ./save_singlefile.sh "https://example.com/job" "/path/to/output.html"
#
# Uses the SingleFile CLI through npx. If Chrome/Chromium is installed in a common
# location, it is passed explicitly so the capture uses a real browser engine.
set -euo pipefail

url="${1:-}"
output="${2:-}"

if [ -z "$url" ] || [ -z "$output" ]; then
  echo "Usage: save_singlefile.sh <url> <output.html>" >&2
  exit 1
fi

# `D:/x` means different things to the two bash flavours that run this script.
# Under WSL the drive is mounted at /mnt/d; under Git Bash there is no /mnt at
# all and a drive-letter path is already understood as-is. Rewriting to /mnt
# unconditionally is what broke it: Git Bash then tried `mkdir -p /mnt/d/...`
# and died with "cannot create directory '/mnt': Permission denied".
# `wslpath` exists only under WSL, so it is the test for which world we are in.
normalize_output_path() {
  local p="$1"
  if ! command -v wslpath >/dev/null 2>&1; then
    printf '%s\n' "$p"
    return
  fi
  if [[ "$p" =~ ^([A-Za-z]):[/\\](.*)$ ]]; then
    local drive="${BASH_REMATCH[1],,}"
    local rest="${BASH_REMATCH[2]//\\//}"
    printf '/mnt/%s/%s\n' "$drive" "$rest"
    return
  fi
  printf '%s\n' "$p"
}

output="$(normalize_output_path "$output")"

if ! command -v npx >/dev/null 2>&1; then
  echo "ERROR: npx not found. Install Node.js or use manual browser SingleFile capture." >&2
  exit 1
fi

find_browser() {
  command -v google-chrome 2>/dev/null || \
    command -v google-chrome-stable 2>/dev/null || \
    command -v chromium 2>/dev/null || \
    command -v chromium-browser 2>/dev/null || \
    command -v chrome.exe 2>/dev/null || true
}

win_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  elif command -v wslpath >/dev/null 2>&1; then
    wslpath -w "$1"
  else
    printf '%s\n' "$1"
  fi
}

POWERSHELL="$(command -v powershell.exe 2>/dev/null || true)"
if [ -n "$POWERSHELL" ] && command -v wslpath >/dev/null 2>&1; then
  work="$(mktemp -d /tmp/sf.XXXXXX)"
  trap 'rm -rf "$work"' EXIT
  ps1="$work/save-singlefile.ps1"
  cat > "$ps1" <<'PS1'
param(
  [Parameter(Mandatory=$true)][string]$Url,
  [Parameter(Mandatory=$true)][string]$Output
)
$ErrorActionPreference = "Stop"
$parent = Split-Path -Parent $Output
if ($parent -and -not (Test-Path -LiteralPath $parent)) {
  New-Item -ItemType Directory -Path $parent | Out-Null
}
& npx.cmd -y single-file-cli $Url $Output `
  --browser-headless=true `
  --browser-width=1440 `
  --browser-height=1000 `
  --browser-wait-until=networkIdle `
  --browser-wait-delay=2500 `
  --browser-load-max-time=90000 `
  --browser-capture-max-time=90000 `
  --block-scripts=false `
  --block-images=false `
  --filename-conflict-action=overwrite
exit $LASTEXITCODE
PS1
  "$POWERSHELL" -NoProfile -ExecutionPolicy Bypass -File "$(win_path "$ps1")" \
    -Url "$url" \
    -Output "$(win_path "$output")"
  if [ ! -s "$output" ]; then
    echo "ERROR: SingleFile capture did not create a non-empty output file: $output" >&2
    exit 1
  fi
  echo "HTML: $output"
  exit 0
fi

browser="$(find_browser)"
if [ -z "$browser" ]; then
  for candidate in \
    "C:/Program Files/Google/Chrome/Application/chrome.exe" \
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe" \
    "/c/Program Files/Google/Chrome/Application/chrome.exe" \
    "/c/Program Files (x86)/Google/Chrome/Application/chrome.exe" \
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" \
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"; do
    if [ -f "$candidate" ]; then
      browser="$candidate"
      break
    fi
  done
fi

mkdir -p "$(dirname "$output")"

args=(
  -y single-file-cli
  "$url"
  "$output"
  --browser-headless=true
  --browser-width=1440
  --browser-height=1000
  --browser-wait-until=networkIdle
  --browser-wait-delay=2500
  --browser-load-max-time=90000
  --browser-capture-max-time=90000
  --block-scripts=false
  --block-images=false
  --filename-conflict-action=overwrite
)

if [ -n "$browser" ]; then
  args+=(--browser-executable-path="$browser")
fi

npx "${args[@]}"

if [ ! -s "$output" ]; then
  echo "ERROR: SingleFile capture did not create a non-empty output file: $output" >&2
  exit 1
fi

echo "HTML: $output"
