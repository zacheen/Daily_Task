# Gmail important-mail check, instruction file

This is the single instruction source shared by two scheduled tasks, one at 50 minutes past each hour (06:50 to 21:50) and one at 20 minutes past each hour (07:20 to 22:20). Before 2026-09-28 there were four slots a day, 06:25, 11:40, 16:55 and 22:10. The scheduled task's own SKILL.md is only a one-line stub, so do not write rules back into it.

## Division of responsibilities

| Who | Responsible for |
|---|---|
| `statemachine.py` | Time window, interval coverage, watermark, dedupe queue, atomic file writes |
| `shared/gmail-gate-hook.ps1` plus `statemachine.py gate` | Decides before the model starts whether this round is skipped outright, see the section The skip decision happens before the model starts |
| You (the LLM) | Calling the Gmail MCP, **judging importance**, writing the push notification text |

**Whatever the script tells you, do it, and do not recompute it yourself.** The rules for advancing the time window and the watermark are deliberately not written here, because they must be deterministic. Prose rules were misunderstood on cold reads too many times, so that part has been moved out. The script proves coverage is complete by bisecting on the query boundaries itself, and does not rely on the time shown on a message, because Gmail filters on internalDate while the displayed Date header lags behind it by a difference with no upper bound.

To change the **judgement criteria**, change this file. To change the **time window or state logic**, change `statemachine.py` and run `test/test_statemachine.py`.

## Goal
Check the Gmail account `scout` for new mail since the last check, and judge whether any of it is important. Push a notification only when there really is important mail.

Everything the user reads is written in the language the last paragraph of SKILL.md names, which is Traditional Chinese. That covers the toast messages and every text field in `round.json` that the task list shows, such as `summary` and `action`. The Chinese examples in this file show that form, and the UI labels such as 待分類 and 追蹤中 stay exactly as written.

The account parameter of the Gmail MCP tools must always be `"scout"`. This account alias is the only means of identification. **Do not look up the actual mailbox address it maps to, and do not write the address into any output**. Passing any other value fetches a different mailbox.

## Top principle
Missing one message that needed action is far worse than sending one extra notification. Every trade-off leans in this direction.

- **Insufficient information does not mean unimportant.** A message you cannot judge must be kept for the next round, and must not be discarded as unimportant
- **A failure does not mean no new mail.** When a search fails, report `--failed`, and do not treat it as zero messages
- **A failed notification does not mean notified.** Write to `notifiedIds` only when sending definitely succeeded, and "sent successfully" is defined as the one and only channel in step 5 returning `Toast sent.`. Notification has only this one path, and there is no backup channel to fall back on

## Usage-saving principle
This task is scheduled for 32 slots a day. A slot within 45 minutes of a successful check is blocked before the model starts, so in practice about one round really runs per hour. Token cost is multiplied by the number of rounds that actually run. Follow this principle as long as it does not violate the Top principle.

- Prefer judging from the snippet that comes with the search results. The snippet comes along for free and needs no extra call
- When the sender, subject or snippet is already enough to judge, never read the body. But **when you cannot judge, read it**. Reading the body has no limit on the number of times, see the section When to read the body
- Do not write a report, do not output intermediate lists, and do not summarize unimportant mail

## Required permissions and fixed commands

Scheduled tasks run unattended. **Getting stuck on a permission prompt is the same as the whole round not running, and nobody will press Allow.** This is more fatal than any missed-mail bug, because not even an alert can be sent.

Always call the script in this form. **The string is fixed and must not be rewritten**, because the allowlist matches it word for word.

**Run every command with the Bash tool, including ones this document does not list.** A rule also lets the same line through when it is run with the PowerShell tool, but that is insurance for when the model does not follow this, not the normal path.

```
conda run -n ML python "D:\dont_move\git_save\Daily_Task\Email_Check\statemachine.py" <subcommand>
```

### Only the commands on the list may run

The allowlist matches word for word, so **a command not on it stops at a permission prompt, and during a scheduled run nobody will press Allow**. So the Bash and PowerShell commands the whole round may run are the closed list below, and there is no fifth kind.

| Command | In which step |
|---|---|
| `begin` / `wait` / `step` / `commit` of `statemachine.py` | Steps 1, 3, 6 |
| `calendar_check.py` | Step 4 |
| `notify.ps1` | Step 5 |
| `open-task-list.ps1` | Step 7 |

`statemachine.py gate` is not in this table, because the hook runs it for you before you start, and the fact that you can read this document means it already returned that the round runs as usual. **Do not run it again yourself.**

**Do not compose a command of your own to look through a file in order to save tokens.** This is a failure that really happened. One round, after reading the bodies and just before writing `round.json`, ran the line `conda run -n ML python -c ...` to print the source code of `cmd_step` in `statemachine.py`, got stuck on the prompt, and the whole round stopped there. The Usage-saving principle gives way to this rule, because the cost of getting stuck is the whole round going to zero, while what is saved is only a few hundred tokens.

That command would not have run even if someone had pressed Allow. `conda run` does not support arguments containing a newline, and a multi-line `python -c` throws `NotImplementedError` right away, so it was a dead end from the start.

To view a file's contents, always use the **Read tool**. It covers every file under `Email_Check/` and does not prompt.

The status table in this document plus the `round.json` format in step 6 are the **complete interface contract**, independent of how the script is implemented internally.

### Rules go in both layers

**These rules must be placed in both the project's `.claude/settings.local.json` and the user-level `~\.claude\settings.json`.** The user level holds only the absolute-path forms, and the relative-path ones stay in the project file.

The reason is a known failure whose **cause has not yet been found**. In the 2026-09-09 21:32 round the toast command got stuck on a permission prompt, even though the project file had a word-for-word match `PowerShell(& "...notify.ps1" *)`. The prompt stays in the app waiting for someone to press it, and if nobody does, the whole round does not run, and there is no alert of any kind.

**Do not say "the project settings file is not loaded during scheduled runs".** That guess was disproved by measurement on 2026-09-10. The Qualification task writes `Personal_Task/Qualification/reports/` and `Personal_Task/Qualification/baseline.md` every day and succeeded seven days in a row, while the rules covering those paths exist only in the project file. So the project layer does take effect.

The real cause is not yet determined. The current main suspect is the `&` at the start of the command. It is PowerShell's call operator, but if permission matching uses the command parser shared with Bash, `&` would be treated as a separator, so `prefix *` would never match.

**On 2026-09-10 it got stuck again, this time in the Qualification task, so everything switched to forms without `&`.** Every caller now goes through the Bash tool plus `powershell.exe -NoProfile -File "...notify.ps1" -Message ... -Title ...`, a form measured to send a Chinese toast, and its allow rule was already in both layers. The rules in the `&` form are left in both settings files for now and not deleted, but no caller uses them any more, so do not switch back.

The value of placing rules in both layers is not "because the project layer does not work". It is that the working directory is removed as a variable, so when both layers match, not a single rule goes missing, whatever the cause.

**Mirror deny as well.** If only allow is mirrored, the user-level `Edit(...Email_Check\**)` would allow editing those four request files, while the deny that blocks them exists only in the project file, which loosens permissions for nothing.

Be careful with backslashes when writing rules into JSON. In a Python string, the `\t` in `"...task_list\tasks-restore.json"` is a tab, and `json.dumps` faithfully writes it as the `\\t` escape, so the rule breaks, and this cannot be seen in the file. Always build backslashes with `chr(92)`.

The `permissions.allow` of both settings files must include the following.

| Entry | Purpose |
|---|---|
| `Bash(conda run -n ML python "D:\dont_move\git_save\Daily_Task\Email_Check\statemachine.py"*)` | State machine |
| `Bash(conda run -n ML python "D:\dont_move\git_save\Daily_Task\Email_Check\calendar_check.py"*)` | Calendar check |
| `mcp__gmail__search_emails` | Fetch mail |
| `Bash(powershell.exe -NoProfile -File "D:\dont_move\git_save\Daily_Task\shared\notify.ps1"*)` | Local toast notification, current form |
| `PowerShell(& "D:\dont_move\git_save\Daily_Task\shared\notify.ps1" *)` | Old form, no callers left, kept for reference |
| `Bash(powershell.exe -NoProfile -File "D:\dont_move\git_save\Daily_Task\shared\open-task-list.ps1"*)` | Open the 待辦清單 (task list), current form |
| The `PowerShell(...)` version of each of the four current Bash rules above, with the string inside the parentheses identical word for word | Insurance for when the model runs the same command with the PowerShell tool, see below the table |
| `mcp__gmail__get_email_body` | Read the body |
| `Read(Email_Check/**)` and its absolute-path version | Read this file and config.json |
| `Edit(Email_Check/**)` and its absolute-path version | Write round.json |
| `Bash(grep:*)` / `Bash(sed -n:*)` / `Bash(head:*)` / `Bash(tail:*)` | Read-only fuses, see the previous section, the normal flow should never need them |

Those four read-only forms are fuses, not permissions, and must not be used to look through the source code of `statemachine.py`.

**An allow rule is bound to a tool, so the same command run with a different tool does not match.** A `Bash(...)` rule matches only calls of the Bash tool, so if the model instead runs the identical command with the PowerShell tool, it still stops at a prompt. Claude Code on this machine treats PowerShell as the primary shell, and for a command block that does not name a tool, like the one in step 1, which tool gets used each round is in effect decided by the model on the spot. gmail-check-2210 ran `begin` with PowerShell this way in two rounds, on 2026-09-23 and 2026-09-25, and both rounds got stuck on the prompt. The earlier of the two did not even get a status, and there was no alert of any kind. Of the 70 rounds up to 2026-09-25, only these 2 chose PowerShell and all the rest used Bash, and the SKILL.md files of the four tasks differ only in the two lines of name and description, so this is not a problem specific to one task, and any task can draw it. After the PowerShell versions were added on 2026-09-26, running the `--help` of the state machine and of the calendar check with `claude -p --tools PowerShell --permission-mode default`, loading only the user layer and then only the project layer, was not denied in either layer, while running a command outside the list the same way was denied. The PowerShell versions for `notify.ps1` and `open-task-list.ps1` were not measured, because running them would really pop up a notification and open a window. This behaviour appeared only after the schedule switched to a new model on 2026-09-22. Before that all 55 rounds went through Bash, and after it 3 of the 20 rounds up to 2026-09-27 went through PowerShell. The model and the Claude Code version were changed together on the same day, so it cannot be told which one caused it. So from 2026-09-27 on, the command of every step says explicitly to run it with the Bash tool, and the PowerShell-version rules stay as insurance.

