"""Read-only Gmail MCP server for Claude Code, over IMAP.

Two mailboxes are exposed through one server; every tool takes an `account`
("scout" or "main"). Credentials are Google app passwords in .env, not OAuth:
an OAuth app stuck in "Testing" publishing status gets refresh tokens that
expire after 7 days, and publishing to production requires a verified domain
this project does not have. App passwords never expire.

The tradeoff is that an app password is not scope-limited the way
gmail.readonly was — it grants full IMAP access. Every command issued here is
read-only (SELECT is readonly, fetches use BODY.PEEK so they don't set the
\\Seen flag), but that is a property of this code, not a restriction Google
enforces. Revoke a single password at
https://myaccount.google.com/apppasswords without touching the other.
"""

import email
import email.utils
import imaplib
import re
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from email.header import decode_header, make_header
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values
from mcp.server import MCPServer

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# alias -> (user env key, app password env key)
ACCOUNTS = {
    "scout": ("GMAIL_SCOUT_USER", "GMAIL_SCOUT_APP_PASSWORD"),
    "main": ("GMAIL_MAIN_USER", "GMAIL_MAIN_APP_PASSWORD"),
}
DEFAULT_ACCOUNT = "scout"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
# Seconds any one socket read may block. Without it a silent connection hangs
# forever, wedging the todo page's link lookup for the life of its process.
IMAP_TIMEOUT = 30
# Outlook on the web reads no search from its URL (six forms tried 2026-10-02),
# so the query rides in the fragment and the todo page copies it to the
# clipboard. The todo page mirrors this prefix.
OUTLOOK_SEARCH = "https://outlook.office.com/mail/#q="
# Enough to build a preview line without pulling whole messages during a search.
SNIPPET_FETCH_BYTES = 2048
SNIPPET_CHARS = 200
# CONTENT-TYPE is fetched only to make the truncated body chunk parseable as MIME.
# TO, DELIVERED-TO, X-FORWARDED-FOR and RETURN-PATH feed _origin_mailbox and
# are never reported raw.
HEADER_FIELDS = ("SUBJECT FROM DATE LIST-UNSUBSCRIBE CONTENT-TYPE"
                 " TO DELIVERED-TO X-FORWARDED-FOR RETURN-PATH"
                 " X-MS-EXCHANGE-FORWARDINGLOOP")

mcp = MCPServer("gmail")


def _credentials(account: str) -> tuple[str, str]:
    if account not in ACCOUNTS:
        raise RuntimeError(f"Unknown account {account!r}; use one of {sorted(ACCOUNTS)}.")
    cfg = dotenv_values(ENV_FILE)
    user_key, pw_key = ACCOUNTS[account]
    user = (cfg.get(user_key) or "").strip()
    # Google displays app passwords in four groups of four; the spaces are cosmetic.
    password = (cfg.get(pw_key) or "").replace(" ", "")
    if not user or not password:
        raise RuntimeError(f"{user_key} / {pw_key} not set in {ENV_FILE}.")
    return user, password


def _all_mail_folder(imap: imaplib.IMAP4_SSL) -> str:
    """Locate All Mail by its RFC 6154 \\All attribute.

    The display name is localized — a zh-TW account reports
    "[Gmail]/&UWiQ6JD1TvY-" — so matching on "[Gmail]/All Mail" fails outright
    on both of this project's mailboxes.
    """
    typ, lines = imap.list()
    if typ == "OK":
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            if r"\All" in line:
                match = re.search(r'"([^"]+)"\s*$', line)
                if match:
                    return f'"{match.group(1)}"'
    return '"[Gmail]/All Mail"'


@contextmanager
def _mailbox(account: str):
    """Yield (IMAP connection with All Mail selected read-only, own address).

    The address comes back out because _origin_mailbox has to recognise this
    mailbox inside a forwarding chain, and nothing downstream knows it.
    """
    user, password = _credentials(account)
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
    try:
        imap.login(user, password)
        imap.select(_all_mail_folder(imap), readonly=True)
        yield imap, user
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass


