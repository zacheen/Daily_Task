"""Tests for the calendar lookup. Pure functions only, no network."""

import datetime as dt
import importlib.util
import os
import shutil
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_check.py")
WORK = tempfile.mkdtemp(prefix="cctest.")
spec = importlib.util.spec_from_file_location("cc", SRC)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

fails = []


def check(label, cond, extra=""):
    # ascii(), because conda run relays stdout through a cp950 console that
    # raises on non-ASCII and the fixtures contain emoji.
    print(("PASS  " if cond else "FAIL  ") + label
          + (("  " + ascii(extra)) if extra else ""))
    if not cond:
        fails.append(label)


# Titles never match word for word between a mail subject and a calendar entry.
check("a reminder subject matches the calendar title",
      cc.titles_match("Tesla Supercharging Your Resume Workshop",
                      "Tesla Supercharging Your Resume Workshop"))
check("boilerplate prefixes do not block a match",
      cc.titles_match("Invitation: AI201-1B | Live Class @ Weekly from 3pm",
                      "AI201-1B"))
check("emoji do not block a match",
      cc.titles_match("Synced invitation: ✨AI201-1B | Live Class ✨",
                      "✨AI201-1B | Live Class ✨"))
check("unrelated titles do not match",
      not cc.titles_match("Campus to Career: Guest Speaker",
                          "Tesla Supercharging Your Resume Workshop"))
check("an empty title never matches", not cc.titles_match("", "anything"))
# "Workshop" alone is stripped as boilerplate, so it must not carry a match.
check("shared boilerplate alone is not a match",
      not cc.titles_match("Resume Workshop", "Networking Workshop"))

ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "BEGIN:VEVENT",
    "SUMMARY:Tesla Supercharging Your Resume Workshop",
    "DTSTART;TZID=America/Los_Angeles:20260908T163000",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "SUMMARY:✨AI201-1B | Live Class ✨",
    "DTSTART;TZID=America/Los_Angeles:20260916T150000",
    "RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20261118T235959Z",
    "END:VEVENT",
    "BEGIN:VEVENT",
    # A real folder emits CRLF + one space; the trailing space belongs to the
    # first line's content, not the fold marker. Omitting it would merge the words
    # into "thenext line", since unfolding only strips the leading fold marker.
    "SUMMARY:A folded title that continues on the ",
    " next line",
    "DTSTART;VALUE=DATE:20261001",
    "END:VEVENT",
    "END:VCALENDAR",
])

# An abbreviated one-word calendar entry is the real case (the user has an event
# titled just "Tesla"), but coverage of the shorter side alone would let any mail
# mentioning that word match, and a wrong FOUND loses the event entirely.
check("a one-word calendar title matches when contained in the subject",
      cc.titles_match("Tesla Supercharging Your Resume Workshop", "Tesla"))
check("a one-word title does not match an unrelated subject",
      not cc.titles_match("Campus to Career: Guest Speaker", "Tesla"))
check("one shared token is not enough when both sides are longer",
      not cc.titles_match("Google Cloud Next registration", "Google Interview Loop"))
check("two shared tokens are enough",
      cc.titles_match("You are invited to Campus to Career: Guest Speaker",
                      "Campus to Career"))

evs = cc.parse_events(ICS)
check("all VEVENTs are parsed", len(evs) == 3, len(evs))
check("folded lines are rejoined",
      evs[2]["SUMMARY"] == "A folded title that continues on the next line", evs[2])
check("the fold marker space is stripped, not doubled",
      "  " not in evs[2]["SUMMARY"], evs[2]["SUMMARY"])
check("a TZID prefix does not break the date",
      cc.event_date(evs[0]["DTSTART"]) == dt.date(2026, 9, 8), evs[0]["DTSTART"])
check("a date-only DTSTART parses",
      cc.event_date(evs[2]["DTSTART"]) == dt.date(2026, 10, 1))
check("junk dates return None", cc.event_date("not-a-date") is None)

# The real recurring case: a weekly Wednesday class with an UNTIL.
weekly = evs[1]
check("a later Wednesday in range is covered",
      cc.rrule_covers(weekly, dt.date(2026, 9, 23)))