**The table above is not the whole of the project file.** The same `.claude/settings.local.json` serves every scheduled task under this working directory, and the tasks in `Personal_Task/` have their own domain and path rules, which cannot be written into this document because this repo is public. Each one's spec is written in that task's own README.md under `Personal_Task/`. When rebuilding the project file, look at both sides. Rebuilding from this table alone misses half, and the missing half fails silently during scheduled runs instead of raising an error interactively.

Both settings files also still keep an old `Start-Process` rule that hard-codes the local `pythonw.exe` path. **It has no callers left and is kept for reference. Do not switch back to using it.** That path contains the local user name, so it exists only in the settings files that are not under version control, and this document does not repeat it.

**Rules in the `Write(路徑)` form are never matched, only `Edit(路徑)` is**, and an `Edit` rule covers every file-editing tool (including creating a new file). Path entries must be placed in both relative and absolute forms, because the working directory during a scheduled run is not guaranteed.

From now on, any new tool call or command **must be added to the allowlist at the same time**. If it is not, interactive tests look normal, and scheduled runs get stuck silently.

### The skip decision happens before the model starts

Besides permissions, the project's `.claude/settings.local.json` also has a `hooks.UserPromptSubmit` section, whose command is `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:/dont_move/git_save/Daily_Task/shared/gmail-gate-hook.ps1"` with a timeout of 60 seconds. When rebuilding that file, this section must be restored too. It goes only in the project layer and is not mirrored to the user layer, because placing it in both layers would check the same prompt twice, and the consequence of this section going missing is only that every round runs as usual, without missing a single check.

This hook acts only on a prompt that starts with `<scheduled-task name="gmail-check-`, which is the marker the desktop app's scheduler itself wraps around the prompt, and every other prompt is let through before Python starts. Only when the marker matches does it run `statemachine.py gate`, and when that returns `SKIP` it blocks the prompt, so the round ends before the model starts and spends not a single token. `gate` returns `SKIP` only when all of the following hold, and in every other case the round runs as usual, including when the hook itself errors or times out.

- The last successful `commit` was within 45 minutes, and its time is not in the future
- `pendingNotify` is empty, with no notification owed
- `coverage.intervals` is empty, with no interval left unscanned
- No config warning is owed
- `state.json` can be read

Another round that is currently running is **deliberately not counted** as a reason to skip. The round it would block has to get as far as `begin` and `wait`, so that someone takes over when the earlier round has died.

The decision sits before the model starts because making it inside the session is too expensive. On 2026-09-27 a skip that ended only after reading this document and running `begin` and `wait` was measured at a cache write of 60,219 and a cache read of 234,470, close to the cost of a whole round, of which about 23k was the start of the session itself and about 37k was this document.

The results measured the same day are as follows. The desktop app's scheduler does trigger the project-level `UserPromptSubmit`, which arrived about 5 seconds after dispatch. When another task was triggered manually right after a round had committed, that triggered round was blocked, ending with 0 turns in 5 seconds, and the Runs panel still recorded it as `succeeded`, which is consistent with entry 1 of `Known_concern.md`. Headless mode additionally confirmed that input and cache tokens are both 0 when a prompt is blocked, and that when the hook times out the prompt is sent as usual. The cost is that every interactive prompt in this project waits about 0.7 seconds longer for PowerShell to start, and a scheduled prompt that matches the marker waits about 5 seconds because of `conda run`.
## Procedure


### Statuses the script returns

All four subcommands share this table. **Treat any status not in the table as an anomaly**, send a notification saying so and then end, and do not guess.

| `status` | Subcommand | Meaning | What you do |
|---|---|---|---|
| `PROCEED` | begin | Normal start | Continue with step 1 |
| `ROUND_ALREADY_RUNNING` | begin | Another round wrote progress within the last 20 minutes and is running | **Run `wait` instead** (see below the table), do not touch Gmail, do not send a notification |
| `STILL_RUNNING` | wait | That round is still running, and this wait ran its full length | **Run `wait` again** |
| `COVERED` | wait | That round has committed, and it scanned this period | **End this round quietly.** Do not rerun `begin`, do not send a notification |
| `TAKE_OVER` | wait | That round has written no progress for over 20 minutes and is treated as dead | **Go back to step 1 and rerun `begin`**, and this round takes over |
| `SEARCH` | step | Ranges remain to scan | Take the new `nextQuery` back to the start of step 3 |
| `DONE` | step | This round's search is finished | Go to step 4 |
| `SEARCH_FAILED` | step | You reported a search failure, and the range is left for the next round | **Still send notifications for the backlog that step 2 already judged important**. Do not swallow confirmed important mail because the search for new mail failed |
| `COMMITTED` | commit | This round has been committed atomically | Read its fields to wrap up (see steps 6 and 7), and this round ends |
| `ABORT_STATE_CORRUPT` | begin / step / commit | `state.json` is corrupt | **Send a notification saying so, then end. Do not touch Gmail, and do not try to repair the state file** |
| `NO_ROUND_IN_PROGRESS` | step / commit | This round's `round-progress.json` is missing or unreadable | **Abandon this round and end quietly.** Do not rerun `begin`, do not send a notification |
| `FINDINGS_UNREADABLE` | step / commit | `round.json` is unreadable, or the `roundToken` does not belong to this round | **Abandon this round and end quietly.** Do not retry the same `step`, and above all do not treat it as `DONE` |
| `STATE_CHANGED_ABORT` | begin / step / commit | Another round wrote `state.json` after this round finished reading the state, so this round's write was blocked | **Abandon this round and end quietly.** Do not rerun, do not send a notification |

Why the last three can abandon quietly without missing mail. The range was **deliberately not** retired, so the next round rescans the same period. Mail that earlier batches of this round already judged important was written into the queue at the moment of its own `step`, and the next round's `notifyNow` re-sends it. Only the batch that has not landed yet is abandoned, and that batch's time range is still there too.

One point about `STATE_CHANGED_ABORT` needs stressing. It is not a fault but the safeguard taking effect. The two schedules share one state, and during a catch-up run or a manual run two rounds may overlap. The script would rather have the later round abandon entirely than let it overwrite the earlier round's todos and watermark. **The winning round's state is the correct one**, so do not try to write your round's results back in, and do not send a notification because of it. Sending one would only produce a false alarm the user has no way to act on.

The most common cause of `NO_ROUND_IN_PROGRESS` is not a corrupt file but the other scheduled task's `commit` clearing the shared `round-progress.json`. That means someone else is handling it, and the right thing is for you to exit quietly.

`ROUND_ALREADY_RUNNING` exists for catch-up runs after a shutdown. Tasks that missed their slots all fire at once when the app reopens, but any one round scans the whole period since the previous round, so one round is enough. The first round to run `begin` proceeds as usual, and the rest receive this status at `begin`, at which point the script has not written anything yet. **On receiving it, do not simply end. Run the line below instead to wait for that round's result.** Run it with the Bash tool. Each call waits at most 90 seconds.

```
conda run -n ML python "D:\dont_move\git_save\Daily_Task\Email_Check\statemachine.py" wait
```

If it returns `STILL_RUNNING`, run the same line again. `COVERED` means that round has committed and scanned this period, so end this round quietly. `TAKE_OVER` means that round has written no progress for over 20 minutes and is treated as dead, so go back to step 1 and rerun `begin`, and this round takes over. `wait` only reads and never writes, so several rounds waiting at once do not interfere with each other. If two rounds rerun `begin` at the same time while taking over, one of them receives `ROUND_ALREADY_RUNNING` again and waits again in the same way.

The decision is based on when `round-progress.json` was last written. `begin` and every `step` write it, and `commit` deletes it. Receiving this status used to mean ending at once, and as a result, if the first round died partway, the whole batch of catch-up runs was wasted, because the other rounds had already left. A round that accidentally reruns its own `begin` also receives this status. It likewise runs `wait` instead and takes over once its own progress file expires, so the round is not thrown away. The cost is that the other rounds cannot end until the first round finishes, and when the first round dies it takes up to 20 minutes before anyone takes over. Scheduled slots are several hours apart, so the only rounds that reach this path are the ones catching up at the same time, and a 「立即執行」 (Run now) pressed by hand while another round is still running.


### 1. begin

**Run it with the Bash tool.**

```
conda run -n ML python "D:\dont_move\git_save\Daily_Task\Email_Check\statemachine.py" begin
```

It returns JSON. **For what each `status` means and what to do about it, always look it up in the master table at the start, which is not repeated here.** Every step refers only to the master table, because keeping a separate list in two places has already gone out of sync once.

Every status other than `PROCEED` is in the master table, so follow the table. For `ABORT_STATE_CORRUPT` one more sentence of reasoning is added here. The script deliberately does not make up a watermark on its own, because making up a newer watermark would silently skip all mail since the last check.

Fields that come with `PROCEED`

| Field | Purpose |
|---|---|
| `notifiedIds` | Ids already notified. Always skip any mail that appears here |
| `notifyNow` | Mail whose notification failed last round, to be re-sent this round. Mail the user has already checked off as done does not appear here |
| `judgeOverdue` | Mail awaiting judgement that has waited too long. See step 2 for how to handle it |
| `judgeNow` | Older backlog not yet fully judged. All of it must be judged this round |
| `nextQuery` | The first query to run, containing `query`, `lo`, `hi`, `max_results` |
| `coverageStalled` | **Appears only on a fault**, and when it appears it is `true`. See the section below for how to handle it |
| `stalledHours` | Present exactly when the previous field is. It is how many hours behind coverage is, and the alert message must include this number |

`config` is the health of `config.json`, with fields `status` / `names` / `calendarUrls` / `alert`. You do not need to check yourself whether the file exists, because the script has already looked.

