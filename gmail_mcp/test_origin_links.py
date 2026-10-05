"""Tests for origin_links, the todo list's link into the original mailbox.

No network and no credentials. _mailbox and _credentials are swapped for
fakes that answer the handful of IMAP commands origin_links issues.

Run with
    conda run -n ML --no-capture-output python gmail_mcp/test_origin_links.py
"""

import imaplib
import sys
from contextlib import contextmanager
from urllib.parse import unquote

import server as sv

# Kept before install() swaps it, for the one check that needs the real one.
REAL_MAILBOX = sv._mailbox
HUB = "hub@gmail.example"
ORIGIN = "me@gmail.example"
SCHOOL = "me@school.example"

fails = []


def check(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


class FakeImap:
    """One mailbox. Each message is a dict with uid, and optionally gm_msgid,
    thrid, message_id and the raw header block the hub would return."""

    def __init__(self, messages, fail=None):
        self.messages = messages
        self.fail = fail
        self.literal = None

    def _hits(self, key, value):
        return "OK", [b" ".join(m["uid"] for m in self.messages if m.get(key) == value)]

    def uid(self, command, *args):
        if command == self.fail:
            return "NO", [b"temporary failure"]
        if command == "SEARCH" and args[0] == "X-GM-MSGID":
            # Real IMAP would take anything else as more search syntax.
            assert args[1].isascii() and args[1].isdigit(), f"unsafe id {args[1]!r}"
            return self._hits("gm_msgid", args[1])
        if command == "SEARCH" and args[-1] == "X-GM-RAW":
            query = self.literal.decode()
            self.literal = None
            return self._hits("message_id", query.split("rfc822msgid:", 1)[1])
        if command == "FETCH":
            m = next(m for m in self.messages if m["uid"] == args[0])
            if "X-GM-THRID" in args[1]:
                return "OK", [b"1 (X-GM-THRID %d UID %s)" % (m["thrid"], m["uid"])]
            # Noon, so the local day is the same in any US time zone.
            return "OK", [(b'1 (UID %s INTERNALDATE "14-Sep-2026 12:00:00 -0700" '
                           b"BODY[HEADER.FIELDS (...)] {1}" % m["uid"],
                           m["headers"].encode()), b")"]
        raise AssertionError(f"unexpected IMAP command {command} {args}")


opened = []


def install(hub_messages, origin_messages, origin_login=ORIGIN, fail=(None, None)):
    """Point origin_links at two fake mailboxes. scout is the hub and main logs
    in as origin_login. The school account has no credentials, as in .env.
    fail is (account, command) for the one command that answers NO."""
    box, command = fail
    boxes = {"scout": (FakeImap(hub_messages, command if box == "scout" else None), HUB),
             "main": (FakeImap(origin_messages, command if box == "main" else None),
                      origin_login)}

    @contextmanager
    def fake_mailbox(account):
        opened.append(account)
        yield boxes[account]

    def fake_credentials(account):
        return boxes[account][1], "app-password"

    opened.clear()
    sv._mailbox = fake_mailbox
    sv._credentials = fake_credentials


def gmail_forward(message_id, origin=ORIGIN):
    return (f"Delivered-To: {HUB}\nX-Forwarded-For: {origin} {HUB}\n"
            f"Delivered-To: {origin}\nMessage-ID: <{message_id}>\n")


def raises(error, call):
    try:
        call()
    except error:
        return True
    return False


# --- The case the link exists for ---------------------------------------------
install([{"uid": b"7", "gm_msgid": "111", "headers": gmail_forward("a@mta")}],
        [{"uid": b"3", "message_id": "a@mta", "thrid": 0x1a2b3c4d5e6f7081}])
got = sv.origin_links(["111"])
check("a Gmail forward links to the origin's own thread, in hex, under authuser",
      got == {"111": f"https://mail.google.com/mail/?authuser={ORIGIN}#all/1a2b3c4d5e6f7081"},
      got)
check("and the hub and the origin are each logged in to once", opened == ["scout", "main"],
      opened)

install([{"uid": b"7", "gm_msgid": "111",
          "headers": gmail_forward("a@mta", origin="me+jobs@gmail.example")}],
        [{"uid": b"3", "message_id": "a@mta", "thrid": 5}],
        origin_login="Me+Jobs@gmail.example")
got = sv.origin_links(["111"])
check("a login in mixed case still matches, and its plus is escaped in the query",
      got == {"111": "https://mail.google.com/mail/?authuser=me%2Bjobs@gmail.example#all/5"},
      got)

# --- The Exchange origin gets an Outlook search instead -----------------------
def exchange_forward(subject, sender="Some One <noreply+news@sender.example>"):
    return (f"Delivered-To: {HUB}\nX-MS-Exchange-ForwardingLoop: {SCHOOL};tenant\n"
            f"Subject: {subject}\nFrom: {sender}\nMessage-ID: <b@exchange>\n")


def query(link):
    return unquote(link[len(sv.OUTLOOK_SEARCH):]) if link else link


install([{"uid": b"7", "gm_msgid": "222", "headers": exchange_forward("Example: Notice Posted!")}],
        [])
got = sv.origin_links(["222"])
check("an Exchange origin gets subject, sender and day as one Outlook search, no second login",
      query(got["222"]) == 'Subject:"Example: Notice Posted!" AND '
                           "From:noreply+news@sender.example AND received:9/14/2026"
      and got["222"].startswith(sv.OUTLOOK_SEARCH) and opened == ["scout"], (got, opened))

install([{"uid": b"7", "gm_msgid": "222",
          "headers": exchange_forward("=?utf-8?b?6YCZ5pivIDMg5YmH5ris6Kmm?=")}], [])
got = query(sv.origin_links(["222"])["222"])
check("an encoded subject is decoded into the query", got.startswith('Subject:"這是 3 則測試"'),
      got)

install([{"uid": b"7", "gm_msgid": "222", "headers": exchange_forward('Say "hi" now')}], [])
got = query(sv.origin_links(["222"])["222"])
check("a quote in the subject cannot end the phrase early", got.startswith('Subject:"Say hi now"'),
      got)

install([{"uid": b"7", "gm_msgid": "222", "headers": exchange_forward("Hello", sender="")}], [])
check("an Exchange mail with no sender gets no search rather than a loose one",
      sv.origin_links(["222"]) == {"222": None})

# --- Every case that must give None rather than a wrong link ------------------

install([{"uid": b"7", "gm_msgid": "333",
          "headers": f"Delivered-To: {HUB}\nTo: {HUB}\nMessage-ID: <c@direct>\n"}], [])
got = sv.origin_links(["333"])
check("mail that came straight in to the hub never gets a link into the hub",
      got == {"333": None} and opened == ["scout"], (got, opened))

install([{"uid": b"7", "gm_msgid": "444",
          "headers": f"Delivered-To: {HUB}\nX-Forwarded-For: {ORIGIN} {HUB}\n"}], [])
check("a message with no Message-ID cannot be joined, so no link",
      sv.origin_links(["444"]) == {"444": None} and opened == ["scout"], opened)

install([{"uid": b"7", "gm_msgid": "555", "headers": gmail_forward("d@mta")}], [])
check("an origin holding no copy, such as one moved to Trash, gives no link",
      sv.origin_links(["555"]) == {"555": None})

install([{"uid": b"7", "gm_msgid": "666", "headers": gmail_forward("e@mta")}],
        [{"uid": b"3", "message_id": "e@mta", "thrid": 10},
         {"uid": b"4", "message_id": "e@mta", "thrid": 11}])
got = sv.origin_links(["666"])["666"]
check("copies in two different threads link to the newest rather than to nothing",
      got is not None and got.endswith("#all/b"), got)

install([{"uid": b"7", "gm_msgid": "777", "headers": gmail_forward("f@mta")}],
        [{"uid": b"3", "message_id": "f@mta", "thrid": 12},
         {"uid": b"4", "message_id": "f@mta", "thrid": 12}])
check("two copies in the same thread still link to that thread",
      sv.origin_links(["777"])["777"].endswith("#all/c"))

heard = []
install([{"uid": b"7", "gm_msgid": "901",
          "headers": gmail_forward("h@mta", origin="other@gmail.example")}], [])
got = sv.origin_links(["901"], warn=heard.append)
check("a Gmail origin without credentials gets no link, and warn names it and the todo",
      got == {"901": None} and len(heard) == 1 and "other@gmail.example" in heard[0]
      and "901" in heard[0], (got, heard))

heard.clear()
install([{"uid": b"7", "gm_msgid": "333",
          "headers": f"Delivered-To: {HUB}\nTo: {HUB}\nMessage-ID: <c@direct>\n"}], [])
sv.origin_links(["333"], warn=heard.append)
check("mail that came straight in is not warned about, since no credential would help",
      heard == [], heard)

install([{"uid": b"7", "gm_msgid": "888", "headers": gmail_forward('g "x"@mta')}], [])
check("a Message-ID that would split the search term is skipped",
      sv.origin_links(["888"]) == {"888": None} and opened == ["scout"], opened)

install([], [])
got = sv.origin_links(["999", "abc", "1 OR ALL", "1\u0663"])
check("an unknown id gives None, and one that is not ASCII digits never reaches IMAP",
      got == {"999": None, "abc": None, "1 OR ALL": None, "1\u0663": None}
      and opened == ["scout"], (got, opened))

install([], [])
check("ids that are all unusable never log in at all",
      sv.origin_links(["abc"]) == {"abc": None} and opened == [], opened)

# --- A failure raises instead of answering None -------------------------------
# A None is kept for good by the todo page, so only a real answer may be one.
for box in ("scout", "main"):
    for command in ("SEARCH", "FETCH"):
        install([{"uid": b"7", "gm_msgid": "111", "headers": gmail_forward("a@mta")}],
                [{"uid": b"3", "message_id": "a@mta", "thrid": 5}], fail=(box, command))
        check(f"a {command} answering NO in {box} raises",
              raises(imaplib.IMAP4.error, lambda: sv.origin_links(["111"])))


@contextmanager
def unreachable(account):
    raise OSError("imap down")
    yield  # never runs, but makes this a generator for @contextmanager


sv._mailbox = unreachable
check("an unreachable mailbox raises", raises(OSError, lambda: sv.origin_links(["111"])))

install([], [], origin_login=ORIGIN)
check("the hub alias is never offered as an origin account, even for its own address",
      sv._account_for(HUB) is None and sv._account_for(ORIGIN) == "main")


# --- Every connection is opened with a timeout --------------------------------
class Opened(Exception):
    pass


seen = {}


def fake_ssl(host, port, **kwargs):
    seen.update(kwargs)
    raise Opened


real_ssl = sv.imaplib.IMAP4_SSL
sv.imaplib.IMAP4_SSL = fake_ssl
sv._credentials = lambda account: ("x@gmail.example", "app-password")
opened_with_timeout = raises(Opened, lambda: REAL_MAILBOX("scout").__enter__())
sv.imaplib.IMAP4_SSL = real_ssl
check("a connection is opened with IMAP_TIMEOUT, so a silent server cannot hang a lookup",
      opened_with_timeout and seen.get("timeout") == sv.IMAP_TIMEOUT
      and sv.IMAP_TIMEOUT is not None, seen)

print()
print("ALL PASS" if not fails else f"SOME FAIL  {fails}")
sys.exit(1 if fails else 0)
