#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKIP_ADB=0
NO_START=0
APT_UPDATED=0

usage() {
  cat <<'EOF'
Usage: ./install-unix.sh [--skip-adb] [--no-start]

Installs missing prerequisites, creates the locked Python environment, builds the
dashboard, and starts TheDoPixel on macOS or Linux.

  --skip-adb  Do not install Android Platform Tools (for FTP-only setups)
  --no-start  Complete installation without starting TheDoPixel
  -h, --help  Show this help
EOF
}

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

run_privileged() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    fail "Administrator access is required to install system packages (sudo was not found)."
  fi
}

refresh_path() {
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
  hash -r
}

ensure_curl() {
  command -v curl >/dev/null 2>&1 || fail "curl is required to install missing prerequisites."
}

ensure_homebrew() {
  refresh_path
  if command -v brew >/dev/null 2>&1; then
    return
  fi
  ensure_curl
  printf 'Installing Homebrew...\n'
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  refresh_path
  command -v brew >/dev/null 2>&1 || fail "Homebrew installed but is not available on PATH. Open a new terminal and rerun this script."
}

linux_package_manager() {
  for manager in apt-get dnf yum pacman zypper apk; do
    if command -v "$manager" >/dev/null 2>&1; then
      printf '%s\n' "$manager"
      return
    fi
  done
  fail "No supported Linux package manager was found (apt, dnf, yum, pacman, zypper, or apk)."
}

linux_install() {
  manager="$1"
  shift
  case "$manager" in
    apt-get)
      if [ "$APT_UPDATED" -eq 0 ]; then
        run_privileged apt-get update
        APT_UPDATED=1
      fi
      run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
      ;;
    dnf|yum)
      run_privileged "$manager" install -y "$@"
      ;;
    pacman)
      run_privileged pacman -S --needed --noconfirm "$@"
      ;;
    zypper)
      run_privileged zypper --non-interactive install "$@"
      ;;
    apk)
      run_privileged apk add "$@"
      ;;
  esac
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  if [ "$PLATFORM" = "macos" ]; then
    ensure_homebrew
    brew install uv
  else
    ensure_curl
    printf 'Installing uv...\n'
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  refresh_path
  command -v uv >/dev/null 2>&1 || fail "uv installed but is not available on PATH. Open a new terminal and rerun this script."
}

install_node() {
  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    return
  fi
  if [ "$PLATFORM" = "macos" ]; then
    ensure_homebrew
    brew install node
  else
    manager="$(linux_package_manager)"
    linux_install "$manager" nodejs npm
  fi
  refresh_path
  command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 || fail "Node.js/npm could not be installed."
}

validate_node() {
  if ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)'; then
    fail "Node.js 18 or newer is required. Upgrade Node.js and rerun this script."
  fi
}

install_adb() {
  if command -v adb >/dev/null 2>&1; then
    return
  fi
  if [ "$PLATFORM" = "macos" ]; then
    ensure_homebrew
    brew install --cask android-platform-tools
  else
    manager="$(linux_package_manager)"
    case "$manager" in
      apt-get) linux_install "$manager" adb ;;
      *) linux_install "$manager" android-tools ;;
    esac
  fi
  refresh_path
  command -v adb >/dev/null 2>&1 || fail "Android Platform Tools installed but adb is not available on PATH."
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-adb) SKIP_ADB=1 ;;
    --no-start) NO_START=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "Unknown option: $1"
      ;;
  esac
  shift
done

case "$(uname -s)" in
  Darwin) PLATFORM="macos" ;;
  Linux) PLATFORM="linux" ;;
  *) fail "This installer supports macOS and Linux. Use install-windows.ps1 on Windows." ;;
esac

printf 'TheDoPixel setup for %s\n' "$PLATFORM"
refresh_path
install_uv
install_node
validate_node
if [ "$SKIP_ADB" -eq 0 ]; then
  install_adb
fi

cd "$PROJECT_DIR"
printf 'Creating the Python environment...\n'
uv sync --frozen

printf 'Installing and building the dashboard...\n'
npm --prefix "$PROJECT_DIR/frontend" ci
npm --prefix "$PROJECT_DIR/frontend" run build

if [ ! -f "$PROJECT_DIR/.env" ]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
fi
chmod +x "$PROJECT_DIR/pixel-relay"

printf 'TheDoPixel installation is complete.\n'
printf 'Start it later with: %s/pixel-relay\n' "$PROJECT_DIR"
if [ "$NO_START" -eq 0 ]; then
  exec "$PROJECT_DIR/pixel-relay"
fi
