#!/usr/bin/env bash
# ==============================================================================
# deploy_skill.sh - Deploy aimail SKILL.md to agy, opencode, and other runtimes
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIMAIL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SKILL_SRC="${AIMAIL_DIR}/skills/aimail/SKILL.md"

if [ ! -f "${SKILL_SRC}" ]; then
  echo "Error: Canonical SKILL.md not found at ${SKILL_SRC}" >&2
  exit 1
fi

MODE="symlink"
TARGETS="agy,opencode"

while [[ "$#" -gt 0 ]]; do
  case $1 in
    --copy) MODE="copy"; shift ;;
    --symlink) MODE="symlink"; shift ;;
    --targets) TARGETS="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: ./scripts/deploy_skill.sh [--symlink|--copy] [--targets agy,opencode,claude]"
      exit 0
      ;;
    *) echo "Unknown parameter: $1"; exit 1 ;;
  esac
done

# If aimail CLI is available, delegate to it for rich formatting
if command -v aimail >/dev/null 2>&1; then
  exec aimail skill install --mode "${MODE}" --targets "${TARGETS}"
fi

# Fallback standalone shell logic if aimail is not yet in PATH
echo "Deploying aimail skill (source: ${SKILL_SRC})"
echo "Mode: ${MODE}"

IFS=',' read -ra T_ARRAY <<< "$TARGETS"
for t in "${T_ARRAY[@]}"; do
  case $t in
    agy)
      DEST="${HOME}/.gemini/config/skills/aimail/SKILL.md"
      ;;
    opencode)
      DEST="${HOME}/.config/opencode/skills/aimail/SKILL.md"
      ;;
    claude)
      DEST="${HOME}/.claude/skills/aimail/SKILL.md"
      ;;
    *)
      echo "  [!] Unknown target runtime: $t"
      continue
      ;;
  esac

  mkdir -p "$(dirname "${DEST}")"
  rm -f "${DEST}"
  if [ "${MODE}" = "symlink" ]; then
    ln -s "${SKILL_SRC}" "${DEST}"
    echo "  ✓ [${t}] Symlinked ${DEST} -> ${SKILL_SRC}"
  else
    cp "${SKILL_SRC}" "${DEST}"
    echo "  ✓ [${t}] Copied to ${DEST}"
  fi
done

echo ""
echo "Deployment complete."
