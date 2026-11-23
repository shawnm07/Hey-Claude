"""
Approval lock for Hey Claude (a Claude Code PreToolUse hook on PowerShell/Bash).

Email is done in the browser: compose-email.ps1 opens a filled-in Gmail compose
tab (allowed freely), and pressing Send is the only thing that needs approval.
This hook looks at every shell command Claude runs and, if it would press Send
(the send-compose.ps1 helper, or any Ctrl+Enter keystroke), allows it ONLY if
the listener recorded a clear spoken "yes" to a confirmation question within
the last 90 seconds. Each "yes" allows exactly one send. Otherwise the command
is blocked (exit code 2) and Claude is told to ask first.

The listener writes workspace/.approval (timestamp + the approved question)
when the user answers a question with a clear yes.
"""
import json
import re
import sys
import time
from pathlib import Path

TOKEN = Path(__file__).resolve().parent / "workspace" / ".approval"
MAX_AGE_S = 90

raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").lstrip("﻿")
try:
    command = json.dumps(json.loads(raw).get("tool_input", {})).lower()
except Exception:
    command = raw.lower()                              # can't parse: still check the raw text (fail closed)
presses_send = (
    "send-compose" in command
    or re.search(r"\^\{enter\}|\^~|ctrl\s*\+\s*enter|vk_return.{0,80}vk_control|vk_control.{0,80}vk_return", command)
)
if not presses_send:
    sys.exit(0)                                        # anything else: not our business

try:
    stamp = float(TOKEN.read_text(encoding="utf-8").splitlines()[0])
    fresh = 0 <= time.time() - stamp < MAX_AGE_S
except Exception:
    fresh = False

if fresh:
    TOKEN.unlink(missing_ok=True)                      # one approval = one send
    sys.exit(0)

print("Blocked by the approval lock: pressing Send needs the user's spoken approval first. "
      "Ask ONE confirmation question that names the recipient(s), the subject and how it starts, "
      "and wait for a clear yes before trying again.", file=sys.stderr)
sys.exit(2)