When `status` is `present`, read `Email_Check/config.json` to get `names`, which are the various forms of the user's own name. Copy the path exactly, and do not write just `config.json`. The working directory is the repo root rather than this folder, so a bare file name resolves to `Daily_Task\config.json`, where nothing exists. For `incomplete` (the file exists but `names` is empty), `missing` (it does not exist) and `malformed` (the JSON is broken), always skip identity recognition and do everything else as usual, and **do not abort**. Being unable to recognize who the person is only disables the early-exit rule in Mail not addressed to the user, and the direction is keeping more mail rather than missing mail.

When `alert` is `true`, the notification in step 5 must carry an extra section about the config file anomaly. It is `true` only in the round where the state turns from normal to abnormal, or from one kind of anomaly to another, so a missing file left unfixed does not send a notification every round.

**If the notification succeeds, report `configAlerted` in step 6.** Alerts follow the same 「預設保留」 (keep by default) rule as mail, so if it is not reported it is treated as not yet delivered and the next round sends it again. Leaving it out only costs one extra notification, and leaving it out after a successful send causes no error either. Conversely, filling it in when the send failed silences this fault permanently, until it turns into a different kind of fault.

When `calendarUrls` is 0, **do not call `calendar_check.py`**. An empty array is a valid setting in which the user deliberately turned off the calendar check, not a fault. Calling it would only return `UNCHECKED` and waste a subprocess. Handle events directly as not on the calendar, per step 4.

`config.example.json` is a template for people to read. Do not read it at runtime.

#### On `coverageStalled`, send a toast at once, before anything else

**These two fields normally do not exist.** A normal round's return does not contain them at all, so there is no need to judge the size of the number or compare it against any threshold. Seeing them means a fault, and not seeing them means normal. The threshold comparison has already been done in the script.

Send it with the exact command format from step 5. Copy the message from the line below verbatim, replacing only the number with the value of `stalledHours`.

```
Gmail 檢查已經 <stalledHours> 小時沒有跑完任何一輪，信可能正在累積，需人工檢查
```

**After sending it, continue this round normally, and do not abort.** A lagging front does not mean this round will fail too, and finishing this round is exactly how the front catches up.

**This goes first because this round may also die partway.** The stage that is stuck notifies no one, so the alert must go out before the round reaches it.

Why this number can be trusted. The coverage front tracks "which time ranges have been scanned", not "when mail was seen". `step` retires a range based on the query bounds, not on how many messages were found, so an empty range advances the front just as a full one does. If the mailbox is quiet for three days the front still moves up to now, so a stuck front means only one thing, that some round did not finish.

The 24-hour threshold is longer than the longest normal gap between two slots, which is the 8 hours 30 minutes from 22:20 to 06:50 the next day, so it does not fire under normal conditions. A computer shut down for two days and then turned back on fires it once, and that is not a false alarm, since it really did not run for two days.

**What this alert cannot catch.** If `begin` itself cannot run, for example because it hits a permission prompt, the whole round never starts and this section is never executed. What it covers is the case where `begin` succeeds but the rest does not finish.

### 2. Settle the backlog first

Re-send mail in **`notifyNow`** directly per step 5. Its importance was already confirmed last round, so do not judge it again.

Mail in **`judgeOverdue`** has already waited more than two rounds. **Never drop it because you cannot decide.**
- For mail that looks like part of a job application process (recruiter, HR, ATS, company domain), comes from a high-risk source (school, government, bank, insurance, landlord), or looks like a personal exchange with a real person, **send a 「待確認」 (needs checking) notification** and let the user open the mail themselves
- Drop it only when you are sure it is marketing or a platform push notification

The reason is that queueing has a cost of its own. When there were only four slots a day, waiting two rounds meant more than ten hours, and an OA due that same afternoon was meaningless by the time its turn came. Since 2026-09-28 about one round actually runs per hour, so waiting two rounds takes about two hours and mail can be escalated to 「待確認」 during the day. There are no rounds at night between 22:20 and 06:50, so the longest wait still lasts until the next morning.

Judge everything in **`judgeNow`** this round. There is no limit on how many times you read a body, so if you cannot decide, read it.

### 3. Search loop

Call `mcp__gmail__search_emails` with `nextQuery`, passing `"scout"` as `account` and passing `query` and `max_results` exactly as the script gave them, and **do not change the query yourself**.

The end of `query` already carries a set of `-from:` exclusions, taken from `excludedSenders` in `config.json`. Those are crawlers and feeds the user set up personally, sending dozens of digests a day. They are the largest source of noise in this mailbox, and none of them needs a notification. **The script builds the list into the query, so you do not need to know it and must not add to it or remove from it yourself.** When `config.json` cannot be read, that whole set of exclusions disappears, and the direction is fetching more mail rather than less, so the only cost is extra tokens.

`query` also carries a set of `-label:` exclusions, taken from `excludedLabels` in `config.json`, which is currently just the `ignore` label. It is applied automatically by a filter the user set up in Gmail, under rules the user personally wrote and confirmed, so new mail already carries the label when it arrives. **A label means the user has already decided about this kind of mail, so do not remind them again**, and that mail does not even need to be fetched.

The decision was made in advance in the rule, not by looking at each message, so **do not infer the reverse, that no label means the user has not seen it**. No label only means that mail does not belong to a category the user excluded, and judgement always falls back to the main criterion.

This is stronger than `excludedSenders`, because a label can be applied to just one message from a sender while that sender's other mail still comes in.

**Only the user can apply labels. You may not apply one, and you may not suggest adding anything to `excludedLabels`.** The Forbidden section already blocks the act of adding a label, and this is the other side of the same line. Judging something unimportant is within your authority, while declaring "never ask me about this again" belongs to the user. When you see a batch of repeated noise mail, the right thing is to judge it unimportant as usual and then mention it once in your report, so the user can decide whether to apply a label.

**The query does not contain `in:inbox`, and you must not add it.** This mailbox has a filter that automatically routes job-search mail out of the inbox. In a measurement over the same 6-hour window, adding it left only 2 messages while leaving it out gave 24, and the ones missed were exactly the replies to applications.

Once you have the results, first judge this batch per step 4, **write this batch's `important`, `todos` and `defer` into `round.json`** (format in step 6, and remember to include the `roundToken` that `begin` gave you), then report with the Bash tool.

```
... statemachine.py step --lo <lo> --hi <hi> --count <這批回傳的封數>
```

If the search errors or times out, retry once. If it still fails, report `... statemachine.py step --failed` instead.

**A search that completes with no output means zero messages, not a failure.** The tool returns an empty list when nothing matches, and the client shows that as no output at all. A failed search always comes back as an error, because the MCP raises rather than returning an empty list. So report no output as `--count 0` with the `--lo` and `--hi` of that query. Reporting it as `--failed` is not the safe side here. It keeps a range that was already scanned and sends a false 「尚有舊信未掃完」 toast, which happened in one round when a 74-minute window really held no mail.

Always look up the `status` from `step` in the master table at the start, and **it is likewise not repeated here**, for the same reason as in step 1. It may return any status whose subcommand column in the table includes step.

When `stuck` is not empty, it means more than 40 messages are packed into the same second and cannot be bisected any further. The notification must say 「舊信追趕卡住 需人工處理」, because that will not resolve itself.

### 4. Decide what is important
There is only one main criterion, the actionability test below. Separately there is one independent exception category.

**Main criterion, there is a next action for the user to take**
The criterion is the GTD actionability test. Both conditions below must hold at the same time.

> 1. The mail has a concrete next action
> 2. That action is for the user to take, not a solicitation broadcast to a large crowd

It counts only when both hold. The second condition exists to exclude mass solicitations. Marketing mail also has a next action, but it is broadcast and is not waiting for the user personally to respond.

**When no recipient name is written, the default is that it is for the user to do.**
Many legitimate mails never mention a name at all. A bank writing "please verify your account", a recruiter writing "are you free Thursday", a landlord writing "remember to pay before the end of the month" name nobody, yet each really is waiting for the user to handle it. When a mail arrives in this mailbox, the default recipient is the user.

Ruling that "this is not for the user to do" requires positive evidence, and the burden of proof lies on the excluding side. Examples of usable evidence.
- Mass marketing carrying a List-Unsubscribe header
- Commercial promotion clearly written for a large audience
- A forwarded third-party mail whose real party is someone else
- An action in the content that holds for any recipient, not one specific to this account or this person's situation

When no such evidence is found, treat it as for the user to do. Do not rule it non-actionable because a name was **not written**. But **someone else's name being written** is a different matter. That is positive evidence, see the Mail not addressed to the user section below.

**Mass sending by itself does not prove "this has nothing to do with the user".**
A mandatory task aimed at a group the user belongs to also counts as a next action. For example, when the school requires all international students to complete check-in by a deadline, that is mass mail, but the user must do it.

So the following sources, even when they look like a newsletter or mass mail, **must not be dropped early on form alone**. First confirm they contain no mandatory task before letting them go. If the snippet does not show everything, read the body.
- School (Northeastern units, OGS, Student Services)
- Government and public agencies (tax, embassies, competent authorities at every level)
- Banks, insurance, landlords, student loans
- **Mail that an organization the user has joined or registered with sends to its members.** Volunteer teams, clubs, event organizers, course camps. The test is that the user agreed earlier to join or take part, so the organization's notice to its members is part of fulfilling a commitment, not a solicitation. A real case is the pre-event notice from the Taiwan Next volunteer group

Conversely, mass mail that is pure commercial marketing (shopping, deals, product launches, third-party career fair solicitations) can be excluded directly on form, without reading the body. **But a recruiting event invitation run by the employer itself is not in this group**. That one is a todo, see "Examples that count as a next action" below.

**The line between an organization the user joined and a pure solicitation is whether the user agreed, not the shape of the mail.**
One domain may send both. A real case is `taiwannext.org`. The Mailchimp list on `contact@` sends outward solicitations (carrying List-Unsubscribe, excludable on form), while the pre-event notices that `volunteer@` sends to registered volunteers are mandatory tasks. **Do not exclude a mail just because an earlier mail from the same domain was excluded.**

#### Mail not addressed to the user

`names` in `config.json` has exactly one use here, which is catching mail whose **real party is not the user**. The `scout` account is a forwarding hub, so it receives misaddressed mail and forwarded third-party mail.

**When the salutation in a mail is someone else's name, the matter is not for the user to do. This is decisive, not advisory.**

