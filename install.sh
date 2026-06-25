#!/usr/bin/env bash
set -euo pipefail

# ─── Kizuna (絆) — One-Command Install ──────────────────────────────────
# Copies hook scripts + config template to ~/.claude/
# Initializes the SQLite memory database with seed data

HARNESS_SRC="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
SCRIPTS_DIR="${CLAUDE_DIR}/scripts"
MEMORY_DIR="${CLAUDE_DIR}/memory"

echo "=== Kizuna (絆) Installer ==="
echo ""

# ─── Step 1: Create directories ─────────────────────────────────────────
echo "[1/4] Creating directories..."
mkdir -p "${SCRIPTS_DIR}" "${MEMORY_DIR}"

# ─── Step 2: Copy scripts ───────────────────────────────────────────────
echo "[2/4] Installing hook scripts..."
cp -v "${HARNESS_SRC}/scripts/"*.py "${SCRIPTS_DIR}/"

# ─── Step 3: Install hook config ─────────────────────────────────────────
echo "[3/4] Installing hook configuration..."
if [ -f "${CLAUDE_DIR}/settings.local.json" ]; then
    echo "  ⚠  settings.local.json already exists — skipping (preserve your settings)"
else
    cp "${HARNESS_SRC}/.claude/settings.template.json" "${CLAUDE_DIR}/settings.local.json"
    echo "  ✓ settings.local.json installed"
    echo ""
    echo "  ⚠  IMPORTANT: Edit ~/.claude/settings.local.json"
    echo "     - Set your ANTHROPIC_AUTH_TOKEN"
    echo "     - Adjust permissions to fit your workflow"
fi

# ─── Step 4: Initialize database ─────────────────────────────────────────
echo "[4/4] Initializing memory database..."
python3 -c "
import sys
sys.path.insert(0, '${SCRIPTS_DIR}')
from db import get_db
db = get_db()
db.init_schema()
stats = db.stats()
print(f'  ✓ harness.db initialized: {stats}')
"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit ~/.claude/settings.local.json (set API key)"
echo "  2. Start a new Claude Code session — hooks auto-activate"
echo "  3. Run 'python3 ${SCRIPTS_DIR}/db.py' to inspect the database"
echo ""
echo "To verify:"
echo "  python3 -m pytest ${HARNESS_SRC}/tests/ -v"