check("a Thursday is not covered",
      not cc.rrule_covers(weekly, dt.date(2026, 9, 24)))
check("a date before DTSTART is not covered",
      not cc.rrule_covers(weekly, dt.date(2026, 9, 9)))
check("a date after UNTIL is not covered",
      not cc.rrule_covers(weekly, dt.date(2026, 12, 2)))
check("a non-recurring event is not covered by rrule",
      not cc.rrule_covers(evs[0], dt.date(2026, 9, 8)))
# Anything the parser does not understand must fall through to False, so the
# caller reports NOT_FOUND rather than guessing FOUND.
check("an unsupported FREQ falls through to False",
      not cc.rrule_covers({"RRULE": "FREQ=SECONDLY", "DTSTART": "20260101"},
                          dt.date(2026, 1, 2)))

# With no URLs configured the answer must be UNCHECKED, never NOT_FOUND.
# NOT_FOUND would be a claim we cannot make, and it changes what the caller does.
real_load = cc.load_urls
cc.load_urls = lambda: ([], "no usable calendarIcsUrls in config.json")
res = cc.lookup("Tesla Supercharging Your Resume Workshop", "2026-09-08")
cc.load_urls = real_load
check("no configured feed gives UNCHECKED", res["status"] == "UNCHECKED", res)
check("the reason survives into the result", res["reason"], res)

# The three empty-list causes need different fixes, so they must not share a
# reason. A missing file reported as an empty list is what hid the fault before.
cc.CONFIG_PATH = os.path.join(os.path.dirname(SRC), "does-not-exist.json")
check("a missing config says so", cc.load_urls() == ([], "config.json not found"),
      cc.load_urls())
bad = os.path.join(WORK, "bad.json")
open(bad, "w", encoding="utf-8").write("{ not json")
cc.CONFIG_PATH = bad
check("an unreadable config says so",
      cc.load_urls()[1] == "config.json is not readable JSON", cc.load_urls())
open(bad, "w", encoding="utf-8").write("[]")
check("a non-object config says so",
      cc.load_urls()[1] == "config.json is not a JSON object", cc.load_urls())
open(bad, "w", encoding="utf-8").write('{"calendarIcsUrls": ["http://insecure/a.ics"]}')
check("a non-https url is not usable",
      cc.load_urls() == ([], "no usable calendarIcsUrls in config.json"),
      cc.load_urls())
cc.CONFIG_PATH = os.path.join(os.path.dirname(SRC), "config.json")

# A partially failed fetch also cannot prove absence.
cc.load_urls = lambda: (["https://example.invalid/a.ics",
                         "https://example.invalid/b.ics"], "")
cc.fetch_all = lambda urls: (cc.parse_events(ICS), ["https://example.invalid/b.ics"])
res = cc.lookup("Nothing like this exists", "2026-09-08")
check("a partial feed failure gives UNCHECKED, not NOT_FOUND",
      res["status"] == "UNCHECKED", res)
res = cc.lookup("Tesla Supercharging Your Resume Workshop", "2026-09-08")
check("a hit still reports FOUND despite a failed feed",
      res["status"] == "FOUND", res)

# The happy paths, with every feed healthy.
cc.fetch_all = lambda urls: (cc.parse_events(ICS), [])
res = cc.lookup("Tesla Supercharging Your Resume Workshop", "2026-09-08")
check("a single event on the right date is FOUND", res["status"] == "FOUND", res)
res = cc.lookup("Tesla Supercharging Your Resume Workshop", "2026-09-09")
check("the same title on the wrong date is NOT_FOUND", res["status"] == "NOT_FOUND", res)
res = cc.lookup("Synced invitation: ✨AI201-1B | Live Class ✨", "2026-09-23")
check("a weekly class is FOUND on a later occurrence", res["status"] == "FOUND", res)
res = cc.lookup("Campus to Career: Guest Speaker", "2026-09-11")
check("an event that is not there is NOT_FOUND", res["status"] == "NOT_FOUND", res)

shutil.rmtree(WORK, ignore_errors=True)

print()
print("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails))
sys.exit(1 if fails else 0)