How to apply it.
- Compare the name in the opening salutation (`Hi X`, `Dear X`, `X 您好`) against `names` in `config.json`. Match loosely, so first name only, surname only, different romanized spellings, and Chinese characters swapped with pinyin all count as a hit
- A hit, or no discernible salutation -> judge as usual
- The salutation is **a clearly different person** -> **not important, not into `todos`, no push**, put the id in `judgedIds` to close it

**A group salutation is not someone else's name and is not exclusion evidence.** `Dear Taiwan Next Volunteers`, `各位同學`, `Dear Students` refer to a group the user is part of, so the user is a recipient. This rule fires only when the salutation is **a specific person who is not the user**. On a group salutation, go back to the main criterion and apply the paragraph above saying that mass sending by itself does not prove it has nothing to do with the user.

Another real case. The user once received a mail from Tesla whose content was 「你已報名的 Supercharging Your Resume Workshop 今天 16:30 舉行」. That **is** a todo, because attending is an action and the time is a hard deadline. Do not exclude it because it reads like a reminder or like event promotion. The basis for the ruling is that it gives a concrete time at which the user must show up, and the user already agreed.

A real case. The user once received a mail from the volunteer group with the subject 「煩請補交 Facebook 個人帳號連結」, opening with `Hi Hsiao Ming`. That is not any form of the user's name, so it was a mail for someone else. The ruling at the time upgraded it to a 「待確認」 (to be confirmed) todo, and **that was wrong**.

**Do not handle this case with `uncertain` or 「待確認」.**
`uncertain` is for mail that is certainly for the user to handle but whose details cannot be fully seen. "Unsure whether this mail is for the user" is not its use. If the salutation is someone else, it is someone else, so close it directly. Do not hand the user an item he should never tick and would not know why it is there.

The reverse does not hold. **The user's name not appearing in a mail does not mean the mail is not for the user.** With no name written, the default is still that it is for the user to do. Only a written name of **someone else** is exclusion evidence.

**Do not treat "the mail contains the user's name" as an importance signal.** Marketing mail personalizes too, and this mailbox has a real case, a Handshake job push whose subject used the user's name directly. The name only identifies the recipient. Importance is always judged by going back to the two conditions above.

Examples that count as a next action.
- Mail from a real recruiter or HR person asking about interest, asking to schedule or pick a time slot, or waiting for a reply
- Interview invitations and reschedules
- OA, online assessments, assignment or interview links, for example HackerRank, CodeSignal
- An offer the user must decide to accept or decline
- A referral reply where the other side is waiting for the user to provide material or confirm
- A school, government, bank, landlord, insurer or tax authority asking to submit, upload, sign, verify, supply missing documents, or pay
- A real person asked a question and is waiting for a reply
- The kind of account security event that really needs hands-on action. For example the account is locked, verification is needed to regain access, a password reset the user never started, the service forcing an immediate password change, a payment method disabled. It also counts when some setting has already been changed and the user did not do it, for example the password was changed, a forwarding rule was added, a recovery contact was replaced. That means the account may already be compromised, so notify. **But a one-time verification code does not count**, see that entry under the examples that do not count as a next action below
- **A verification link for a mailbox or account.** The kind you click to complete registration or activation, for example a job application system asking to verify the candidate account. The link sits in the mail waiting to be clicked, and until it is clicked the thing stays incomplete, so it is something not yet done. **When the mail gives a one-time verification code it does not count**, the boundary is in that entry below
- **Bills to pay.** Credit card bills, utilities, tuition, premiums, tax bills, loans. **It counts even when the mail states no amount or deadline**. The next action for that kind of mail is to log in, look it up and pay
- **Account statements and monthly statements.** The user will check whether the account is in order, so the next action is "go check it once". Do not exclude a statement because it reads like a record. A bill's action is paying and a statement's action is checking, and both are things not yet done. What truly does not count is a **single-transaction receipt**, which is finished once read
- **Events already registered for or agreed to, as long as they have a concrete date and time.** For example a registered workshop, info session, mock interview, or a mandatory department event. **But this category must check the calendar first**, see the Check the calendar for events first section below. Write the time into `action`, for example 「今天 16:30 出席 Tesla 履歷工作坊」, because `deadline` holds only a date, and for a same-day event the date alone does not show how many hours are left.

  **When the subject or body contains 「行前通知」「行前提醒」「注意事項」「See you at」, that word itself declares that the user already agreed to go.** Sending such a mail presupposes the recipient is on the list. Do not read it as event promotion. Promotion recruits people to come, while a pre-event notice gives details to people who already agreed.

  **Such mail often tucks another action under the time and place, for example filling in a registration form, registering a ticket, paying a fee, or replying with attendance.** The snippet is just long enough to hold the date, time and place, and the action is cut off after it out of view, so a snippet showing "event time and place" does not mean "this mail has only the time and place". After ruling it a registered event, read the body to pull out the tucked-in action, see the When to read the body section below
- **A recruiting event invitation run by the employer itself counts even if not yet registered for.** The company's own info session, recruiting kick-off, campus recruiting session or internship info session counts as long as it gives a concrete session time. This is an exception to "Events already registered for or agreed to". That entry requires the user to have agreed first, this one does not, and an opening of `Hi there` naming no one makes no difference. The action is "decide whether to register, and register". `deadline` is the event day, and when there are several sessions use the nearest one still open for registration.

  **The line is who runs it, not the shape of the mail.** Employer-run counts. Third-party career fair organizers and platform solicitations do not, and those stay in the Ignore list. Employer mail relayed through a platform such as Handshake or LinkedIn still counts as employer-run, since the platform is only the delivery channel.

  **This category likewise must check the calendar first.** `FOUND` means this session is already on the schedule, which usually also means the user registered for it, so neither the "register" todo nor the "attend" todo needs creating. **But that is an inference, and it cannot cancel an action the mail requires in black and white.** The line is as follows. When the mail gives only session times, a registration link, or other "sign up yourself if you want to come" content, `FOUND` closes the whole mail. When the mail additionally **explicitly requires** filling in a form, paying, supplying documents, or replying with the attendee count, those entries still must be created, with `action` stating that action itself. The details are exactly as in the Check the calendar for events first section below, which also says that multiple actions from one mail are merged into one entry. `NOT_FOUND` and `UNCHECKED` still create a todo.
- Items with a deadline. **Do not require that "missing it causes real loss"**. Missing a scheduled opportunity counts too, for the same reason urgency is not used as a threshold. Looking only at the downside would miss the mails with the greatest impact

Examples that do not count as a next action.
- **The salutation is someone else's name.** Misaddressed mail or a forwarded third-party mail, where the matter is someone else's to handle. Details in the Mail not addressed to the user section above
- **Rejections.** The user said explicitly that rejections are not important, because the user does not need to take any action and cannot change the outcome. Purely informational bad news is not notified
- The purely informational kind of account security notice, which comes in large volume and is finished once read. This includes notices of a new device sign-in, notices of a sign-in from some location, unusual sign-in location alerts, and mail that only asks the user to confirm "was this you" when the answer is "yes". These geolocation alerts keep coming and are never notified
- **One-time verification codes.** The mail gives a string of digits or letters to copy into another screen, and sign-in codes, two-step verification codes and OTPs all count. The user dealt with it on the spot, so by the time the scheduled run sees it the matter is long over. Always treat it as handled, **not important, not a todo, no push**, put the id in `judgedIds` to close it. A code usually expires within minutes, while the scheduled run can take as long as 8 hours 15 minutes to scan it, so keeping a todo only keeps a dead code.

  **The line is whether the mail gives a code or a link**, not whether the subject says verify. A link is something not yet done, and a code is the residue of something already done. **When both are given, treat it as a link** and create a todo as usual, erring toward one extra item for the user to tick off rather than a miss.

  **A real change to the account still gets notified**, for example the password was changed, a forwarding rule was added, a recovery contact was replaced. Such a mail reports a change that already happened, not a code waiting to be entered, so it follows the account security rule above.
- Marketing CTAs, for example buy now, limited-time offer, sign up now, see more jobs
- Purely commercial newsletters, product promotion, third-party career fair solicitations. **A recruiting event invitation run by the employer itself is not in this category**, it is a todo
- **File-sharing and permission notices sent by a platform.** Mail such as "someone shared a file with you" or "you can now view or edit this file" only **tells you that you gained a permission**, with nothing to do. Canva, Google Drive, Notion and Figma all send them. **Not important, not a todo, and do not notify either.**

  **Judge the sender by the actual address, not the display name.** Platforms put the sharer's name in the display name, so it looks as if a real person sent it. A measured case had the shape `<某個人名> (Canva) <no-reply@canva.com>`. The display name has a person's name and the address is `no-reply@`, so it was sent by the platform and is not correspondence with a real person. Anything from `no-reply@` / `noreply@` / `donotreply@` or a platform domain is always treated as automated mail.

  **A sharing notice a real person sends from their own mailbox is a different matter.** That is a real address you can reply to, and it falls under the independent exception for "personal correspondence from a real person", so it counts as important and is notified (but it has no action to complete, so it does not become a todo). The exclusion is when the mail additionally asks you to finish something by a deadline, and then judge by that thing, not by the sharing itself
- Purely informational notices, single-transaction receipts, records of completed transactions. **This means records of "the thing is already done". A bill to pay is not in this category**, see "Bills to pay" above
- The registration confirmation **itself**. But **a pre-event reminder for a registered event is not in this category**, because it reminds of a commitment with a time, see "Events already registered for or agreed to" above. The line is whether this mail gives a concrete time at which you must show up or act
- Auto-replies that only say your application was received, with no next step
- **Feedback forms.** A form asking the user for feedback on an event, a course or a service, for example a satisfaction survey after an event, or mail like "please take five minutes to fill out the survey". **Not important, not a todo, no push**, put the id in `judgedIds` to close it. This holds even when the sender is the school or the organizer of an event the user attended. This category is an exception to the must-read sources above.

  **This exception covers only feedback forms, and every other form is still a todo.** Registration forms, supplying documents, attendance replies, information registration and Co-Op reporting are all outside the exception, and the volunteer training registration form case is one of them. Do not infer from this entry that "a form where filling it in or not makes no difference can be dropped". The test is whether the form's purpose is collecting feedback, not your estimate of whether it has consequences. When the same mail has a mandatory task besides the feedback form, judge by that task, and do not write the survey into `action`

