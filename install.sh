#!/usr/bin/env bash
# Bootstrap only: configuration and managed releases live in `talaria setup`.
# Parse the complete script before executing when invoked through curl | bash.
talaria_bootstrap() {
set -euo pipefail
umask 077

fail() { printf 'Talaria: %s\n' "$*" >&2; exit 1; }
install_deps=false
interactive=true
repository=https://github.com/liamsmith86/talaria-webui.git
branch=main
forward=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-deps) install_deps=true ;;
        --non-interactive) interactive=false; forward+=("$1") ;;
        --repository|--branch)
            [ "$#" -ge 2 ] || fail "$1 needs a value"
            case "$1" in --repository) repository=$2 ;; --branch) branch=$2 ;; esac
            shift ;;
        --help|-h)
            cat <<'HELP'
Usage: bash install.sh [--install-deps] [setup options]
Linux, macOS, and WSL2; root is optional. No automatic updates.

  --non-interactive             Use defaults without prompts
  --install-deps                Allow installing missing Git, uv, and Python
  --repository URL --branch REF Trusted source (default: Talaria main)
  --directory PATH --config PATH
  --bind local|lan|all          Default: local; --host IP overrides
  --port PORT --public-url URL  Public URL may include /talaria
  --password-file PATH         Read a chosen WebUI password (12+ characters)
  --hermes-home PATH --hermes-python PATH
  --hermes-url URL --hermes-key-file PATH
  --enable-hermes-api           Enable local Hermes API; reuse or create its key
  --plugin                     Install and enable the bundled Hermes plugin
  --restart-hermes              Restart the selected local gateway
  --skip-hermes                 Configure connectivity in the WebUI later
  --service auto|none|systemd|launchd  Default: prompt, or none headlessly

Secrets belong in private files, never command-line arguments.
HELP
            exit 0 ;;
        --enable-hermes-api|--plugin|--restart-hermes|--skip-hermes) forward+=("$1") ;;
        --directory|--config|--bind|--host|--port|--public-url|--password-file|--hermes-home|--hermes-python|--hermes-url|--hermes-key-file|--service)
            [ "$#" -ge 2 ] || fail "$1 needs a value"
            forward+=("$1" "$2"); shift ;;
        *) fail "Unknown option: $1 (see --help)" ;;
    esac
    shift
done
case "$repository" in -*|*$'\n'*|*$'\r'*) fail 'Invalid repository' ;; esac
case "$(uname -s)" in
    Linux|Darwin) ;;
    *) fail 'Use Linux, macOS, or Ubuntu inside WSL2 on Windows.' ;;
esac
confirm() {
    $install_deps && return 0
    $interactive || fail "$1 Re-run with --install-deps or install it yourself."
    local answer
    printf '%s [y/N] ' "$1" >/dev/tty || fail 'No terminal; use --non-interactive.'
    read -r answer </dev/tty || fail 'Input closed.'
    case "$answer" in y|Y|yes|YES) return 0 ;; *) fail 'Dependency installation declined.' ;; esac
}
elevated() {
    if [ "$(id -u)" -eq 0 ]; then "$@"
    elif command -v sudo >/dev/null 2>&1; then sudo "$@"
    else fail 'Install the missing system packages as administrator, then re-run.'; fi
}
if ! command -v git >/dev/null 2>&1 ||
   { ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; }; then
    confirm 'Install Git, curl, and CA certificates using the system package manager?'
    if [ "$(uname -s)" = Darwin ]; then
        [ "$(id -u)" -ne 0 ] || fail 'Install Git/curl using your normal macOS account first.'
        command -v brew >/dev/null 2>&1 || fail 'Install Apple command-line tools (xcode-select --install), then re-run.'
        brew install git curl
    elif command -v apt-get >/dev/null 2>&1; then
        elevated apt-get update
        elevated apt-get install -y git curl ca-certificates
    elif command -v dnf >/dev/null 2>&1; then elevated dnf install -y git curl ca-certificates
    elif command -v yum >/dev/null 2>&1; then elevated yum install -y git curl ca-certificates
    elif command -v apk >/dev/null 2>&1; then elevated apk add git curl ca-certificates
    elif command -v pacman >/dev/null 2>&1; then elevated pacman -S --needed --noconfirm git curl ca-certificates
    elif command -v zypper >/dev/null 2>&1; then elevated zypper --non-interactive install git curl ca-certificates
    else fail 'Install Git and curl (or wget) with your package manager, then re-run.'; fi
