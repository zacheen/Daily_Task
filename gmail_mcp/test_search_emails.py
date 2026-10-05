"""A failed search must raise, not read as no mail.

Offline: _mailbox is swapped for a fake whose SEARCH answers OK (no hits) or NO.

Run with
    conda run -n ML --no-capture-output python gmail_mcp/test_search_emails.py
"""

import imaplib
import sys
from contextlib import contextmanager

import server as sv

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

print()
print("ALL PASS" if not fails else f"SOME FAIL  {fails}")
sys.exit(1 if fails else 0)
