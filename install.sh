#!/usr/bin/env bash
# =============================================================================
# install.sh — Transparent one-command bootstrap for agent-memory-kb
#
# Safety guarantees:
#   - Supports --dry-run (prints exact plan without touching the filesystem)
#   - NEVER modifies global AI client configs (~/.codex, ~/.claude, ~/.cursor,
#     ~/.gemini) or installs background daemons automatically
#   - Uses strict allowlists for --profile and --ref; cleans temp clones on exit
# =============================================================================
set -euo pipefail

REPO_URL="${AGENT_MEMORY_KB_REPO_URL:-https://github.com/JaydenWuGang123/agent-memory-kb.git}"
KB_ROOT="${AGENT_MEMORY_KB_ROOT:-$HOME/personal-kb}"
PROFILE="dev"
GIT_REF="main"
SOURCE_DIR=""
DRY_RUN=0
SKIP_SYNC=0
FORCE=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Bootstrap a local-first agent-memory-kb repository without touching global
IDE/Agent configs or background daemons.

Options:
  --root <path>        Target knowledge base directory (default: ~/personal-kb)
  --profile <mode>     Template profile: dev | general (default: dev)
  --ref <branch|tag>   Git branch or tag to install (default: main)
  --source-dir <path>  Use an existing local agent-memory-kb checkout (offline)
  --skip-sync          Initialize & run kb_doctor only; skip initial archive sync
  --force              Overwrite starter templates if target already exists
  --dry-run            Show planned actions without writing any files
  -h, --help           Show this help message