fi
git --version >/dev/null || fail 'Git is unavailable; finish installing your command-line tools.'
work=$(mktemp -d "${TMPDIR:-/tmp}/talaria-setup.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
download() {
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 20 --max-time 300 "$1" -o "$2"
    else wget --https-only --timeout=30 --tries=3 -q "$1" -O "$2"; fi
}
# Include the standard uv location without modifying any shell startup files.
export PATH="${HOME}/.local/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    confirm 'Install pinned uv 0.12.9 from Astral?'
    download https://astral.sh/uv/0.12.9/install.sh "$work/uv.sh"
    expected=222e006c0fe4a0d793031833e469b21df72311f4e3526ffecca0e19e6dfabc32
    if command -v sha256sum >/dev/null 2>&1; then actual=$(sha256sum "$work/uv.sh")
    elif command -v shasum >/dev/null 2>&1; then actual=$(shasum -a 256 "$work/uv.sh")
    else fail 'Install sha256sum or shasum to verify uv.'; fi
    [ "${actual%% *}" = "$expected" ] || fail 'uv installer checksum mismatch; nothing executed.'
    uv_bin="${HOME}/.local/bin"
    [ "$(id -u)" -ne 0 ] || uv_bin=/usr/local/bin
    UV_UNMANAGED_INSTALL="$uv_bin" sh "$work/uv.sh"
fi
# An existing system Python avoids a private-home interpreter in a system service.
can_admin=false
if [ "$(id -u)" -eq 0 ] || { command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; }; then
    can_admin=true
fi
if $can_admin && [ "$(uname -s)" = Linux ]; then
    export UV_PYTHON_INSTALL_DIR=/opt/talaria-python
fi
python_path=$(uv python find --system --no-project --no-python-downloads "${UV_PYTHON:->=3.12}" 2>/dev/null) || python_path=
if $can_admin && [ "$(uname -s)" = Linux ]; then
    case "$python_path" in /root/*|/home/*)
        python_path=$(uv python find --system --no-project --no-managed-python --no-python-downloads '>=3.12' 2>/dev/null) || python_path=
    ;; esac
fi
if [ -z "$python_path" ]; then
    confirm 'Install Python 3.14 with uv (leaves system Python unchanged)?'
    if $can_admin && [ "$(uname -s)" = Linux ]; then
        elevated sh -c 'umask 022; exec "$@"' sh env UV_PYTHON_INSTALL_DIR=/opt/talaria-python "$(command -v uv)" python install 3.14
    else
        (umask 022; uv python install 3.14)
    fi
    python_path=$(uv python find --system --no-project --no-python-downloads 3.14)
fi
"$python_path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || fail 'Python 3.12 or newer is required.'
export UV_PYTHON_DOWNLOADS=never
# A detached checkout pins setup and the resulting deployment to the same commit.
git check-ref-format "refs/heads/$branch" >/dev/null || fail 'Invalid branch name.'
GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 --single-branch --branch "$branch" -- "$repository" "$work/source"
commit=$(git -C "$work/source" rev-parse HEAD)
# Downloaded dependencies use the repository lock and its seven-day cooldown.
# Do not install development dependencies or leave a temporary Python interpreter behind.
uv run --project "$work/source" --python "$python_path" --locked --no-dev talaria setup \
    --repository "$repository" --branch "$branch" --expect "$commit" ${forward[@]+"${forward[@]}"}
}

talaria_bootstrap "$@"
