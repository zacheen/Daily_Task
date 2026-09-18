"""Regression tests for the todo GUI's pure helpers.

test_statemachine.py only imports statemachine.py, so the deadline ordering and
the GUI's copy of the id policy had no net at all. Both exist because of real
review findings, so they get one here.
"""

import datetime as dt
import importlib.util
import os
import sys

# Email_Check/, one level up from test/. The viewer sits under task_list/.
CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(CODE, "task_list", "task_list_gui.py")
spec = importlib.util.spec_from_file_location("tg", SRC)
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)

fails = []


def check(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


TODAY = dt.date(2026, 9, 8)
key = lambda text: tg._deadline_key(text, TODAY)

# The whole point of parsing: lexicographic order put "10-2" first.
check("a later month sorts after an earlier one", key("10-2") > key("9-15"),
      (key("9-15"), key("10-2")))
check("zero padding does not change the order", key("09-15") == key("9-15"))
check("slashes parse the same as dashes", key("9/15") == key("9-15"))

# Year-end wrap: a bare MM-DD before today's month belongs to next year.
check("January sorts after December", key("01-05") > key("12-30"),
      (key("12-30"), key("01-05")))
check("next-year January gets next year", key("01-05")[0] == 2027, key("01-05"))
check("this-month deadline stays this year", key("09-20")[0] == 2026, key("09-20"))

# A recently overdue bare date must surface, not sink to the bottom. Reading it
# as next year buried the most urgent item in the list.
check("last month sorts before this month", key("08-30") < key("09-20"),
      (key("08-30"), key("09-20")))
check("recently overdue stays in the current year", key("08-30")[0] == 2026, key("08-30"))
check("overdue sorts above a future deadline", key("08-30") < key("12-01"),
      (key("08-30"), key("12-01")))
# The wrap still has to work for dates far behind the current month.
check("far-behind months wrap to next year", key("01-05")[0] == 2027, key("01-05"))
check("January still sorts after December", key("01-05") > key("12-30"))

# An explicit year wins over the wrap heuristic.
check("a full date keeps its own year", key("2026-01-05")[0] == 2026, key("2026-01-05"))
check("explicit years order correctly", key("2027-01-05") > key("2026-12-30"))

# Malformed input must sort last, never raise, never hide the row.
for bad in ("", "尚未確認", "ASAP", "next week", "9", "--", "9-15-16-17"):
    try:
        k = key(bad)
        ok = isinstance(k, tuple)
    except Exception as exc:
        ok = False
        k = type(exc).__name__
    # ascii(), not !r. conda run relays stdout through a cp950 console that
    # raises on non-ASCII, and one of the inputs is CJK.
    check("malformed deadline " + ascii(bad) + " is handled", ok, k)
check("unparseable deadlines sort last", key("ASAP") > key("2027-12-31"), key("ASAP"))

# The GUI's id policy must match the state machine's, or ticking one id-less
# todo would archive every other id-less todo along with it.
check("a normal id is usable", tg._usable_id({"id": "18756537882105"}) == "18756537882105")
for junk in (None, "", "   ", "None", "none", "null", "NULL"):
    check("id " + ascii(junk) + " is rejected", tg._usable_id({"id": junk}) is None)
check("a missing id key is rejected", tg._usable_id({}) is None)
check("surrounding whitespace is stripped", tg._usable_id({"id": " 123 "}) == "123")

# The exit policy decides whether an unattended run leaves a resident server
# behind, so it is a pure function with a net rather than logic inside a thread.
T = tg.CLIENT_TIMEOUT
G = tg.STARTUP_GRACE
check("no page yet, inside the grace, keeps serving",
      tg.exit_reason(1000, 1000, {}, False) == "")
check("no page yet, past the grace, exits",
      tg.exit_reason(1000 + G + 1, 1000, {}, False) == "browser never connected",
      "an unattended run whose browser never opened must not idle forever")
check("one live page keeps serving",
      tg.exit_reason(1000, 900, {"a": 1000}, True) == "")
check("a page gone quiet past the timeout exits",
      tg.exit_reason(1000 + T + 1, 900, {"a": 1000}, True) == "page closed")
check("a page still inside the timeout keeps serving",
      tg.exit_reason(1000 + T, 900, {"a": 1000}, True) == "",
      "the boundary must not kill a tab throttled to one ping a minute")
# The multi-tab case is the whole reason liveness is per page rather than a
# single last-seen timestamp.
check("a second tab keeps serving after the first says goodbye",
      tg.exit_reason(1000, 900, {"b": 1000}, True) == "")
check("the last tab closing exits",
      tg.exit_reason(1000, 900, {}, True) == "page closed")
check("live_clients drops only the stale ones",
      tg.live_clients(1000 + T + 1, {"old": 1000, "new": 1000 + T + 1}) ==
      {"new": 1000 + T + 1})
# A page that reloads gets a fresh id; the old one lingering must not exit.
check("a stale id alongside a live one keeps serving",
      tg.exit_reason(1000 + T + 1, 900,
                     {"old": 1000, "new": 1000 + T}, True) == "")

# The ping interval is hand-typed both in index() and in the PAGE script, with
# nothing tying them together. A stale placeholder is an undefined JS
# identifier that throws, which silently kills the pagehide listener declared
# after it in the same script, not just the ping.
page = tg.index()
check("index substitutes the ping interval",
      str(tg.PING_EVERY_MS) in page, tg.PING_EVERY_MS)
check("no placeholder survives into the served page",
      "__PING_MS__" not in page)
check("the page still asks for a closing beacon", "sendBeacon" in page)

print()
print("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails))
sys.exit(1 if fails else 0)
