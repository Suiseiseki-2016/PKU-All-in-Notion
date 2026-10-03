#!/usr/bin/env bash
# Stage the Tauri sidecar and private Python runtime on macOS/Linux.
# Windows uses stage-sidecar.ps1 because its relocatable runtime layout and
# executable suffix differ.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC_TAURI="$REPO_ROOT/desktop/src-tauri"
BINARIES="$SRC_TAURI/binaries"
RUNTIME_LINK="$SRC_TAURI/resources/runtime"
RUNTIME="$RUNTIME_LINK"

DEV_STUB_ONLY=0
SKIP_RUNTIME=0
for arg in "$@"; do
  case "$arg" in
    --dev-stub-only) DEV_STUB_ONLY=1 ;;
    --skip-runtime) SKIP_RUNTIME=1 ;;
    -h|--help)
      echo "Usage: $0 [--dev-stub-only] [--skip-runtime]"
      exit 0
      ;;
  esac
done

mkdir -p "$BINARIES" "$SRC_TAURI/resources"

TRIPLE="${TAURI_ENV_TARGET_TRIPLE:-$(rustc -vV | awk '/^host:/{print $2}')}"
SIDECAR_DEST="$BINARIES/pku-sync-${TRIPLE}"

LAUNCHER="$REPO_ROOT/desktop/sidecar-launcher"

echo "==> Building pku-sync-sidecar for $TRIPLE"
PROFILE=release
CARGO_ARGS=(--release)
if [[ "$DEV_STUB_ONLY" -eq 1 ]]; then
  PROFILE=debug
  CARGO_ARGS=()
fi
(
  cd "$LAUNCHER"
  cargo build "${CARGO_ARGS[@]}"
)
BUILT="${CARGO_TARGET_DIR:-$LAUNCHER/target}/$PROFILE/pku-sync-sidecar"
if [[ ! -f "$BUILT" ]]; then
  echo "missing $BUILT" >&2
  exit 1
fi
cp -f "$BUILT" "$SIDECAR_DEST"
chmod +x "$SIDECAR_DEST"
echo "    staged $SIDECAR_DEST"

if [[ "$DEV_STUB_ONLY" -eq 1 ]]; then
  RUNTIME="$RUNTIME_LINK"
  if [[ -L "$RUNTIME" ]]; then
    rm "$RUNTIME"
  fi
  mkdir -p "$RUNTIME"
  cat > "$RUNTIME/README.txt" <<'EOF'
Dev stub: no portable Python here.
Windows production staging: desktop/scripts/stage-sidecar.ps1
EOF
  echo "==> Done (dev stub)."
  exit 0
fi

if [[ "$SKIP_RUNTIME" -eq 1 ]]; then
  echo "==> Skipping runtime (--skip-runtime)."
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to stage the private Python runtime." >&2
  exit 1
fi

case "$(uname -s)" in
  Darwin)
    PLATFORM_LABEL="macOS"
    # Desktop may be managed by iCloud/File Provider, which can reject copies
    # of the managed Python distribution and attach metadata that breaks code
    # signing. Assemble it on the local temporary volume and expose a stable
    # resource symlink for Tauri to follow.
    RUNTIME="${PKU_MACOS_RUNTIME_DIR:-/private/tmp/pku-all-in-notion-runtime}"
    PYTHON_INSTALL="${PKU_MACOS_PYTHON_INSTALL_DIR:-/private/tmp/pku-all-in-notion-python-install}"
    ;;
  Linux) PLATFORM_LABEL="Linux" ;;
  *) echo "unsupported Unix platform: $(uname -s)" >&2; exit 1 ;;
esac

echo "==> Staging relocatable $PLATFORM_LABEL runtime"
PYTHON_INSTALL="${PYTHON_INSTALL:-$SRC_TAURI/resources/python-install}"
export UV_PYTHON_INSTALL_DIR="$PYTHON_INSTALL"
mkdir -p "$PYTHON_INSTALL"
uv python install 3.11 --install-dir "$PYTHON_INSTALL"
rm -rf "$RUNTIME"
if [[ "$PLATFORM_LABEL" == "macOS" ]]; then
  # uv 0.6's relocatable venv still leaves an absolute `bin/python` symlink
  # and omits libpython/stdlib on macOS. Ship the complete managed interpreter
  # instead; its Mach-O loader path is already @executable_path/../lib.
  PYTHON_HOMES=("$PYTHON_INSTALL"/cpython-3.11*-macos-*)
  if [[ ${#PYTHON_HOMES[@]} -ne 1 || ! -d "${PYTHON_HOMES[0]}" ]]; then
    echo "expected one managed macOS Python under $PYTHON_INSTALL" >&2
    exit 1
  fi
  /usr/bin/ditto --noextattr --noqtn "${PYTHON_HOMES[0]}" "$RUNTIME"
else
  uv venv "$RUNTIME" --python 3.11 --relocatable --clear --link-mode copy
fi
if [[ -d "$REPO_ROOT/dist" ]] && compgen -G "$REPO_ROOT/dist/pku_course_sync-"*-py3-none-any.whl >/dev/null; then
  WHEEL="$(ls -1t "$REPO_ROOT"/dist/pku_course_sync-*-py3-none-any.whl | head -1)"
  if [[ "$PLATFORM_LABEL" == "macOS" ]]; then
    uv pip install --system --break-system-packages --python "$RUNTIME/bin/python" "$WHEEL"
  else
    uv pip install --python "$RUNTIME/bin/python" "$WHEEL"
  fi
else
  if [[ "$PLATFORM_LABEL" == "macOS" ]]; then
    uv pip install --system --break-system-packages --python "$RUNTIME/bin/python" -e "$REPO_ROOT"
  else
    uv pip install --python "$RUNTIME/bin/python" -e "$REPO_ROOT"
  fi
fi
"$RUNTIME/bin/python" -m pku_sync --help >/dev/null
echo ok > "$RUNTIME/.bundle-ok"
if [[ "$PLATFORM_LABEL" == "macOS" ]]; then
  if [[ -e "$RUNTIME_LINK" || -L "$RUNTIME_LINK" ]]; then
    rm -rf "$RUNTIME_LINK"
  fi
  ln -s "$RUNTIME" "$RUNTIME_LINK"
fi
echo "==> Runtime ready: $RUNTIME"
