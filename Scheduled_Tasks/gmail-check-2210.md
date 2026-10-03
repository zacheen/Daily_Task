---
name: gmail-check-2210
description: Checks the scout Gmail account for new mail at 20 minutes past each hour from 07:20 to 22:20, skipped before the model starts when a check succeeded within the last 45 minutes
---

Read D:\dont_move\git_save\Daily_Task\Email_Check\INSTRUCTIONS.md, then follow the steps in that file exactly.

That file is the only source of rules for this task. Do not act from memory or guesswork, and do not add rules here.

If that file cannot be read, do not guess what to do. Instead run the line below **with the Bash tool** to send a toast. Keep the string exactly as written. Never use a form that starts with `&`, which has a known permission-matching failure and hung one run each on 2026-09-09 and 2026-09-10.

powershell.exe -NoProfile -File "D:\dont_move\git_save\Daily_Task\shared\notify.ps1" -Message "Gmail check stalled / Email_Check\INSTRUCTIONS.md not found / needs attention" -Title "Gmail"

Then stop, and do not touch Gmail. **This toast is the only notification channel.**
