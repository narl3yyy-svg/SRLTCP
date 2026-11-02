#!/bin/bash
# Cloud sessions start on a fresh machine: install the graphify CLI that the
# /graphify skill and the graphify hooks in settings.json run. Pinned to the
# version the skill files in .claude/skills/graphify were written by.
# Only in Claude Code cloud sessions (nothing is installed on your own PC).
[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0
export PATH="$HOME/.local/bin:$PATH"
command -v graphify >/dev/null 2>&1 && exit 0
if command -v uv >/dev/null 2>&1; then
  uv tool install -q graphifyy==0.9.100 >/dev/null 2>&1
else
  python3 -m pip install -q --user graphifyy==0.9.100 >/dev/null 2>&1
fi
[ -n "$CLAUDE_ENV_FILE" ] && echo "export PATH=\"$HOME/.local/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
exit 0
