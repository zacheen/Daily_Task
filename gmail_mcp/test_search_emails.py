"""A failed search or fetch must raise, not read as no mail.

Offline: _mailbox is swapped for a fake that answers SEARCH and FETCH as told.

Run with
    conda run -n ML --no-capture-output python gmail_mcp/test_search_emails.py
"""

import asyncio
import imaplib
import importlib.util
import os
import sys
from contextlib import contextmanager

import server as sv

# Sampled before the build_server call below imports the MCP SDK.
SDK_AT_IMPORT = "mcp" in sys.modules
# Kept before install() swaps it, for the SELECT check that needs the real one.
REAL_MAILBOX = sv._mailbox

fails = []


def check(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


class FakeImap:
    def __init__(self, answer):
        self.answer = answer
        self.literal = None

    def uid(self, command, *args):
        assert command == "SEARCH" and args[-1] == "X-GM-RAW", (command, args)
        self.literal = None
        return self.answer


def install(answer):
    @contextmanager
    def fake_mailbox(account):
        yield FakeImap(answer), "hub@gmail.example"

    sv._mailbox = fake_mailbox


def raises(error, call):
    try:
        call()
    except error:
        return True
    return False


QUERY = "after:1791234382 before:1791238855"

install(("OK", [b""]))
got = sv.search_emails(QUERY, 40)
check("a search with no hits returns an empty list", got == [], got)

install(("NO", [b"temporary failure"]))
check("a search answering NO raises rather than returning no hits",
      raises(imaplib.IMAP4.error, lambda: sv.search_emails(QUERY, 40)))
check("get_latest_email answering NO raises rather than reporting an empty INBOX",
      raises(imaplib.IMAP4.error, lambda: sv.get_latest_email()))

class FetchImap:
    """SEARCH finds `hits`, UID 7 by default. FETCH answers whatever `fetch` holds."""

    def __init__(self, fetch, hits=b"7"):
        self.fetch = fetch
        self.hits = hits
        self.literal = None
        self.searched = []

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searched.append(args)
            self.literal = None
            return "OK", [self.hits]
        assert command == "FETCH", (command, args)
        return self.fetch


def install_fetch(fetch, hits=b"7"):
    box = FetchImap(fetch, hits)

    @contextmanager
    def fake_mailbox(account):
        yield box, "hub@gmail.example"

    sv._mailbox = fake_mailbox
    return box


HEADERS = b"Subject: Interview\r\nFrom: hr@example.com\r\n\r\n"
STAMP = b'INTERNALDATE "14-Sep-2026 12:00:00 -0700"'

install_fetch(("OK", [(b"1 (X-GM-MSGID 42 " + STAMP + b" BODY[HEADER.FIELDS (...)] {1}", HEADERS),
                      (b" BODY[TEXT]<0> {5}", b"hello"), b")"]))
got = sv.search_emails(QUERY, 40)
check("a normal FETCH gives a row with its id, received stamp and snippet",
      len(got) == 1 and got[0]["id"] == "42" and got[0]["received"].startswith("2026-09-")
      and got[0]["subject"] == "Interview" and got[0]["snippet"] == "hello", got)

install_fetch(("OK", [(b"1 (BODY[HEADER.FIELDS (...)] {1}", HEADERS),
                      (b" BODY[TEXT]<0> {5}", b"hello"),
                      b" X-GM-MSGID 42 " + STAMP + b")"]))
got = sv.search_emails(QUERY, 40)
check("X-GM-MSGID and INTERNALDATE after the last literal are still read",
      len(got) == 1 and got[0]["id"] == "42" and got[0]["received"].startswith("2026-09-"), got)

install_fetch(("NO", [b"temporary failure"]))
check("a FETCH answering NO raises rather than returning an empty row",
      raises(imaplib.IMAP4.error, lambda: sv.search_emails(QUERY, 40)))

install_fetch(("OK", [None]))
check("a FETCH of a UID that vanished raises rather than returning a row with no id",
      raises(imaplib.IMAP4.error, lambda: sv.search_emails(QUERY, 40)))

install_fetch(("OK", [(b"1 (BODY[HEADER.FIELDS (...)] {1}", HEADERS), b")"]))
check("a FETCH carrying no X-GM-MSGID raises rather than returning a row with no id",
      raises(imaplib.IMAP4.error, lambda: sv.search_emails(QUERY, 40)))

install_fetch(("OK", [(b"1 (BODY[] {5}", b"Content-Type: text/plain\r\n\r\nbody text"), b")"]))
check("get_email_body returns the text part", sv.get_email_body("42") == "body text")

install_fetch(("NO", [b"temporary failure"]))
check("get_email_body raises when the FETCH fails",
      raises(imaplib.IMAP4.error, lambda: sv.get_email_body("42")))

box = install_fetch(("OK", [None]))
check("get_email_body refuses an id that would run as search syntax",
      raises(ValueError, lambda: sv.get_email_body("1 OR ALL")) and not box.searched)

install_fetch(("OK", [None]), hits=b"")
check("get_email_body raises when no message has the id",
      raises(LookupError, lambda: sv.get_email_body("42")))

check("get_email_body refuses digits outside ASCII, which imaplib cannot encode",
      raises(ValueError, lambda: sv.get_email_body("\u0663")))

install_fetch(("OK", [None]))
check("get_email_body raises on a UID that vanished, not a bare StopIteration",
      raises(imaplib.IMAP4.error, lambda: sv.get_email_body("42")))
check("origin_links raises on a hub UID that vanished, so the page retries",
      raises(imaplib.IMAP4.error, lambda: sv.origin_links(["42"])))
check("a thread lookup raises on a UID that vanished rather than reading no copy",
      raises(imaplib.IMAP4.error,
             lambda: sv._thread_ids(FetchImap(("OK", [None])), "m@x.example")))

install_fetch(("OK", [(b"1 (X-GM-MSGID 42 " + STAMP + b" BODY[HEADER.FIELDS (...)] {1}",
                       HEADERS), b")"]),
              hits=b" ".join(str(n).encode() for n in range(1, sv.MAX_SEARCH_RESULTS + 11)))
got = sv.search_emails(QUERY, 999)
check("max_results above the clamp returns MAX_SEARCH_RESULTS rows",
      len(got) == sv.MAX_SEARCH_RESULTS, len(got))


class RefusingSelect:
    """An IMAP4_SSL whose login works and whose SELECT answers NO."""
    logged_out = False

    def __init__(self, *args, **kwargs):
        pass

    def login(self, user, password):
        return "OK", [b"logged in"]

    def list(self):
        return "OK", [rb'(\HasNoChildren \All) "/" "[Gmail]/All Mail"']

    def select(self, mailbox, readonly=False):
        return "NO", [b"mailbox unavailable"]

    def close(self):
        raise imaplib.IMAP4.error("CLOSE illegal in state AUTH")

    def logout(self):
        RefusingSelect.logged_out = True


real_ssl, real_credentials = imaplib.IMAP4_SSL, sv._credentials
imaplib.IMAP4_SSL, sv._credentials = RefusingSelect, lambda account: ("u", "p")


def open_refused():
    with REAL_MAILBOX("scout"):
        pass


check("a refused SELECT raises at once rather than at the next command",
      raises(imaplib.IMAP4.error, open_refused) and RefusingSelect.logged_out)
imaplib.IMAP4_SSL, sv._credentials = real_ssl, real_credentials

spec = importlib.util.spec_from_file_location(
    "statemachine", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "Email_Check", "statemachine.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)
check("the round's page size fits under the server's clamp, or a full page reads as short",
      sm.MAX_RESULTS <= sv.MAX_SEARCH_RESULTS, (sm.MAX_RESULTS, sv.MAX_SEARCH_RESULTS))

check("importing server does not load the MCP SDK", not SDK_AT_IMPORT)
names = sorted(t.name for t in asyncio.run(sv.build_server().list_tools()))
check("build_server registers the three tools",
      names == ["get_email_body", "get_latest_email", "search_emails"], names)

print()
print("ALL PASS" if not fails else f"SOME FAIL  {fails}")
sys.exit(1 if fails else 0)
