# Gmail MCP Server (read-only)

A personal Gmail MCP server for Claude Code. It talks IMAP, and one server reads two mailboxes.

Every command it sends is a read. `SELECT` passes `readonly=True` and every fetch uses `BODY.PEEK`, so a message it reads is not marked as read. **This is a property of this program, not a limit Google enforces.** The Security section below explains why.

## Two mailboxes

| alias | mailbox | role |
|---|---|---|
| `scout` (default) | The forwarding hub's Gmail, with its address only in `.env` | The collection point, which receives every message forwarded from the main Gmail and from Outlook |
| `main` | The main Gmail, with its address only in `.env` | The main mailbox, used only to look up mail from before forwarding was set up |

Every tool takes an `account` parameter, which defaults to `scout`.

## Why not OAuth

This server used to run on the Gmail API with OAuth. The reasons it moved away are written here so nobody moves it back.

While an OAuth app stays in the Testing publishing status, its refresh token **expires after seven days**, and renewing it needs a person to open a browser and authorize again. That is fatal for unattended automation. Lifting the seven-day limit means publishing the app to production, and Google then asks for an Application home page and a Privacy policy URL whose domains have verified ownership in Search Console. This project has no domain of its own, so it cannot pass that step.

An app password never expires and needs no Cloud project, no consent screen and no review. No feature is lost, because Gmail's IMAP supports the `X-GM-RAW` extension, which takes the same syntax as the Gmail search box.

## Layout

- `server.py`, the MCP server itself, over stdio
- `.env`, the two accounts and their app passwords, **kept out of git**
- `credentials.json` and `token.json` are leftovers from the OAuth era. No code reads them any more, so they can be deleted

### Three tools

```python
get_latest_email(account="scout")
search_emails(query="", max_results=10, account="scout")
get_email_body(message_id, account="scout")
```

The `query` of `search_emails` takes Gmail search syntax as is, for example `label:career_event in:anywhere`, `from:github.com` or `subject:interview newer_than:7d`.

A search that finds nothing returns an empty list, which Claude Code shows as no output at all. A search that fails always raises and never returns an empty list as well. The scheduled check takes 0 messages to mean that time window has been scanned, so a failure read as 0 messages would leave the mail in that window unscanned forever. `get_latest_email` works the same way and raises on failure instead of reporting an empty inbox. The tests are in `test_search_emails.py`.

Besides `subject` / `from` / `date` / `snippet`, the returned metadata carries `list_unsubscribe`. Bulk mail has this header and personal mail does not, which makes it the cleanest signal for whether a message is promotional.

There is also `mailbox`, the mailbox a message was originally sent to before it was forwarded here. `scout` is the forwarding hub, so the sender alone does not say which account holds the original, and this field fills that gap. Exchange forwarding stamps `X-MS-Exchange-ForwardingLoop`, whose first segment is the forwarding mailbox. Gmail-to-Gmail forwarding leaves `Delivered-To` and `X-Forwarded-For`. Both give the full address. Only when neither stamp is present does it fall back to `To`, and the `To` of bulk mail is the sender's own list. In that case it returns only the domain, checked against the envelope sender rewritten to `<tenant>.onmicrosoft.com`, and the tenant wins when the two disagree. When a message was sent straight to the account itself, it returns the account alias instead of the real address, because the caller displays this field.

The `ForwardingLoop` rule is not an optional extra. Bulk mail often sends its whole recipient list through Bcc, so such a message has no `To` at all and there is nothing to fall back to. Only this stamp can tell which mailbox the message was forwarded from.

The last one, `received`, is when this mailbox received the message, taken from IMAP's `INTERNALDATE` and converted to local time. It is used instead of `date` because the sender writes `date` itself, so how far it lags behind internalDate has no upper bound, and Gmail sorts and displays by internalDate. Both fields stay. `date` is what the sender claims and `received` is when the mailbox actually got the message.

`message_id` is `X-GM-MSGID`, which is unique across the account and does not change when a message moves between folders. It is tied to the account, so the `account` passed to `get_email_body` must be the one the id came from.

## Mail links for the task list

`origin_links(hub_ids)` is not an MCP tool. It is a function the `Daily_Task` task list page imports directly. It takes `scout` `X-GM-MSGID` values and returns a Gmail web link that opens the copy in the mailbox that originally received the message, so a reply goes out from the original address.

Gmail ids are tied to a mailbox, and a `scout` id opens nothing in the original mailbox. The two copies are matched by the `Message-ID` header, which forwarding leaves unchanged. It first reads that header and the original mailbox from `scout`. It then logs in to the original mailbox, finds that copy's `X-GM-THRID` with `rfc822msgid:`, converts it to hexadecimal and appends it after `#all/`, with the account picked by `authuser=`.

Only the Gmail mailboxes whose credentials are in `.env` get a link that opens the message, which today means `main`. Mail sent straight to `scout` and mail whose original was already moved to the trash return None, and the subject on that task list row stays plain text. The school's Exchange mailbox gets a search instead, described two paragraphs below.