**Do not use urgency to decide whether to notify.**
Mail with no deadline, due next week, or under no time pressure at all must still be notified, as long as it has a next action for the user. The user explicitly asked for this, because using urgency as a threshold would mean the user never receives "important but not urgent" mail, and that kind is often the most consequential, for example a first contact from an unknown recruiter. The deadline is information the notification text carries, not a threshold for whether to notify.

**Independent exception, personal correspondence from a real person**
This category counts as important even without a next action. As long as it is not automated, not marketing and not mass mail, it counts regardless of topic. This is the only category that does not have to pass the actionability test.

Do not stretch this exception. Individualized automated mail from an institution or company is not personal correspondence from a real person, and that kind goes back through the actionability test.

**The rejection exclusion takes precedence over this exception.** A rejection written personally by an interviewer is still not notified, because what the user wants is "don't bother me when no action is needed", not "bother me whenever a real person wrote it". But if that mail, besides rejecting, invites applying to another role or asks for a reply, it has a next action and is notified under the main criterion.

**When this conflicts with the Ignore list, the actionability test wins.**

#### Check the calendar for events first

The user's calendar is his scheduling system. An event already on the calendar needs no todo, and listing it again is just noise. **Todos exist to catch the gaps**, meaning events registered for but not on the calendar.

When a mail is ruled "an event already registered for or agreed to", first run this with the Bash tool.

```
conda run -n ML python "D:\dont_move\git_save\Daily_Task\Email_Check\calendar_check.py" --summary "<信件主旨>" --date <YYYY-MM-DD>
```

`--date` is the event day, not the day the mail arrived. The returned `status` has only three values.

| status | Meaning | What you do |
|---|---|---|
| `FOUND` | A matching event is on the calendar | **No todo for attending.** If the mail has no other action, put the id in `judgedIds` to close it |
| `NOT_FOUND` | All calendars were checked and none has it | Create a todo, with `action` saying 「把 X 加進日曆」 plus the time |
| `UNCHECKED` | Could not check reliably, `reason` says which kind, see below | Create a todo and append 「日曆未確認」 to the end of `action` |

`UNCHECKED` has four causes, all handled the same way, and `reason` is only for humans to read.

- No URL configured
- A calendar fetch failed
- **`--date` cannot be parsed.** For example a nonexistent date such as `2026-02-31`. The script deliberately does not fall back to matching on title alone, because that would let a same-named event in September cancel the todo you need to create. When you see this, go back and check the event date you computed
- **Found only in a stale cache.** Every matched event comes from old data served after a fetch failure. **Not every feed has to be down.** As long as none of the live ones match, this is the result. Old data only proves the event once existed, not that it still does, so it does not count as `FOUND`. Conversely, as soon as any single live feed matches it is `FOUND`, because existence needs only one source

**The calendar check covers only the attend item and cannot cancel other actions the mail explicitly requires.**
Filling in a registration form, registering a ticket, paying a fee, replying with the attendee count, none of these is done just because the event is on the calendar. `FOUND` only lets you skip the 「把 X 加進日曆」 entry, and the "register" item of the employer recruiting event category (reason above, the event being on the calendar means the user registered). Every other action must still be created, with `action` stating that action itself rather than attendance. Getting this rule backwards really did drop items once, because the user's calendar holds both the event itself and side events around it, so `FOUND` is easily true.

**But multiple actions from the same mail go into one todo, not one todo each.**
The script upserts keyed by message id. If one round reports two `todos` with the same id, the later one's `action` simply overwrites the earlier one, so you think you created two things but only one is kept. To keep them all, write them all into one `action` string, for example 「9-12 志工訓練 填訓練報名表並繳 200 元保證金」.

**`UNCHECKED` must never be treated as `FOUND`.** Failing to check does not mean it is there, and that direction makes the user think the event is already on the calendar and miss it. The script obeys this too. When some calendar fetches fail it returns `UNCHECKED` rather than `NOT_FOUND`, because absence cannot be proven.

Matching is "similar title + matching date", because calendar titles are not word-for-word identical to mail subjects. Recurring events (for example a class every Wednesday) use `RRULE` to decide whether the date falls within range.

`calendarIcsUrls` is a list rather than a single URL, because **one URL covers only one calendar**. Each subscribed group calendar needs its own entry. A real case is that the CodePath course invitation sender is `...@group.calendar.google.com`, which is someone else's calendar and does not appear in the main calendar's feed.

#### Ignore list
- Job digest mail whose subject starts with `[Job Scout]`. That is the user's own crawler, and the step 3 query already ignores it via `excludedSenders`, so this is insurance in case one slips through
- Job pushes and job alerts from platforms such as LinkedIn, Indeed, Glassdoor, Handshake, Lensa. **Employer mail relayed by a platform is not a job push**, including recruiting event invitations and direct messages from real people, so judge it by the main criterion
- Purely commercial newsletters, marketing promotions, product promotion, third-party career fair solicitations. **A recruiting event invitation run by the employer itself is not in this category**
- Rejections of applications
- Automatic confirmations that only say your application was received, without any next action
- System auto-mail such as GitHub notifications and social platform notifications

#### Dedupe rules
**Always dedupe by Gmail message id, and this is the only rule.**
- An id already in notifiedIds is **skipped entirely, with no notification and no todo**, and goes straight into `judgedIds` to close it. That mail was fully handled, todo included, in the round it was notified. This is also the intended effect when the user manually adds an id to `notifiedIds`, meaning "I handled it myself, don't bring it up again"
- **A different id is a different mail and must be judged on its own.** Do not merge them as a duplicate delivery because the sender and subject are the same. Job correspondence uses same-subject threaded replies heavily. For example HR first sends an OA link in one `Re: Next steps` thread and later sends a request to pick an interview slot. Both have their own mandatory action, and merging them drops one
- The notification's presentation may merge one thread into one line of text, but each mail's new actions and deadlines must be kept

#### When to read the body
Use mcp__gmail__get_email_body only when sender, subject and snippet cannot decide it and the result would affect whether to notify.

**There is no limit on body reads, and mail scanned this round must be fully judged this round.**
If you cannot decide, read it. Do not hold it back for the next round. The Usage-saving principle gives way to this rule, because reading one body costs only a few hundred tokens while missing one mail that needed action has no upper bound.

Typical cases for reading the body.
1. Old backlog in judgeNow. Judge all of it and do not defer it any further
2. Mail from a school, government, bank, insurer, landlord or student loan, as long as a mandatory task has not been ruled out
3. Mail whose snippet shows the user may need to do something but the sentence is cut off
4. Suspected mail from a real recruiter or HR person whose subject does not show whether it is an auto-mail, a rejection or a real person's reply
5. **Pre-event notices for registered events.** Such mail is always laid out with date, time and place first and the task after, and the snippet holds only the first half, so the snippet always looks purely informational. This is the only kind of mail where a snippet that looks fine means you must read the body anyway, so do not close it on the snippet

Do not read the body for pure commercial marketing or platform job alerts, since those can be excluded on form.

**Mail you still cannot decide after reading the body must not be treated as unimportant.**
Put it into `defer` in step 6, and the next round handles it first. Insufficient information means not yet judged, not judged unimportant. When `get_email_body` cannot fetch the body, it also goes into `defer`, and do not treat it as nothing. But `defer` is now the exception rather than the norm. No quota will run out, so the only legitimate reason to leave a mail for the next round is that reading it still did not settle it.

There are two reasons to store from / subject / snippet rather than only the id. First, the next round does not have to search again just to see the list. Second, once the watermark advances that mail no longer appears in any query, so with only the id the context can never be recovered, and not even a 「待確認」 notification could be written.

If the mail already has clear clues pointing to a mandatory task and only the details cannot be fully seen, you may notify directly and mark 「待確認」 in the summary rather than holding it back.

### 5. Notify

Notification has exactly one channel, the local Windows toast. **The command string is fixed, so do not rewrite it.** The allowlist matches it verbatim, and an unattended run stuck at a permission prompt is the same as the whole round not running.

**Run it with the Bash tool.** The line below is in the `powershell.exe -NoProfile -File` form and contains no `&`, so Bash does not take anything in it as the background-execution operator.

```
powershell.exe -NoProfile -File "D:\dont_move\git_save\Daily_Task\shared\notify.ps1" -Message "<那一行訊息>" -Title "Gmail"
```

**The old `& "路徑" ...` form was retired on 2026-09-10. Do not switch back to it.** With the PowerShell tool, that form got stuck at a permission prompt once on 2026-09-09 and once on 2026-09-10, even though both layers of settings files held a verbatim-matching rule. The Rules go in both layers section above records the full chain of reasoning.

A reply of `Toast sent.` means it was delivered, so write that batch's ids into `notifiedIds` in step 6. On failure, do not retry, do not try another way of writing it and do not switch to another tool. That batch stays in the queue for the next round.

**Do not call `PushNotification`.** It pushes to a phone, a route that works only with Remote Control, and the user's school has not granted that permission, so it **always** returns `Mobile push not sent (Remote Control inactive)` and sends nothing at all. Calling it only wastes one round trip per round, and it also turns "was the user actually notified" into a question that needs judging.

**No double quotes** in the message, because they truncate the PowerShell argument. Do not worry about `&`, `<` and `>`, since the script escapes them itself.

The message is one line, at most 200 characters, no markdown, fields separated by slashes, no colons.

**Wherever else this document speaks of a push, meaning a notification we send ourselves, it is always the toast above.** The job-alert pushes mentioned for platforms such as LinkedIn and Handshake are a different thing. Those are mail sent by others, so do not mix the two up.

The push message itself uses the second person 「你」, because it is shown directly to the user. Everywhere else, this instruction file always says "the user". Do not unify the two.

Mail with a next action goes first and is marked 「需動作」 (action needed). A deadline, when there is one, must be included. But having no deadline does not mean no notification is needed.

Examples
- Action needed    Gmail 需動作 1 封 / Northeastern OGS / 9-15 前補交註冊文件
- No deadline    Gmail 需動作 1 封 / Waymo recruiter 問你有無興趣 / 等你回覆
- Mixed      Gmail 重要信 2 封 / 需動作 房東要求 9-10 前確認續約 / Anthropic 面試邀約
- Uncertain    Gmail 需動作 1 封 / Northeastern OGS 疑似補件要求 / 待確認 請自行開信
- Catching up    Gmail 重要信 1 封 / Anthropic 面試邀約 / 尚有舊信未掃完