Examples:
  curl -fsSL https://raw.githubusercontent.com/JaydenWuGang123/agent-memory-kb/main/install.sh | bash -s -- --profile dev
  curl -fsSL https://raw.githubusercontent.com/JaydenWuGang123/agent-memory-kb/main/install.sh | bash -s -- --profile general --dry-run
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      [[ $# -ge 2 ]] || { echo "[install] Error: --root requires a path" >&2; exit 2; }
      KB_ROOT="$2"
      shift 2
      ;;
    --profile)
      [[ $# -ge 2 ]] || { echo "[install] Error: --profile requires dev or general" >&2; exit 2; }
      PROFILE="$2"
      shift 2
      ;;
    --ref)
      [[ $# -ge 2 ]] || { echo "[install] Error: --ref requires a branch or tag" >&2; exit 2; }
      GIT_REF="$2"
      shift 2
      ;;
    --source-dir)
      [[ $# -ge 2 ]] || { echo "[install] Error: --source-dir requires a directory" >&2; exit 2; }
      SOURCE_DIR="$2"
      shift 2
      ;;
    --skip-sync)
      SKIP_SYNC=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[install] Error: unknown option '$1'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$PROFILE" in
  dev|general) ;;
  *)
    echo "[install] Error: invalid --profile '$PROFILE' (expected: dev or general)" >&2
    exit 2
    ;;
esac

if [[ ! "$GIT_REF" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "$GIT_REF" == *".."* ]]; then
  echo "[install] Error: invalid --ref '$GIT_REF'" >&2
  exit 2
fi

if [[ -z "$KB_ROOT" || "$KB_ROOT" == "/" ]]; then
  echo "[install] Error: refusing unsafe --root '$KB_ROOT'" >&2
  exit 2
fi

# Expand leading ~ safely
if [[ "$KB_ROOT" == "~" ]]; then
  KB_ROOT="$HOME"
elif [[ "$KB_ROOT" == "~/"* ]]; then
  KB_ROOT="$HOME/${KB_ROOT#~/}"
fi

# Resolve Python 3.9+
PYTHON_BIN=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PYTHON_BIN="$(command -v "$cand")"
      break
    fi
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "[install] Error: Python 3.9+ is required." >&2
  exit 1
fi

if [[ -z "$SOURCE_DIR" ]] && ! command -v git >/dev/null 2>&1; then
  echo "[install] Error: git is required to fetch $REPO_URL (or pass --source-dir)." >&2
  exit 1
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[install] DRY-RUN plan (no files will be modified):"
  echo "  - Python:     $PYTHON_BIN"
  if [[ -n "$SOURCE_DIR" ]]; then
    echo "  - Source:     local directory $SOURCE_DIR"
  else
    echo "  - Source:     git clone --depth 1 --branch $GIT_REF $REPO_URL (temporary dir)"
  fi
  echo "  - Target KB:  $KB_ROOT"
  echo "  - Profile:    $PROFILE"
  echo "  - Init cmd:   $PYTHON_BIN init_kb.py --root \"$KB_ROOT\" --profile \"$PROFILE\"$([[ $FORCE -eq 1 ]] && echo ' --force')"
  if [[ "$SKIP_SYNC" -eq 0 ]]; then
    echo "  - First sync: $PYTHON_BIN \"$KB_ROOT/_同步/build_kb_index.py\" --output-root \"$KB_ROOT\" --skip-icloud"
  else
    echo "  - First sync: skipped (--skip-sync)"
  fi
  echo "  - Doctor:     $PYTHON_BIN \"$KB_ROOT/_同步/kb_doctor.py\" --root \"$KB_ROOT\" --skip-external-checks"
  echo "  - Global cfg: untouched (prints manual MCP & Agent Skill next steps only)"
  exit 0
fi

TMP_CLONE=""
cleanup() {
  if [[ -n "$TMP_CLONE" && -d "$TMP_CLONE" ]]; then
    rm -rf "$TMP_CLONE"
  fi
}
trap cleanup EXIT

if [[ -n "$SOURCE_DIR" ]]; then
  if [[ ! -f "$SOURCE_DIR/init_kb.py" ]]; then
    echo "[install] Error: init_kb.py not found in --source-dir '$SOURCE_DIR'" >&2
    exit 1
  fi
  WORK_SRC="$SOURCE_DIR"
else
  TMP_CLONE="$(mktemp -d "${TMPDIR:-/tmp}/agent-memory-kb-install.XXXXXX")"
  echo "[install] Cloning $REPO_URL (ref: $GIT_REF)..."
  git clone --depth 1 --branch "$GIT_REF" "$REPO_URL" "$TMP_CLONE" >/dev/null
  WORK_SRC="$TMP_CLONE"
fi

echo "[install] Initializing knowledge base at $KB_ROOT (profile=$PROFILE)..."
INIT_ARGS=("$WORK_SRC/init_kb.py" "--root" "$KB_ROOT" "--profile" "$PROFILE")
if [[ "$FORCE" -eq 1 ]]; then
  INIT_ARGS+=("--force")
fi
"$PYTHON_BIN" "${INIT_ARGS[@]}"

if [[ "$SKIP_SYNC" -eq 0 ]]; then
  echo "[install] Running initial sync & local FTS5 index build..."
  "$PYTHON_BIN" "$KB_ROOT/_同步/build_kb_index.py" --output-root "$KB_ROOT" --skip-icloud
fi

echo "[install] Running kb_doctor health check..."
"$PYTHON_BIN" "$KB_ROOT/_同步/kb_doctor.py" --root "$KB_ROOT" --skip-external-checks >/dev/null

echo ""
echo "============================================================================="
echo "[install] agent-memory-kb is ready at: $KB_ROOT"
echo "============================================================================="
echo ""
echo "Next optional steps (nothing in your global IDE/Agent config was modified):"
echo ""
echo "1. Install the Agent Skill (Codex / Claude Code / Cursor / Antigravity):"
echo "     npx skills add JaydenWuGang123/agent-memory-kb"
echo ""
echo "2. Connect the read-only MCP server (install optional deps first if needed):"
echo "     $PYTHON_BIN -m pip install -r \"$WORK_SRC/requirements.txt\"  # or from the GitHub repo"
echo "   Claude Code:"
echo "     claude mcp add --scope local --transport stdio personal-kb -- \"$PYTHON_BIN\" \"$KB_ROOT/_同步/kb_mcp.py\""
echo "   Codex / Cursor / Antigravity JSON snippet:"
cat <<EOF
     {
       "mcpServers": {
         "personal-kb": {
           "command": "$PYTHON_BIN",
           "args": ["$KB_ROOT/_同步/kb_mcp.py"],
           "env": { "AGENT_MEMORY_KB_ROOT": "$KB_ROOT" }
         }
       }
     }
EOF
echo ""
echo "3. Try your first instant local search:"
echo "     $PYTHON_BIN \"$KB_ROOT/_同步/kb_search.py\" --root \"$KB_ROOT\" -q \"项目\" --mode fts"