The string starting with `FMfcg` in Gmail's address bar is another encoding of the same thread id and also opens the message directly. How to convert it is written in the comment on `origin_links`. If both direct-open formats stop working, `#search/rfc822msgid:<Message-ID>` is still left to fall back on. It needs no login to the original mailbox, so it works for a mailbox without credentials too, at the cost of only listing that one message and needing another click to open it. The same comment describes all three forms in full. The task list page saves the links it gets to `Daily_Task/Email_Check/task_list/mail-links.json`, so after changing the format, increase `LINKS_VERSION` in `task_list_gui.py` by one, or the saved links keep the old format.

The school's Exchange mailbox has no usable URL that opens a message. Outlook on the web does not read a search condition from the URL, and Graph needs administrator approval. Both were tested on 2026-10-02. So for such a message it returns `OUTLOOK_SEARCH` followed by an AQS search condition made of the subject, the sender and the date `scout` received the message. The task list page copies the search condition to the clipboard and opens Outlook, and the user pastes it into the search box. Only a message that carries the `X-MS-Exchange-ForwardingLoop` stamp, with the stamped mailbox being the original mailbox, takes this route.

The tests are in `test_origin_links.py`. They use a fake IMAP and need no network and no credentials.

## One-time setup

### Once per account

1. At <https://myaccount.google.com/security>, confirm that **2-Step Verification is on**. Without it, the page in the next step does not exist.
2. In that account's Gmail settings, on the **Forwarding and POP/IMAP** tab, confirm that IMAP is enabled. Newer Gmail may have removed this switch and left IMAP always on, so not finding it is normal.
3. At <https://myaccount.google.com/apppasswords>, generate an app password. Names such as `gmail-mcp-main` and `gmail-mcp-scout` are suggested, so either one can be revoked on its own later.

When generating an app password, watch which account is signed in. With several accounts signed in at once, that page opens under the default account, so do the two accounts separately and use a private window for the second. A mistake here does not fail silently, because the IMAP login is rejected outright.

### Fill in `.env`

Create `.env` from `.env.example` with the four keys below.

```
GMAIL_MAIN_USER=<main mailbox address>
GMAIL_MAIN_APP_PASSWORD=<app password of the main mailbox>
GMAIL_SCOUT_USER=<forwarding hub address>
GMAIL_SCOUT_APP_PASSWORD=<app password of the forwarding hub>
```

Google shows an app password as four groups of four characters. The spaces can stay when you paste it, because the program strips them. The password is **shown only once**, so if you did not copy it, delete that one and generate a new one.

### Verify

```bash
conda run -n ML python gmail_mcp/server.py --check
```

Setup succeeded when both lines say `OK`. This mode prints only the status and the message counts, never a password.

### Done

The MCP server is registered with `claude mcp add` (user scope). After changing `server.py`, **start a new Claude Code session**, because a running MCP process still holds the old tool schema.

## Pitfalls already hit

| symptom | cause |
|---|---|
| Hard-coding `[Gmail]/All Mail` finds no folder | The name is localized, and a zh-TW account reports `[Gmail]/&UWiQ6JD1TvY-`. Find the folder by its RFC 6154 `\All` attribute, never by matching its name |
| A search misses some messages | Gmail search skips spam and trash by default. Forwarding breaks SPF, so a message can be misjudged as spam, and the query has to include `in:anywhere` |
| The snippet is a run of MIME boundary garbage | `BODY[TEXT]` returns the raw MIME body, and a multipart message starts with a boundary and part headers. The program puts the message's `Content-Type` back in front so the truncated fragment can be parsed, and this is already fixed |
| `conda run` crashes while printing message content | `conda run` relays the child process's stdout to the cp950 console, which raises `UnicodeEncodeError` on any character outside cp950. Have the script write a UTF-8 file itself instead of relying on `conda run` to relay the output |

## Troubleshooting

- **`--check` shows FAIL and says the login was rejected.** The app password was pasted wrong, or a different account was signed in when it was generated. Generate a new one at <https://myaccount.google.com/apppasswords>.
- **`--check` says the keys are not set in `.env`.** One of the four keys has no value, or `.env` is not next to `server.py`.
- **`label:` finds nothing.** That label does not exist in that account. Each account has its own labels, and `career_event` exists only in `scout`.
- **To revoke access**, delete the matching app password at <https://myaccount.google.com/apppasswords> and clear its values in `.env`. Revoking one does not affect the other.

## Security

An app password differs from the old `gmail.readonly` scope in one substantive way. It is **not a read-only credential**. It grants full IMAP access, and with SMTP it can even send mail. This program issues only read commands, but that is a choice the code makes, and nothing on Google's side blocks other uses.

So treat `.env` as a password file. It is already in `.gitignore`, and that entry must stay.

Also note that neither the Gmail API nor IMAP has a permission level such as "read only one label". The code narrows its searches with `label:career_event`, which **reduces the actual exposure** but is not a boundary Google enforces. The credential itself can see the whole mailbox.
