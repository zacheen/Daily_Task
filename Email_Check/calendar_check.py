"""Is this event already on the user's Google Calendar?

Reads the "secret address in iCal format" URLs from config.json. That is the
only Google Calendar path that does not need OAuth, which matters because this
project deliberately abandoned OAuth: the app is stuck in Testing publishing
status, so refresh tokens expire every seven days and an unattended run would
silently start failing. A secret iCal URL has no expiry and only breaks if the
user presses Reset in Calendar settings.

One URL covers one calendar, and the two configured feeds are complementary,
not redundant. Measured 2026-09-10: the Google feed carries 1197 events but no
CodePath class at all, while the Outlook feed carries 123 events including
every CodePath one. A single feed would have missed them, which is why
`calendarIcsUrls` is a list and why a one-feed NOT_FOUND proves nothing.

Ambiguity resolves to NOT_FOUND, never FOUND. A wrong NOT_FOUND costs one
redundant todo; a wrong FOUND means the user never puts the event in their
calendar and misses it.

    python calendar_check.py --summary "Tesla Resume Workshop" --date 2026-09-08
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CACHE_PATH = os.path.join(HERE, "calendar-cache.json")
CACHE_TTL = 1800
FETCH_TIMEOUT = 25
MIN_TOKEN_OVERLAP = 0.6

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_NOISE = re.compile(r"[^0-9a-z一-鿿]+")
_STOP = {"the", "a", "an", "and", "or", "of", "for", "to", "your", "you",
         "invitation", "invite", "invited", "synced", "reminder", "workshop",
         "event", "live", "class", "session", "meeting", "webinar"}


def normalize(text: str) -> set[str]:
    """Comparable tokens from a title.

    Calendar titles and mail subjects rarely match word for word, so emoji,
    punctuation and the boilerplate that only ever appears in one of the two
    ("Synced invitation:", "Reminder:") are dropped before comparing.
    """
    words = _NOISE.sub(" ", text.lower()).split()
    return {w for w in words if w not in _STOP and len(w) > 1}


def titles_match(subject: str, summary: str) -> bool:
    """Whether a mail subject and a calendar title describe the same thing.

    Two shared tokens are normally required. Coverage of the shorter side alone
    is not enough: a calendar entry abbreviated to one word ("Tesla") would then
    match every mail mentioning that word, and a wrong FOUND means the user
    never puts the event in their calendar.

    A single shared token only counts when the calendar title is that one token
    and it appears in the subject. That is the real abbreviation case, and the
    caller still requires the dates to agree, so a same-day collision on one
    word is needed to fool it.
    """
    a, b = normalize(subject), normalize(summary)
    if not a or not b:
        return False
    shared = a & b
    if len(shared) >= 2 and len(shared) / min(len(a), len(b)) >= MIN_TOKEN_OVERLAP:
        return True
    return len(b) == 1 and b <= a


def unfold(text: str) -> list[str]:
    """RFC 5545 folds long lines with a leading space on the continuation."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_events(text: str) -> list[dict]:
    events: list[dict] = []
    cur: dict | None = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            name, value = line.split(":", 1)
            cur[name.split(";")[0].upper()] = value.strip()
    return events


def event_date(value: str) -> dt.date | None:
    digits = re.sub(r"[^0-9]", "", value)[:8]
    try:
        return dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except Exception:
        return None


def rrule_covers(ev: dict, target: dt.date) -> bool:
    """Whether a recurring event plausibly falls on `target`.

    Only enough of RRULE is handled to cover the real case, a weekly class with
    an UNTIL date. Anything more exotic falls through to False so the caller
    reports NOT_FOUND rather than guessing FOUND.
    """
    rule = ev.get("RRULE", "")
    if not rule:
        return False
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    start = event_date(ev.get("DTSTART", ""))
    if start is None or target < start:
        return False
    until = event_date(parts["UNTIL"]) if "UNTIL" in parts else None
    if until and target > until:
        return False
    freq = parts.get("FREQ", "")
    if freq == "DAILY":
        return True
    if freq == "WEEKLY":
        days = [WEEKDAYS[d] for d in parts.get("BYDAY", "").split(",") if d in WEEKDAYS]
        return target.weekday() in (days or [start.weekday()])
    if freq == "MONTHLY":
        return target.day == start.day
    return False


def load_urls() -> tuple[list[str], str]:
    """Usable feed URLs, plus why there are none.

    The reason distinguishes missing / broken config.json from a deliberately
    empty `calendarIcsUrls`: the first two are faults needing different fixes,
    the third is not. statemachine.py's begin output is the authority on
    config health; this only keeps the local reason honest.
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return [], "config.json not found"
    except Exception:
        return [], "config.json is not readable JSON"
    if not isinstance(data, dict):
        return [], "config.json is not a JSON object"
    urls = data.get("calendarIcsUrls") or []
    good = [u for u in urls if isinstance(u, str) and u.startswith("https://")]
    return good, "" if good else "no usable calendarIcsUrls in config.json"


def fetch_all(urls: list[str]) -> tuple[list[dict], list[str]]:
    """Events from every feed, plus the URLs that failed.

    Cached briefly so several lookups in one round cost one request each feed.
    """
    cache = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}

    now = time.time()
    events: list[dict] = []
    failed: list[str] = []
    dirty = False
    for i, url in enumerate(urls):
        key = str(i)
        hit = cache.get(key)
        if hit and now - hit.get("at", 0) < CACHE_TTL:
            events.extend(hit.get("events", []))
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Email_Check/1.0"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                parsed = parse_events(r.read().decode("utf-8", "replace"))
            slim = [{k: e[k] for k in ("SUMMARY", "DTSTART", "RRULE") if k in e}
                    for e in parsed]
            cache[key] = {"at": now, "events": slim}
            events.extend(slim)
            dirty = True
        except Exception:
            # Stale cache beats no data, but the URL is still reported as failed
            # so the caller can flag the answer as unverified.
            if hit:
                events.extend(hit.get("events", []))
            failed.append(url)

    if dirty:
        try:
            with open(CACHE_PATH, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(cache, fh, ensure_ascii=False)
        except Exception:
            pass
    return events, failed


def lookup(subject: str, date_text: str) -> dict:
    urls, why = load_urls()
    if not urls:
        return {"status": "UNCHECKED", "reason": why}

    target = event_date(date_text) if date_text else None
    events, failed = fetch_all(urls)

    if not events and failed:
        return {"status": "UNCHECKED", "reason": "all calendar feeds failed",
                "failedFeeds": len(failed)}

    for ev in events:
        if not titles_match(subject, ev.get("SUMMARY", "")):
            continue
        if target is None:
            return {"status": "FOUND", "summary": ev.get("SUMMARY", ""),
                    "dtstart": ev.get("DTSTART", ""), "matchedOn": "title only"}
        start = event_date(ev.get("DTSTART", ""))
        if start == target or rrule_covers(ev, target):
            return {"status": "FOUND", "summary": ev.get("SUMMARY", ""),
                    "dtstart": ev.get("DTSTART", ""),
                    "matchedOn": "title and date"}

    out = {"status": "NOT_FOUND", "eventsScanned": len(events)}
    if failed:
        # Some feeds answered and some did not, so absence is not conclusive.
        out["status"] = "UNCHECKED"
        out["reason"] = "some calendar feeds failed"
        out["failedFeeds"] = len(failed)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Check a feed for an event")
    ap.add_argument("--summary", required=True, help="mail subject or event title")
    ap.add_argument("--date", default="", help="YYYY-MM-DD, optional")
    args = ap.parse_args(argv)
    print(json.dumps(lookup(args.summary, args.date), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