def _search(imap: imaplib.IMAP4_SSL, query: str, strict: bool = False) -> list[bytes]:
    """Run a Gmail search string via X-GM-RAW, newest first.

    strict raises on a failed search instead of reading it as no hits, for a
    caller whose "no hits" gets remembered.

    X-GM-RAW takes the same syntax as the Gmail search box, so `label:`,
    `newer_than:` and `in:anywhere` all work. `in:anywhere` matters because a
    plain search skips Spam and Trash, and forwarded mail fails SPF often
    enough to land there.

    Sent as a UTF-8 literal rather than interpolated into a quoted string,
    since a quoted string can't hold an unescaped double quote (needed for
    Gmail's phrase syntax, e.g. subject:"info session") or non-ASCII
    (needed for this account's Chinese label names). Both used to fail
    with `BAD Could not parse command` rather than returning no hits.
    """
    # imaplib appends the {n} literal marker after the final arg and sends the
    # bytes on the continuation, so CHARSET UTF-8 has to precede X-GM-RAW.
    imap.literal = query.encode("utf-8")
    typ, res = imap.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
    if strict:
        _require_ok(typ, res)
    if typ != "OK" or not res or not res[0]:
        return []
    # SEARCH returns ascending UIDs; callers want the newest messages first.
    return res[0].split()[::-1]


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _part_text(part) -> str:
    """Decode one MIME part, tolerating a payload cut off mid-transfer-encoding."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if not payload:
        return ""
    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def _snippet(content_type: str, text_blob: bytes) -> str:
    """Build a preview from a partial BODY[TEXT] fetch.

    BODY[TEXT] is the raw MIME body, so on multipart mail the first bytes are a
    boundary and part headers, not readable content. Pasting the message's own
    Content-Type in front makes the truncated chunk parseable, which is the only
    way to reach the text part without a second BODYSTRUCTURE round trip.
    """
    raw = (f"Content-Type: {content_type}\r\n\r\n".encode() if content_type else b"") + text_blob
    message = email.message_from_bytes(raw)
    plain, html_text = "", ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() == "text/plain" and not plain:
            plain = _part_text(part)
        elif part.get_content_type() == "text/html" and not html_text:
            html_text = _strip_html(_part_text(part))
    text = plain or html_text or text_blob.decode("utf-8", errors="replace")
    # A 2 KB cut can land mid-character; drop the replacement chars it leaves behind.
    return re.sub(r"\s+", " ", text.replace("�", "")).strip()[:SNIPPET_CHARS]


def _address(value: str) -> str:
    """The bare address out of one header value, lowercased for comparison."""
    return email.utils.parseaddr(value)[1].strip().lower()


def _origin_mailbox(headers, hub: str, account: str) -> str:
    """Which of the user's own mailboxes took delivery before any forwarding.

    scout is a forwarding hub, so From and Subject say nothing about where the
    original copy sits. The answer is an address where a header names the
    mailbox outright and a bare domain where the headers only narrow it down,
    because pointing at the wrong account is worse than pointing at the right
    organisation.

    Exchange stamps `X-MS-Exchange-ForwardingLoop: <mailbox>;<tenant>` on
    anything a mailbox forwards, to catch loops, and that names the address
    outright. Gmail to Gmail instead stamps its own Delivered-To under the
    hub's and adds `X-Forwarded-For: <origin> <hub>`.

    Only when neither stamp is there does this fall back to the To header,
    which is the user's own address on ordinary mail but the sender's list on
    bulk mail and a colleague on a Cc, so it is trusted no further than its
    domain. Bulk mail sent via Bcc carries no To header at all, so the stamps
    above are the only way to tell which mailbox took delivery. Exchange also
    rewrites the envelope sender of anything it forwards out to
    <tenant>.onmicrosoft.com, and that tenant overrules a To whose domain
    disagrees with it, which keeps a newsletter addressed to its own list
    from being reported as the user's mailbox.

    Mail that came straight in reports the alias, never the hub's address:
    callers display and log this field, and that address stays out of it.
    """
    hub = hub.strip().lower()
    # Before Delivered-To, because a mail that Exchange forwarded on to another
    # forwarding account would otherwise report the middle hop as the origin.
    # Headers prepend, so the bottom one is the earliest, same as below.
    for value in reversed(headers.get_all("X-MS-Exchange-ForwardingLoop") or []):
        addr = value.split(";")[0].strip().lower()
        if "@" in addr and addr != hub:
            return addr
    # Each hop prepends its own, so the oldest Delivered-To is the first
    # mailbox that took delivery -- read the list from the bottom up.
    for value in reversed(headers.get_all("Delivered-To") or []):
        addr = _address(value)
        if addr and addr != hub:
            return addr
    for token in headers.get("X-Forwarded-For", "").split():
        addr = _address(token)
        if addr and addr != hub:
            return addr
    # Only the first Return-Path is the forwarding hop's; the ones under it
    # belong to the original sender.
    tenant = re.search(r"@([^@>\s]+)\.onmicrosoft\.com>?\s*$",
                       headers.get("Return-Path", "") or "")
    tenant = tenant.group(1).lower() if tenant else ""
    for _name, addr in email.utils.getaddresses(headers.get_all("To") or []):
        addr = addr.strip().lower()
        if not addr or addr == hub or "@" not in addr:
            continue
        domain = addr.rsplit("@", 1)[1]
        # Not a break on mismatch: a bulk To can be followed by the real one.
        if not tenant or domain.split(".")[0] == tenant:
            return domain
    return tenant or account


def _received(prefix: bytes) -> str:
    """INTERNALDATE off a FETCH response prefix, as local `YYYY-MM-DD HH:MM`.

    Gmail sorts and displays by internal date, and the Date header can lag it
    without bound, so this is the stamp that finds a message again in a client
    while the header's own is the one that does not.

    Internaldate2tuple already converts to local time and returns None rather
    than raising when the prefix holds no INTERNALDATE, which strftime then
    rejects.
    """
    try:
        return time.strftime("%Y-%m-%d %H:%M", imaplib.Internaldate2tuple(prefix))
    except Exception:
        return ""


def _metadata(imap: imaplib.IMAP4_SSL, uid: bytes, hub: str, account: str) -> dict:
    typ, data = imap.uid(
        "FETCH",
        uid,
        f"(X-GM-MSGID INTERNALDATE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})]"
        f" BODY.PEEK[TEXT]<0.{SNIPPET_FETCH_BYTES}>)",
    )
    if typ != "OK" or not data:
        return {}
    # imaplib returns one (prefix, literal) tuple per requested item. The prefix
    # names the item, which is the only way to tell the header block from the
    # body chunk once both are in the same response.
    msgid, received, header_blob, text_blob = "", "", b"", b""
    for part in data:
        if not isinstance(part, tuple) or len(part) < 2:
            continue
        prefix = part[0].decode("utf-8", "replace")
        found = re.search(r"X-GM-MSGID (\d+)", prefix)
        if found:
            msgid = found.group(1)
        received = received or _received(part[0])
        if "HEADER" in prefix:
            header_blob = part[1]
        elif "TEXT" in prefix:
            text_blob = part[1]
    headers = email.message_from_bytes(header_blob)
    return {
        "id": msgid,
        "subject": _decode(headers.get("Subject", "(no subject)")),
        "from": _decode(headers.get("From", "")),
        "mailbox": _origin_mailbox(headers, hub, account),
        # When this mailbox took delivery, which is what the mail client
        # shows. "date" below is the sender's own claim and can be older.
        "received": received,
        "date": headers.get("Date", ""),
        # Present on bulk mail (newsletters, notifications) and absent on personal
        # mail, which makes it the cleanest signal for filtering announcements.
        "list_unsubscribe": headers.get("List-Unsubscribe", ""),
        # Best effort: the first 2 KB of an HTML mail is often just <head> CSS,
        # so this can come back empty even for a message with plenty of text.
        "snippet": _snippet(headers.get("Content-Type", ""), text_blob),
    }


@mcp.tool()
def get_latest_email(account: str = DEFAULT_ACCOUNT) -> dict:
    """Get subject, sender, date and snippet of the newest email in the INBOX.

    account: "scout" (default, the forwarding hub) or "main".
    `mailbox` is where it was delivered before being forwarded on, as an
    address or just a domain, or the account alias when it came straight in.
    `received` is when this mailbox took delivery, in local time.
    """
    with _mailbox(account) as (imap, hub):
        uids = _search(imap, "in:inbox", strict=True)
        if not uids:
            return {"error": f"{account} INBOX is empty"}
        return _metadata(imap, uids[0], hub, account)


@mcp.tool()
def search_emails(query: str = "", max_results: int = 10, account: str = DEFAULT_ACCOUNT) -> list:
    """Search mail with Gmail query syntax, newest first.

    Examples: 'label:career_event in:anywhere', 'from:github.com',
    'subject:interview newer_than:7d'. Empty query returns recent INBOX mail.
    account: "scout" (default) or "main".
    Each hit carries `mailbox`, where it was delivered before being forwarded
    on, as an address or just a domain, or the account alias for mail that
    came straight in. It also carries `received`, when this mailbox took
    delivery, in local time.
    No hits returns an empty list (may display as no output); a failed search
    raises an error instead.
    """
    with _mailbox(account) as (imap, hub):
        # strict: the scheduled check retires a time range on zero hits, so a
        # failed SEARCH read as no mail would skip that range for good.
        uids = _search(imap, query or "in:inbox", strict=True)
        return [_metadata(imap, uid, hub, account)
                for uid in uids[: max(1, min(max_results, 50))]]


@mcp.tool()
def get_email_body(message_id: str, account: str = DEFAULT_ACCOUNT) -> str:
    """Get the text body of one email by id (ids come from the other two tools).

    account must match the one the id came from — ids are per-mailbox.
    """
    with _mailbox(account) as (imap, _hub):
        typ, res = imap.uid("SEARCH", "X-GM-MSGID", message_id)
        if typ != "OK" or not res or not res[0]:
            return f"(no message with id {message_id} in {account})"
        typ, data = imap.uid("FETCH", res[0].split()[0], "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return "(could not fetch message)"
        msg = email.message_from_bytes(data[0][1])
        plain, html_parts = [], []
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            text = _part_text(part)
            if not text:
                continue
            if part.get_content_type() == "text/plain":
                plain.append(text)
            elif part.get_content_type() == "text/html":
                html_parts.append(text)
        if plain:
            return "\n".join(plain)
        if html_parts:
            return _strip_html("\n".join(html_parts))
        return "(no readable text body)"


def _account_for(address: str) -> str | None:
    """The alias that logs in as this address. Never the hub, because a link
    into the forwarding hub is exactly what origin_links exists to avoid."""
    for account in ACCOUNTS:
        if account == DEFAULT_ACCOUNT:
            continue
        try:
            user, _password = _credentials(account)
        except RuntimeError:
            continue
        if user.lower() == address:
            return account
    return None


def _require_ok(typ: str, data) -> None:
    """Raise on a failed command. Read as "no such message", it would become a
    None the caller keeps for good instead of retrying."""
    if typ != "OK":
        raise imaplib.IMAP4.error(f"IMAP command failed: {typ} {data!r}")


def _thread_ids(imap: imaplib.IMAP4_SSL, message_id: str) -> set[int]:
    """X-GM-THRID of every copy of this Message-ID in the selected mailbox."""
    found = set()
    for uid in _search(imap, f"rfc822msgid:{message_id}", strict=True):
        typ, data = imap.uid("FETCH", uid, "(X-GM-THRID)")
        _require_ok(typ, data)
        for part in data or []:
            raw = part[0] if isinstance(part, tuple) else part
            hit = re.search(rb"X-GM-THRID (\d+)", raw) if isinstance(raw, bytes) else None
            if hit:
                found.add(int(hit.group(1)))
    return found


def _exchange_forwarded(headers, origin: str) -> bool:
    """Whether the origin is the Exchange mailbox whose forwarding stamp names it."""
    return any(value.split(";")[0].strip().lower() == origin
               for value in headers.get_all("X-MS-Exchange-ForwardingLoop") or [])


def _outlook_search(headers, received: str) -> str | None:
    """OUTLOOK_SEARCH plus an AQS query for this one mail, or None when the
    headers lack a part of it.

    Subject and From alone matched three mails of a sender reusing its
    subject, received: narrowed it to one. The day is scout's INTERNALDATE in
    local time, valid only if Outlook runs in the same time zone and the
    forward took less than the rest of the day.
    """
    # A quote would end the phrase early, so it becomes a space.
    subject = " ".join(_decode(headers.get("Subject", "")).replace('"', " ").split())
    sender = _address(headers.get("From", ""))
    try:
        day = time.strptime(received[:10], "%Y-%m-%d")
    except ValueError:
        return None
    if not subject or not sender:
        return None
    query = (f'Subject:"{subject}" AND From:{sender} '
             f"AND received:{day.tm_mon}/{day.tm_mday}/{day.tm_year}")
    return OUTLOOK_SEARCH + quote(query, safe="")


def origin_links(hub_ids: list[str],
                 warn: Callable[[str], None] | None = None) -> dict[str, str | None]:
    """A web link per scout message id into the mailbox that first received
    the mail, so a reply goes out from that address.

    Not an MCP tool. The todo list imports it, which keeps the link out of the
    scheduled LLM's output and lets todos written before this existed get one.

    A Gmail origin gets a link that opens its own copy. Gmail ids are per
    mailbox, so scout's own id opens nothing there, and the Message-ID header,
    which survives forwarding, joins the two copies. The school's Exchange
    mailbox gets an OUTLOOK_SEARCH link instead, since opening a message there
    needs an id only Graph supplies, and Graph needs admin approval at the
    school tenant.

    None where no link can be built: mail that came straight in, a Gmail
    origin without credentials in .env, no usable Message-ID, an origin
    holding no copy, or an Exchange mail missing its subject, sender or date.
    Copies in more than one thread link to the newest, since the user would
    rather open the wrong copy than get no link. Raises when a mailbox cannot
    be reached or a command fails, so the caller can retry rather than
    remember a None.

    warn, when given, hears about a Gmail origin left without a link for lack
    of credentials, which otherwise looks the same as any other None.
    """
    links: dict[str, str | None] = {hid: None for hid in hub_ids}
    # ASCII digits only, because the id is spliced into an IMAP command, and
    # isdigit alone passes digits such as U+0663 that imaplib cannot encode.
    wanted = [hid for hid in hub_ids if hid.isascii() and hid.isdigit()]
    if not wanted:
        return links
    pending: dict[str, dict[str, str]] = {}
    with _mailbox(DEFAULT_ACCOUNT) as (imap, hub):
        for hid in wanted:
            typ, res = imap.uid("SEARCH", "X-GM-MSGID", hid)
            _require_ok(typ, res)
            if not res or not res[0]:
                continue
            typ, data = imap.uid("FETCH", res[0].split()[0],
                                 f"(INTERNALDATE BODY.PEEK[HEADER.FIELDS "
                                 f"({HEADER_FIELDS} MESSAGE-ID)])")
            _require_ok(typ, data)
            prefix, blob = next((p for p in data or [] if isinstance(p, tuple)), (b"", b""))
            headers = email.message_from_bytes(blob)
            origin = _origin_mailbox(headers, hub, DEFAULT_ACCOUNT)
            if _exchange_forwarded(headers, origin):
                links[hid] = _outlook_search(headers, _received(prefix))
                continue
            message_id = str(headers.get("Message-ID") or "").strip().strip("<>")
            # Whitespace or a quote would split the rfc822msgid: search term.
            if message_id and not re.search(r'[\s"]', message_id):
                pending.setdefault(origin, {})[hid] = message_id
    for origin, by_hub in pending.items():
        account = _account_for(origin)
        if account is None:
            # An alias or bare domain means no mailbox was named, so no
            # credential could fix it.
            if warn and "@" in origin:
                warn(f"no credentials in .env for {origin}, "
                     f"{len(by_hub)} todo(s) left without a link: {', '.join(by_hub)}")
            continue
        with _mailbox(account) as (imap, _own):
            for hid, message_id in by_hub.items():
                threads = _thread_ids(imap, message_id)
                if not threads:
                    continue
                # A Gmail id is its creation time in ms shifted left 20 bits, and
                # a thread id is its first message's id, so max() picks the
                # thread started last. Checked on 2026-10-03 against 4000
                # messages, where id >> 20 matched INTERNALDATE within 2 s.
                # Hex X-GM-THRID is Gmail's older URL form, still redirected to
                # the current one, the FMfcg... token in the address bar. That
                # token is the first fallback if the redirect is retired. Build
                # it by base64-encoding "f:" + the decimal X-GM-THRID (standard
                # alphabet, no padding), reading those characters as one base-64
                # number, most significant digit first, and writing it in
                # base 40 over BCDFGHJKLMNPQRSTVWXZbcdfghjklmnpqrstvwxz.
                # Decoding a token copied from the address bar the same way
                # gives back "f:<thread id>", which re-checks the recipe.
                # The second fallback is #search/rfc822msgid: plus message_id
                # under the same authuser. It needs no origin login, so it also
                # covers an origin without credentials, but lists the one hit
                # and costs a click.
                # All three forms worked in the origin account on 2026-10-02.
                # After a new form or new None rules for an id already looked
                # up, bump LINKS_VERSION in Daily_Task's
                # Email_Check/task_list/task_list_gui.py, or the todo page keeps
                # serving its cached answers.
                links[hid] = (f"https://mail.google.com/mail/"
                              f"?authuser={quote(origin, safe='@')}#all/{max(threads):x}")
    return links


def _check() -> None:
    """Verify both mailboxes are reachable. Prints status only, never the password."""
    for account in ACCOUNTS:
        try:
            with _mailbox(account) as (imap, _hub):
                total = len(_search(imap, "in:anywhere"))
                labelled = len(_search(imap, "label:career_event in:anywhere"))
            print(f"{account:6} OK  {total} msgs, {labelled} labelled career_event")
        except Exception as exc:
            print(f"{account:6} FAIL  {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
    else:
        mcp.run()  # stdio transport