With no important mail, **do not push** and end quietly. Step 6 must still run, though.

When `commit` returns `shouldAnnounceBacklog` as true, send a 「尚有舊信未掃完」 push even if there is no important mail. The script already does the throttling (at most once every 3 rounds), so just do what it says and do not decide on throttling yourself.

The same applies when `begin`'s `config.alert` is true. Push even if there is no important mail. If there is important mail, append the alert to the end of that same message and do not send a second one. The script already does this throttling too, so do not judge it yourself.

Examples
- Config only    Gmail 設定檔異常 / config.json 不存在 / 身分辨識已停用 / 請照 config.example.json 補一份
- Appended after mail    Gmail 需動作 1 封 / Waymo recruiter 問你有無興趣 / 另設定檔 names 是空的，身分辨識已停用

### 6. commit

First write this round's results to `Email_Check/round.json`.

```json
{
  "roundToken": "begin 回傳的那個字串，原樣照抄",
  "important": [{"id": "...", "from": "...", "subject": "...",
                 "mailbox": "搜尋結果那兩欄，原樣照抄", "received": "...",
                 "summary": "一行摘要"}],
  "notifiedIds": ["toast 回了 Toast sent. 的 id"],
  "configAlerted": true,
  "judgedIds": ["判定完成、不需要再追的 id"],
  "defer": [{"id": "...", "from": "...", "subject": "...", "mailbox": "...",
             "received": "...", "snippet": "..."}],
  "todos": [{"id": "...", "from": "...", "subject": "...", "mailbox": "...",
             "received": "...", "action": "一行下一步動作",
             "deadline": "2026-09-15 或空字串", "uncertain": false}]
}
```

**`step` and `commit` read this file leniently about omitted fields, but four cases count as unreadable.**

1. The JSON syntax is broken and does not parse
2. The top level is not an object
3. `roundToken` is filled in but does not match this round
4. Any one of the six array fields has the wrong type, as described below

Everything else is fine. A missing file counts as empty, an omitted field counts as an empty array, extra fields are ignored, and leaving out `roundToken` entirely also passes. The template above is **an overview of the fields, not a list of required ones**.

**But a wrong type is the fourth unreadable case and is not covered by the leniency.** Whenever any of the six fields `important` / `failedNotify` / `defer` / `todos` / `notifiedIds` / `judgedIds` is present, it must be an array. The elements of the first four must be objects, and the elements of the last two must be strings or numbers. If any of these fails, the whole `round.json` is judged `FINDINGS_UNREADABLE`, the round is abandoned, the interval is kept, and the next round rescans it.

`null` also counts as wrong. **Omitting** a field and **filling in `null`** are different here. Omitting it is a valid empty report, while filling in `null` is a broken report.

**Wrong field types inside each object are rejected the same way.**

| Field | Type |
|---|---|
| `from` / `subject` / `snippet` / `mailbox` / `received` / `action` / `summary` / `deadline` | String. With no deadline, `deadline` gets an **empty string**, never a number |
| `id` | String or number are both accepted, since every consumer calls `str()` on it anyway |
| `uncertain` | Only a real `true` / `false`. **The string `"false"` is rejected**, because the GUI would treat it as true and show 「待確認」 (to be confirmed) |
| `firstDeferredRound` / `createdRound` | Integer. These are the script's own bookkeeping fields, and **you should not fill them in at all** |

If any field does not match, the whole file is sent back. **Filling in `null` does not count as a mismatch.** It is the same as omitting that field, since the script drops `null` anyway.

A wrong type cannot be silently treated as empty the way an omitted field is, because what can be rescued differs between the two. An omitted field drops backlog items that are **already in a queue**, and the "keep by default" rule holds them for the next round. A wrong type drops mail that **this batch just found and that has not landed yet**. That mail was never in any queue, and `step` is about to retire this interval, so once the watermark crosses it, it can never be found again. So the only option here is to abandon the whole round and rescan, which is the only way to keep the mail.

An omitted field does not kill the round with `FINDINGS_UNREADABLE`. Its cost, written in each field's own description below, is that the mail stays in a queue or a notification is missed, not that the whole round aborts.

**Every `important` / `defer` / `todos` entry must carry the Gmail message `id`.** An `id` given as an empty string, `None` or `null` all count as missing, and that entry becomes an orphan that can never be resolved, caught only afterwards by the `itemsMissingId` alert. Search results already carry `id`, so just copy it.

`roundToken` must copy the value `begin` gave verbatim. The script uses it to confirm that these findings belong to this round, because the two schedules share one `round.json`, and a catch-up run or a manual run can interleave two rounds. A token mismatch is treated as a read failure and the interval is kept, which is the safe direction. Leaving it out disables the check.

(`failedNotify` is still accepted, but you do not need it. `important` already covers its purpose, because mail judged important lands right away and is removed only once notification succeeds.)

**The queue rule is "keep by default".** Any backlog item not listed in any of the fields above is left untouched by the script for the next round. This is deliberate, because this round may die before `commit`, and if "not reported" were treated as "handled", mail nobody judged would silently vanish.

Each of the three queues has its own exit condition. They are not the same one.

| Queue | Which `begin` field it appears in | What you report that removes it | What the user does that also removes it |
|---|---|---|---|
| To-notify | `notifyNow` | `notifiedIds`, meaning the toast was definitely sent | Ticks it done in the GUI |
| To-judge | `judgeNow` / `judgeOverdue` | `judgedIds`, or listing it in `important` (judged important, it moves into the to-notify queue), or a toast successfully sent for it | Ticks it done in the GUI |
| 待辦 (todo) | Not in `begin`, it is on the 待辦清單 (task list) | Nothing does. **Never try to remove it** | Ticks it done in the GUI |

The rightmost column is not your business. It is listed only so you know that "I did not report it, yet it is gone" is normal. Ticking it done is the user declaring the matter finished, and the script clears that id from all three queues at once.

So `judgedIds` is your main exit for the **to-judge queue**, but not the single exit shared by all three queues. When the same id is listed in both `judgedIds` and `defer`, the script leans toward keeping it, and `defer` wins.

`important` holds **every message in this batch judged important**, whether or not it goes into 待辦. `summary` is the one line of text you put into the push.

**All three of `important`, `todos` and `defer` must carry `mailbox` and `received`.** Copy both straight from the search results. Do not infer them yourself, and do not convert the time yourself.

`mailbox` is the mailbox this message was originally sent to, and the user relies on it to know which account to go back to for the original. `scout` is the forwarding hub, and the sender says only who sent it, not which account the mail now sits in. When mail was sent straight to the hub itself, that field is the code name `scout`, since the MCP has already replaced the real address, so copying it does not break the "do not write out the address" rule.

`received` is when that mailbox received the message, and the MCP already gives it in local time. **Do not use `date` in its place.** `date` is written by the sender, its lag behind internalDate has no upper bound, and Gmail sorts and displays by internalDate, so only `received` matches the time the user sees in the mailbox.

Leaving out either field only costs that row one line of hint and does not fail the round.

`from` and `subject` must be filled in too. After a successful notification, an entry with no matching todo is put by the script into 待分類 (untriaged) on the 待辦清單 to wait for the user to grade it, and that row is displayed from these three fields, with `summary` used as the row's action text. Filling in only `id` and `summary` causes no error, but the user sees a row without being able to tell which message it is.

When the same message is also listed in `todos`, the script keeps only the todo you wrote and adds no extra row. **This needs no cooperation from you. List it in both as usual.** For how many entries the script created from pushes, see `filedFromPushThisRound`.

**This field must be written into `round.json` before calling that batch's `step`.** `step` commits it atomically together with the interval retirement, so "judged important" is persisted at that moment, and only after that is `notifiedIds` the condition for removing it. Reverse the order and there is a fatal window. Once `step` retires the interval, the watermark crosses that message and it never appears in any query again. If it has not landed by then, a process crash loses it permanently, without even an alert.

**The exception for personal correspondence from a real person must go into `important`.** It does not go into `todos` (it has no action to complete), so `important` is its only landing place. Leaving it out means missing an important message.

Set `configAlerted` to `true` only when this round's `begin` gave `config.alert` as true and that config-problem part was **definitely pushed successfully**. In every other case leave it out. `commit` uses it to decide whether to record the new config state, and recording it means the next round no longer alerts.

`judgedIds` holds ids that **are fully judged and need no further queueing**, including those judged unimportant and those in `judgeOverdue` that already got a 「待確認」 notification. **This is the exit from the to-judge queue.** Leaving one out keeps that message queued, reappearing in `judgeNow` to be judged again every round. Mail judged important does not rely on it, since listing it in `important` moves it into the to-notify queue, as the table above shows.

`failedNotify` holds mail judged important whose toast reported failure or an unclear result. **The summary must be stored along with the id**, because after the watermark advances that message never appears in any query again, and with only the id the notification text can never be written.

`todos` holds mail **with a next action for the user to take**. It goes onto the 待辦清單 for the user to tick off.

- For `action`, use the same one line you were already going to write into the push, and **do not write a second version**. The judgment has already been made and this only lands the same result, so it adds almost no tokens
- **Do not put the standalone exception for personal correspondence from a real person into todos.** That kind has no action to complete, so on the list it becomes noise that can never be ticked off. It is still pushed. It just does not go into 待辦
- `deadline` uses the **full `YYYY-MM-DD`**, for example `2026-09-15`. With no deadline, fill in an empty string. The year is required. With only `09-15` the GUI cannot tell "just overdue last month" from "next September", and overdue items are exactly the ones that most need to sort first
- `uncertain` is for the 「待確認 請自行開信」 kind, where information is insufficient but there are clues of something that must be done
- **One item per message, keyed by message id.** Do not merge by subject or thread. The reverse holds too. When one message has several actions, combine them into this entry's `action` and never split them into two items with the same id, because the upsert would overwrite one of them. See the Check the calendar for events first section for details
- **Mail whose salutation names someone else does not go into `todos`.** That is not the user's task
- You **never need to delete a todo, and must never try to**. Only "user ticks + script cleanup" removes items. A message once judged 待確認 that later turns out to be marketing stays for the user to tick off. Do not withdraw it automatically, since that could withdraw something the user still cares about

