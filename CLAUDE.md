# Daily_Task

## Tests to run after changing code

The five files under `Email_Check/test/` run directly as files, with no pytest, and take about 36 seconds in total. After changing `statemachine.py`, `task_list/task_list_gui.py` or `calendar_check.py`, run the matching one.

```
conda run -n ML --no-capture-output python "D:\dont_move\git_save\Daily_Task\Email_Check\test\test_statemachine.py"
```

`--no-capture-output` is required. The tests print Chinese, and when conda reprints the child process's output it goes through cp950 and raises UnicodeEncodeError. That looks like a test failure even though the tests themselves passed.

After changing `shared/deploy_skills.py`, run `shared/test_deploy_skills.py` the same way. It works only inside a temporary directory and never touches the real scheduled tasks.

## Do not edit a scheduled task's SKILL.md directly

Edit the source under `Scheduled_Tasks/` in the repo and deploy it with `shared/deploy_skills.py`, as the README section "How scheduled tasks are wired" describes. An edit made directly to the deployed copy has no version control, and the next deploy treats it as an outside edit and refuses to overwrite it.

## The 收件 and 收信 columns in the task list

Their values are not computed in this repo. `收件` is the mailbox a message was originally sent to before it was forwarded here, and `收信` is when that mailbox received it. The Gmail MCP computes both, and the scheduled LLM copies them into `round.json` unchanged. The derivation rules are `_origin_mailbox` and `_received` in `D:\dont_move\git_save\gmail_mcp\server.py`. That directory has no version control, so a bad change there has no history to recover from.

Changing those rules means running `gmail_mcp/test_origin_mailbox.py`. It covers two header shapes that were actually misjudged, including a bulk mail sent entirely through Bcc with no `To` at all.

## The open-mail link on a task list subject

The subject link opens the message in the mailbox that originally received it, not the copy in the forwarding hub, so that a reply goes out from the original address. The link is not computed in this repo either. `task_list_gui.py` calls `origin_links` in `gmail_mcp/server.py` in the background, which looks it up over IMAP without involving the scheduled LLM. Each result, including "this message has no link", is saved to `Email_Check/task_list/mail-links.json`, each todo is looked up only once, and only the web page reads or writes that file. A failed lookup is retried once after 5 seconds and then given up, so those messages get no link this time, nothing is saved, and the next page load tries again. The page is started under `pythonw` with no console, so the reasons a todo got no link, meaning each failed attempt, each give-up and each Gmail mailbox with no credentials, are appended to `Email_Check/task_list/mail-links.log`, which keeps its last 200 lines. A message the original mailbox holds in more than one thread links to the newest thread rather than to nothing. Only the Gmail mailboxes whose credentials are in `gmail_mcp/.env` get a link that opens the message directly. The school's Exchange mailbox gets a semi-automatic search instead. Clicking the subject copies `Subject:"<subject>" AND From:<sender> AND received:<M/D/YYYY>` to the clipboard and opens Outlook on the web, and pasting it into the search box and pressing Enter lists only that message. The date is required, because when one sender reuses the same subject, the search without it lists several messages. Two other routes were tried on 2026-10-02 and neither works, so do not try them again. The first passes the search in the URL. Six URL forms were tried, among them `outlook.office.com/mail/deeplink/search?query=`, `/mail/search?query=`, `/mail/?q=`, `/mail/inbox?q=` and `outlook.office365.com/owa/?path=/mail/search&query=`, with either the subject or `Subject:"…" AND From:…` as the condition, and none of them listed the message in a browser signed in to the school account. The second looks up the message's `webLink` through Microsoft Graph by its `internetMessageId`, and even Microsoft's own Microsoft Graph Command Line Tools, signing in with a device code, need administrator approval from the school (Approval required).

Changing those link rules means running `gmail_mcp/test_origin_links.py`. After changing the link format or the rules, increase `LINKS_VERSION` in `task_list_gui.py` by one. When the page sees a different version it drops the whole cache and looks everything up again. Otherwise the old links and the saved "no link" results stay in use until each todo leaves the list.

## Do not compress INSTRUCTIONS.md to save tokens

On 2026-10-02 the user decided to translate `Email_Check/INSTRUCTIONS.md` into English to save tokens. A translation keeps every rule and every confirmed judgement, so it is not what the rest of this section rejects. The text the user reads, meaning the toasts and the task list fields in `round.json`, stays in Traditional Chinese, set by the language paragraph deployed into SKILL.md and stated again in the file's Goal section. The UI labels the code and the web page match, such as 待分類, 追蹤中 and 普通, stay in Chinese inside the English text.

Compression was measured and rejected on 2026-09-20. The four Gmail tasks did not rank as a single line in the 24-hour usage attribution of Claude Code `/usage`, and three paragraphs that looked as if they were only for people each turned out, when checked one by one, to carry runtime rules. **Rarely used does not mean unused at runtime.** Compressing the procedure and reading less of each message body were rejected too. The first would change judgements the user confirmed. The second breaks the rule that missing a message that needed action is far worse than sending one extra notification, and this task fails silently.

To reopen this, first bring new attribution data showing that scheduled runs take a significant share, or a run failure traceable to how the instruction file is organized. Without either, do not measure again.

That measurement was taken when there were only four runs a day. Since 2026-09-28 there are two scheduled tasks, one at 50 and one at 20 minutes past each hour, and a slot that comes within 45 minutes of a completed round is blocked by a hook before the model starts. In practice about one round finishes per hour, roughly sixteen in the daytime, so that attribution no longer reflects current usage. This alone is not a reason to reopen, which still needs new attribution data first.
