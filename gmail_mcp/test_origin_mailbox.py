"""Header tests for the two fields the todo list uses to locate a message.

Both bugs this function has shipped were found by a person reading the todo
list and saying the answer looked wrong, because nothing here exercised it.
Every case below is a real header shape pulled off the live mailbox, so a
future simplification that breaks one breaks a mailbox that exists.

No network and no credentials: _origin_mailbox takes a parsed header block
and _received takes a FETCH response prefix, so both are pure.

Run with
    conda run -n ML --no-capture-output python gmail_mcp/test_origin_mailbox.py
"""

import email
import re
import sys

import server as sv

HUB = "hub@gmail.example"
ACCOUNT = "scout"

fails = []


def check(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


def origin(raw):
    """Run the resolver over a header block written the way a message carries it."""
    return sv._origin_mailbox(email.message_from_string(raw), HUB, ACCOUNT)


# --- Gmail to Gmail forwarding names the origin outright ---------------------
# The hub's own Delivered-To is prepended above the sending account's, so the
# answer is the bottom one, not the top.
got = origin(f"""Delivered-To: {HUB}
X-Forwarded-To: {HUB}
X-Forwarded-For: me@personal.example {HUB}
Delivered-To: me@personal.example
To: me@personal.example
Subject: Example digest
""")
check("a Gmail forward reports the account that took delivery, not the hub",
      got == "me@personal.example", got)

got = origin(f"""Delivered-To: {HUB}
X-Forwarded-For: me@personal.example {HUB}
Subject: no Delivered-To from the origin, only the forwarded-for pair
""")
check("X-Forwarded-For carries it when the origin left no Delivered-To",
      got == "me@personal.example", got)

# --- Exchange stamps the forwarding mailbox ----------------------------------
got = origin(f"""Delivered-To: {HUB}
Return-Path: <bounces+SRS=xxxxx=YYYYY@school.onmicrosoft.com>
To: Given Family <me@school.example>
X-MS-Exchange-ForwardingLoop: me@school.example;00000000-0000-0000-0000-000000000000
Subject: Example deadline approaching
""")
check("the Exchange forwarding stamp is taken as the answer",
      got == "me@school.example", got)

# The bug a user reported. Bulk senders put the whole recipient list in Bcc, so
# the message arrives with no To header at all and every To-based fallback has
# nothing to read. Before the stamp was consulted this returned the alias, which
# looked exactly like a lookup failure.
got = origin(f"""Delivered-To: {HUB}
Return-Path: <sender@school.example>
X-MS-Exchange-ForwardingLoop: me@school.example;00000000-0000-0000-0000-000000000000
X-MS-Exchange-CrossTenant-Id: 00000000-0000-0000-0000-000000000000
Subject: Example waiting list
""")
check("a Bcc blast with no To header still resolves, which is the reported bug",
      got == "me@school.example", got)

# Also reported: the To here is the sender's own list, so trusting it named an
# address that was never the user's.
got = origin(f"""Delivered-To: {HUB}
Return-Path: <bounces+SRS=xxxxx=YYYYY@school.onmicrosoft.com>
To: Students <students@codepath.example>
X-MS-Exchange-ForwardingLoop: me@school.example;00000000-0000-0000-0000-000000000000
Subject: Example session starts tomorrow
""")
check("the stamp beats a To header holding the sender's own mailing list",
      got == "me@school.example", got)

# A second forwarding account in the middle would otherwise win on Delivered-To,
# which is why the stamp is read before the chain rather than after it.
got = origin(f"""Delivered-To: {HUB}
Delivered-To: me@personal.example
X-Forwarded-For: me@personal.example {HUB}
X-MS-Exchange-ForwardingLoop: me@school.example;00000000-0000-0000-0000-000000000000
Subject: forwarded twice, school first
""")
check("a two-hop forward reports the first mailbox, not the middle one",
      got == "me@school.example", got)

# --- Fallbacks for an Exchange forward with no stamp -------------------------
got = origin(f"""Delivered-To: {HUB}
Return-Path: <bounces+SRS=xxxxx=YYYYY@school.onmicrosoft.com>
To: Given Family <me@school.example>
Subject: no forwarding stamp, To agrees with the tenant
""")
check("without the stamp the To is trusted as far as its domain",
      got == "school.example", got)

got = origin(f"""Delivered-To: {HUB}
Return-Path: <bounces+SRS=xxxxx=YYYYY@school.onmicrosoft.com>
To: Students <students@codepath.example>
Subject: no forwarding stamp, To is the sender's list
""")
check("a To that disagrees with the forwarding tenant loses to the tenant",
      got == "school", got)

got = origin(f"""Delivered-To: {HUB}
Return-Path: <bounces+SRS=xxxxx=YYYYY@school.onmicrosoft.com>
To: Students <students@codepath.example>, Given Family <me@school.example>
Subject: the real recipient sits behind the list
""")
check("scanning continues past a bulk To rather than stopping at it",
      got == "school.example", got)

# --- Mail that came straight in ----------------------------------------------
# The alias, never the hub's address: callers display and log this field.
got = origin(f"""Delivered-To: {HUB}
Return-Path: <bounces+60337934-2529-hub=gmail.example@em5923.automated.airbnb.example>
To: {HUB}
Subject: Example account notice
""")
check("mail addressed to the hub reports the alias, not the hub address",
      got == ACCOUNT, got)

check("a message with no routing headers at all reports the alias",
      origin("Subject: nothing to go on\n") == ACCOUNT)

# --- Shapes a real header block throws at the parser -------------------------
check("a display name around the address is stripped",
      origin(f"Delivered-To: {HUB}\nDelivered-To: Given Family <Me@School.EXAMPLE>\n")
      == "me@school.example")
check("the hub is recognised whatever case it arrives in",
      origin(f"Delivered-To: {HUB.upper()}\nTo: {HUB.upper()}\n") == ACCOUNT)
check("an empty forwarding stamp falls through instead of returning blank",
      origin(f"X-MS-Exchange-ForwardingLoop: ;tenant-guid\nTo: me@school.example\n")
      == "school.example")

check("a bare address with no display name still parses",
      sv._address("Me@School.EXAMPLE") == "me@school.example")
check("an address inside angle brackets parses the same way",
      sv._address("Given Family <Me@School.EXAMPLE>") == "me@school.example")
check("a header with no address at all gives an empty string, never None",
      sv._address("undisclosed-recipients:;") == "")

# --- The received stamp ------------------------------------------------------
# Local time, so the exact string is machine dependent; the shape and the
# failure path are what can actually regress.
stamp = sv._received(b'1 (X-GM-MSGID 1234567890123456789 INTERNALDATE '
                     b'"17-Sep-2026 17:05:38 -0700" BODY[HEADER.FIELDS (SUBJECT)] {42}')
check("an INTERNALDATE in a FETCH prefix comes back as YYYY-MM-DD HH:MM",
      bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", stamp)), stamp)
check("the stamp is converted out of the sender's offset into local time",
      stamp.startswith("2026-09-1"), stamp)
# Internaldate2tuple returns None rather than raising, and strftime is what
# rejects it, so this guards a TypeError and not a parse error.
check("a prefix with no INTERNALDATE gives an empty string rather than raising",
      sv._received(b'1 (X-GM-MSGID 123 BODY[TEXT] {10}') == "")

print()
print("ALL PASS" if not fails else f"SOME FAIL  {fails}")
sys.exit(1 if fails else 0)