**Write all three of `important`, `todos` and `defer` into `round.json` as soon as each batch is processed, not at the end.** `defer` is especially easy to overlook. A message that still cannot be judged after reading its body is neither `important` nor yet a `todo`, and `defer` is its only landing place. Once `step` retires the interval, the watermark crosses it, and if it has not landed by then it is gone for good. `step` commits the todos currently in `round.json` atomically together with the interval retirement. If you hold everything until the end to write it, a crash midway loses that batch's todos permanently while coverage has already moved past them.

`defer` holds mail that still cannot be judged after reading the body, or whose body cannot be fetched at all. Store `from` / `subject` / `mailbox` / `received` / `snippet` rather than only the id, again because the context cannot be retrieved later. If the message later becomes a todo, `mailbox` and `received` can only be copied from here, since the watermark has already passed it. `firstDeferredRound` **must not be filled in by you**. The script keeps the earliest one. Filling it in yourself resets the waiting clock, so the "waited too long, send 待確認" rule never fires.

Then run `... statemachine.py commit` with the Bash tool.

Among the fields `COMMITTED` returns, besides `shouldAnnounceBacklog` used in step 5 and `configStatus` / `configAlertStillOwed` explained in step 1, the following are also worth watching.

- `shouldOpenTodoList` is what step 7 follows to decide whether to open the 待辦清單. Do not recompute it yourself
- `newTodosThisRound` is how many todos this round added. It may go into the push, but it is not the basis for opening the window
- `filedFromPushThisRound` is how many of the messages pushed this round were put into 待分類 because they had no matching todo. It is purely informational. **Do not send an extra push because it is nonzero**, since those messages were already pushed this round
- `untriaged` / `triagedThisRound` are how many items 待分類 still holds and how many the user graded this round. Purely informational
- `followedThisRound` is how many items the user moved into or out of 追蹤中 (following) this round. Purely informational
- `archiveBlocked` / `restoreBlocked` / `triageBlocked` / `followBlocked` all four mean something the user clicked in the GUI did not get done. A non-empty string is the reason, and an empty string means nothing is wrong. **Do not miss a single one.** Handle them all the same way, per the table below

| Field | What the user clicked | The symptom they see |
|---|---|---|
| `archiveBlocked` | Ticked done, or clicked 封存 (archive) in 待分類 or 追蹤中 | Ticked items never disappear |
| `restoreBlocked` | Clicked 復原 (restore) in 已封存 (archived) | Two kinds, depending on the string. `restore file unreadable` or `archive unreadable` means it exited before acting, so **nothing happened** and the click had no effect. `archive not writable` means the item **is already back in 待辦** and only the 已封存 copy was not cleared, so the same message shows in both sections |
| `triageBlocked` | Picked 緊急 (urgent), 重要 (important) or 普通 (normal) in 待分類, or clicked 重新分類 (reclassify) on the 待辦清單 | The screen keeps showing 「已排定」, and after the next round it is still not written in |
| `followBlocked` | Clicked 轉追蹤 (move to following) on the 待辦清單, or clicked 回到待辦 (back to todo) or 「天後提醒」 (remind after N days) in 追蹤中 | The screen keeps showing 「已排定追蹤」, 「已排定回到待辦」 or 「N 天後提醒 已排定」, and after the next round it is still not written in |

**Treat all four as "this round did not finish", and do not assert whether it will fix itself.** One string covers two fates. `archive not writable` may mean another round changed the archive file at the same time and this one lost the version check (temporary), or that the file truly cannot be written (it will not fix itself). **The return value cannot tell which, and you must not guess.** Appearing several rounds in a row only means it is worth a look, not that the latter is confirmed, because each round losing a version check on its own also prints the exact same string. Relay it to the user as it is and let them decide whether to investigate.

For a non-empty string, append that sentence to the end of the push to tell the user, and **push even if this round has no important mail**, because they will think they did not click it properly. Typical `archiveBlocked` values are `tick file unreadable` / `archive unreadable` / `archive not writable`, and the other two have a similar shape. Do not send a separate second push for them. Appending to the end of the same one is enough
- `itemsMissingId` is how many queue items have no usable `id`. If nonzero, add the sentence 「有 N 筆缺 id 無法追蹤」 to the end of the push, but **do not send a separate push for it**, since it will not get better on its own and is not urgent

A nonzero `itemsMissingId` is almost always caused by some earlier round leaving out `id` in its report. Such items can never be deduped by id, ticked done or fetched back to read, so they just sit stuck in the queue. So the real point is **prevention**. See the rule above that every entry must carry the Gmail message `id`.

### 7. Open the task list when 待分類 has items

When `commit` returns `shouldOpenTodoList` as `true`, run the `open-task-list.ps1` command from The task list GUI section. When it is `false`, **do not open it**, and do not recompute the condition yourself.

There is only one condition, that 待分類 still holds ungraded mail, whether it came in this round or earlier. Ungraded mail does not leave that section by itself, so if the user does not handle it, the list opens again every round, and that is what the user wants. There is currently no quiet period, so the early-morning round opens it too if there is anything. Report `newTodosThisRound` and `untriaged` as they are, but do not use them to decide on your own.

That command is fire-and-forget. Do not wait for it, do not read its output, and do not retry because it failed. **When port 8765 is already in use, it exits at once by itself, no matter who holds the port.** It only tests whether the port answers and does not identify who is on the other end. In the vast majority of cases that is the page the user already has open, which refreshes itself every 30 seconds and will see this round's new todos automatically. But if some other program holds it, this round simply opens no window, and you still must not retry or launch it another way.

## Rulings the user confirmed

Every entry below was ruled by the user personally after seeing the actual mail. **Do not reclassify them, and do not change this section without the user's explicit consent.** When the judgement rules conflict with these rulings, the rulings win, and you must go back and fix the rules, not the rulings.

Drift happens because each of these cases reads like its opposite, so every entry comes with its dividing line.

| Mail | Ruling | Dividing line |
|---|---|---|
| A bank's credit card statement notice | **Is a 待辦 (todo)** | A bill asks for something not yet done. The mail stating no amount or deadline does not change that. The next action is to log in, look and pay. Do not exclude it because it reads like a transaction record |
| Pre-event reminder for Tesla Supercharging Your Resume Workshop | **Is a 待辦, but check the calendar first** | Having registered equals having committed. If it is already on the calendar, do not create a 待辦. What the user wants is to catch what is missing. Do not exclude it because it reads like event promotion or like a registration confirmation |
| A volunteer team's 「煩請補交 Facebook 連結」, opening with `Hi Hsiao Ming` | **Not a 待辦, no push notification** | The greeting is someone else's name. That task is someone else's to do. Do not handle it as 「待確認」 (to be confirmed). Close it outright |
| 「【行前通知】9/12 Taiwan Tech Summit 志工訓練」 from `Volunteer TaiwanNext <volunteer@taiwannext.org>`, opening with `Dear Taiwan Next Volunteers` | **Is a 待辦**, and the action is filling in the training registration form, not attending | Ruled on 2026-09-10. The original judgement of not important was wrong. Three dividing lines. One, **the word 「行前通知」 by itself means the user has already agreed to go**, since such mail presupposes the recipient is on the list. Two, `Dear ... Volunteers` is **a group greeting, not someone else's name**, unlike the `Hi Hsiao Ming` mail in the row above, and a group greeting is not evidence for exclusion. Three, the snippet is cut off right after the date, time and place, so **the thing to do comes later and cannot be seen**, and therefore mail like this must not be closed from the snippet. The user's own words were 「行前通知代表這是我會參加的活動，所以這其實是我要注意的事情」 |
| Application rejection letter | **Not important** | There is no next action, and it cannot change the outcome. A rejection written personally by a real person is likewise not notified |
| Sign-in from some location, sign-in from a new device, confirm 「這是你嗎」 | **Not important** | Location alerts keep coming, in high volume, and are done once read. But a setting that has already been changed without the user having done it must be notified |
| Handshake job alert pushes with the user's name in the subject | **Not important** | Marketing mail personalizes too. The name only identifies the recipient and is not an importance signal |
| The ServiceNow Early in Career Recruiting Kick Offs invitation from `Lauren Martinez via Handshake` | **Is a 待辦** | Ruled on 2026-09-17. The original judgement of not important was wrong. Any **recruiting event the employer runs itself** is classed as important and goes into 待辦, **without the user having to register first**, and neither an opening of `Hi there` that names no one nor a Register link counts toward exclusion. The dividing line is who runs it. One the employer runs itself counts, and solicitation by a third-party career fair organizer is still excluded. Being forwarded through Handshake does not matter, since the platform is only the delivery channel. The user's own words were 「只要判斷是招募的活動，就應該要歸類到重要的郵件，然後進待辦清單」 |
| Share notification from `<某個人名> (Canva) <no-reply@canva.com>` | **Not important, no push notification** | In essence it says you gained a view or edit permission, and there is nothing to do. **The sender is not a real person.** The display name has a person's name but the address is `no-reply@`, and the sender is always judged by the actual address. If a real person sends the same share notification from their own mailbox, that counts as important and must be notified |
| A bank's monthly consolidated statement | **Is a 待辦** | The user will check whether the accounts are in order, so the action is to go through it once. The reason differs from a payment bill, but it is a 待辦 all the same |
| A card issuer's secure message center 「有新訊息請登入查看」 | **Not yet ruled, rules deliberately unchanged** | The user said this one is indeed important, but is unsure whether future mail with similar content will all be important, so no rule is set yet, and the decision waits until more samples accumulate. **Do not add a rule on your own, and do not file it under any existing category. For what to do before a ruling, see Cases not yet ruled on below** |
| Mail carrying a one-time verification code | **Not important, no push notification** | Ruled on 2026-09-22. The user deals with it on the spot, and the code expires in minutes while the schedule may take as long as 8 hours 15 minutes to see it, so any 待辦 created from it is always dead. **The dividing line is code versus link.** A verification link is still a 待辦 as before, and when one mail gives both, treat it as a link. Notices that an account was actually changed are unaffected by this and are still notified. The user's own words were 「如果是有驗證碼 我覺得算例外 因為我通常當下就會處理 可以算在已處理裡面」 |
| 「Thank You For Attending the Career Symposium!」 from Career Development Silicon Valley, whose body asks you to fill in a five-minute survey | **Not important, does not go into 待辦, no push notification** | Ruled on 2026-09-24. The original judgement of 待辦 was wrong. Feedback forms do not go into 待辦, even when they come from the school and even when the user attended that event. **The exception stops at feedback forms.** Registration forms, requests to submit missing documents, attendance replies and any other form are still a 待辦. The user's own words were 「如果是 fill out survey 可以不用加到 代辦事項」, later adding 「不用加到代辦事項的表單只有意見回饋表單」 |

### Cases not yet ruled on

The rows marked "Not yet ruled" in the ruling table mean **the user has deliberately not set a rule yet**, not that one was left out. But having no rule does not mean having no behaviour, so what to do in the meantime is written here.

**Judge it by the ordinary actionability test, and do not treat it specially because it has not been ruled on yet.** Take the card issuer's 「有新訊息請登入查看」 as an example. It has a next action addressed to the user (log in and look), so it passes the test and becomes a 待辦. That is the expected result.

**Do not do these three things**
- Do not skip it or leave it un-notified because it is not yet ruled. That would keep samples from ever accumulating, and judging from samples that actually show up is exactly what the user wants
- Do not mark it `uncertain` or 「待確認」. That is for mail the user definitely has to act on but whose details cannot all be seen, not for mail whose rule is not set yet
- Do not add a row to the ruling table yourself, and do not file it under an existing category

This default leans toward one extra 待辦 rather than one missed mail, the same direction as the Top principle, so even if the final ruling is not important, the cost is only the user checking off a few items by hand.

A ruling comes about when the user, looking at the task list, says directly which items were misjudged. Only then is the ruling table updated.

## The task list GUI

When the user wants to view or check off 待辦, they run this themselves.

```
conda run -n ML python "D:\dont_move\git_save\Daily_Task\Email_Check\task_list\task_list_gui.py"
```

It starts a Flask server bound only to 127.0.0.1 and opens the browser automatically, and it can simply be closed after viewing. **When the page is closed the process ends by itself**, so a scheduled task that starts it leaves no server that nobody shuts down. If no page connects at all, it also shuts itself down after 90 seconds, so a browser that failed to open does not leave an orphan process.

When a scheduled task starts it, **only this form may be used, and the string is fixed and must not be rewritten**. Run it with the Bash tool.

```
powershell.exe -NoProfile -File "D:\dont_move\git_save\Daily_Task\shared\open-task-list.ps1"
```

Both the `pythonw.exe` part and the `Start-Process` part have moved into `shared/open-task-list.ps1`, together with the reasons for not opening a console window and for letting the server detach from this round's session and live on its own. The interpreter path is built inside the script from `$env:USERPROFILE`, so this document contains no local user name.

The string is fixed verbatim because the allowlist matches verbatim, and an unattended run stuck at a permission prompt is the same as the whole round not running. This `-File` form contains no `&`, and uses the same invocation style as `notify.ps1`, which has been tested to work.

**A scheduled run must never start it with the `conda run` form.** `conda run` waits for the child process to exit, so the whole round would hang there until the user closes the page. The script uses `Start-Process`, which does not have this problem.

### 待分類 and the task list

From top to bottom the page has four sections, 「待分類」 (untriaged), 「待辦清單」 (task list), 「追蹤中」 (following up) and 「已封存」 (archived). Before 2026-09-27 there was also a section 「重要事項」 (important items), holding mail that had been pushed but had no 待辦, and anything not handled there was swept into 已封存 automatically in the next round. The user worried that things would be cleared before they had a chance to look, so it was changed to the current design, where nothing leaves 「待分類」 by itself.

- **待分類** is mail not yet given a level. Mail you judged to be a 待辦, and mail that was pushed but has no matching 待辦, both land here first. Each row has four buttons, 「緊急」 (urgent), 「重要」 (important), 「普通」 (normal) and 「封存」 (archive). Anything not given a level just stays, and is still there in the next round
- **待辦清單** holds what has been given a level, sorted by 緊急, 重要, 普通, and within one level by due date. Each row has only the 「已完成」 (done) checkbox and two buttons, 「轉追蹤」 (move to following up) and 「重新分類」 (reclassify). The three level buttons are deliberately left out to avoid mis-clicks. 「重新分類」 sends it back to 「待分類」
- **追蹤中** (following up) holds items where the user has done their own step and is waiting for the other side to respond. Items arrive from 待辦清單 via 「轉追蹤」 and keep their original level. Each row has two buttons, 「回到待辦」 (back to 待辦) and 「封存」. 「回到待辦」 returns it to its original level. A row that has been here for three full days without being handled turns red, and on a red row the user can enter a number of days and press 「天後提醒」 (remind in N days) to postpone turning red until that day. Turning red is only turning red. **It does not cause the task list to open, and do not push a notification for it**
- **已封存** holds what was checked off as done or archived directly. It is cleared after three days, and until then it can be 「復原」 (restored). An item with a level returns to its original level, one archived from 「追蹤中」 returns to 待辦清單 rather than 追蹤中, one archived without a level returns to 「待分類」, and old mail archived before 2026-09-27 is always treated as 「普通」

Only the user can set the level. **Any `priority`, `followSince` or `followRemindAt` written into `todos` in `round.json` is dropped by the script anyway, so do not write them.** Your only job is to fill in the `from` / `subject` / `summary` of `important` and the `todos` as usual. At the 2026-09-27 switchover, the 待辦 already on the list and the mail in 「重要事項」 were all converted to 「普通」.

The division of writers is the core of this design. Do not break it.
- `state.json` is written only by statemachine, and the GUI only reads it
- `tasks-archive.json` is written only by statemachine, and the GUI only reads it
- `tasks-checked.json` (checked off as done, and 封存 from 「待分類」) is written only by the GUI, and statemachine only reads it
- `tasks-restore.json` (復原 from 已封存) is written only by the GUI, and statemachine only reads it
- `tasks-triage.json` (assigning a level, and 重新分類) is written only by the GUI, and statemachine only reads it
- `tasks-follow.json` (轉追蹤, 回到待辦 and 延後提醒 (postpone reminder)) is written only by the GUI, and statemachine only reads it

The last four are commands the user issues on a row. **You must never write any of these four files.** They stand for "the user says it is done", "the user says pull it back", "the user says this mail is this level" and "the user says this is waiting on the other side". The scheduled LLM has no standing to declare these four things on the user's behalf, and the permission settings file also blocks edits to these four files.

Each file has only one writing **component**, and together with atomic file replacement, a reader always sees either the complete old file or the complete new file. Letting both the GUI and statemachine write the same file would cause a lost update, and what gets overwritten could be exactly the new 待辦 this round just produced.

**This division stops the GUI and statemachine from clobbering each other, not scheduled runs from clobbering each other.** `state.json` and `tasks-archive.json` each have statemachine as their only writing component, but that component may have two scheduled processes running at once, so each file is additionally guarded by its own rev check. **What happens after a loss differs between the two files, so do not conflate them.**

| File | When it loses the rev check | What you see |
|---|---|---|
| `state.json` | The whole round aborts, because state is the round's only landing point | `STATE_CHANGED_ABORT` |
| `tasks-archive.json` | Only that one archiving action is abandoned, and the rest of the round carries on | The corresponding non-empty `*Blocked` string |

The archive side can abandon just one step because those actions can all be redone. The checked file and the restore file are still there and the next round still sees them, so archiving one round late loses nothing. The rev check is detection, not a lock, and the remaining window and its cost are written in Known limitations below.

## Known limitations
The two scheduled tasks share one state, and nothing at the instruction level can lock it. After the app has been shut down, tasks that missed their slots run their catch-ups at the same time. That case is now blocked by `ROUND_ALREADY_RUNNING` returned from `begin`. Only the first round actually starts, the others run `wait` instead until it finishes, and if it dies one of them takes over. What cannot be blocked is the two rounds' `begin` landing in the same window of a few milliseconds, that is, one round finishes its check and, before it writes the progress file, the other round also finishes its check. Then both rounds proceed, but `begin` writes `state.json` first and only then touches `round.json` and the progress file, so if both rounds read the state before the other one saved, the round that writes later gets `STATE_CHANGED_ABORT` and stops without touching either of the first round's two round files at all, and the first round runs to completion as usual. Before 2026-09-27 the round files were written first, so the losing round would also overwrite the first round's token and both rounds died together. One narrower ordering still cannot be blocked. The later round happens to read the state in the few microseconds after the first round has written `state.json` but before it has written the progress file. Then both rounds can write state, and next they overwrite each other's round files. That belongs to the same class as the rev check's remaining window in the paragraph further below. The probability is extremely low, and likewise the consequence cannot be guaranteed to be only a duplicate notification.

**"Abandoning the whole round" refers to the one call that aborted, not to everything that round did.** Every earlier successful `step` has already atomically saved its own batch of findings together with the retirement of its interval, and the abort does not roll them back, nor does it need to, because that mail has already landed. The only thing actually kept for rescanning is the batch at the moment of the abort, whose interval was deliberately not retired. This is the same reasoning as for `FINDINGS_UNREADABLE` and `NO_ROUND_IN_PROGRESS`.

**But the rev check is detection, not a lock, and the remaining window has not been eliminated.** A few microseconds still separate its reading of the disk rev from its writing of the file, and if two rounds land in those microseconds together there is still a lost update, and what gets overwritten is the entire state the earlier round saved, including its 待辦 for intervals that the later-writing round has already marked retired, so nothing scans those intervals again. In that case **mail is missed**, so do not claim any more that the worst case is only a duplicate notification. The probability is extremely low and the cost extremely high. This risk is accepted for now. A real fix would need a file lock, which is not being handled yet.

The main query does not scan Spam or Trash. If mail from a recruiter or the school is misjudged as spam by Gmail, this procedure cannot see it.

`snippet` is the preview string Gmail gives unmodified. Quoted-printable soft line breaks are not undone, so `=` plus a space stays inside and splits words in two (observed in practice as `t= ime` and `conside= ration`). Judging meaning is unaffected, but **before copying wording from the snippet into a push notification, clean these traces out first**, or the user will see garbage.

## Forbidden
Only read mail. Never reply, forward, archive, delete, add labels, mark as read, or change any Gmail setting.
