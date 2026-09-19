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
import hashlib
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
# RRULE parts rrule_covers actually models. An unlisted part means the rule is
# narrower than what it computes, so it refuses the event instead.
RRULE_SUPPORTED = {"FREQ", "BYDAY", "UNTIL", "INTERVAL", "WKST"}
# Marks an event served from an expired cache entry. In-memory only, never
# written to the cache file, and read only by lookup.
STALE_TAG = "_fromExpiredCache"
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
    an UNTIL date. Every other part is rejected rather than ignored, because
    ignoring one always widens the match: INTERVAL=2 would match the skipped
    weeks, COUNT would recur forever, BYMONTHDAY would be overruled by the
    DTSTART comparison. A widened match is a false FOUND, which suppresses the
    todo for an event that is not actually on the calendar.
    """
    rule = ev.get("RRULE", "")
    if not rule:
        return False
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    if parts.keys() - RRULE_SUPPORTED:
        return False
    # WKST is in the supported set only because it changes nothing once
    # INTERVAL > 1 is refused below; it exists to place week boundaries.
    if parts.get("INTERVAL", "1") != "1":
        return False
    start = event_date(ev.get("DTSTART", ""))
    if start is None or target < start:
        return False
    until = event_date(parts["UNTIL"]) if "UNTIL" in parts else None
    if until and target > until:
        return False
    freq = parts.get("FREQ", "")
    if "BYDAY" in parts and freq != "WEEKLY":
        return False
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


def _cache_key(url: str) -> str:
    """Cache slot for one feed.

    Keyed by the URL, never by its position in calendarIcsUrls. Under the old
    positional key, replacing or reordering the list kept serving the previous
    calendar's events from the same slot until the entry aged out, which is a
    FOUND for an event the user does not have.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def fetch_all(urls: list[str]) -> tuple[list[dict], list[str]]:
    """Events from every feed, plus the URLs that failed.

    Cached briefly so several lookups in one round cost one request each feed.
    Events served from an expired entry because the fetch failed carry
    STALE_TAG; a fresh within-TTL hit is normal operation and carries nothing.
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
    wanted = {_cache_key(u) for u in urls}
    for url in urls:
        key = _cache_key(url)
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
            # Expired cache beats no data, but it is only evidence the event
            # existed at some unbounded past time, so each copy is tagged and
            # lookup won't answer FOUND on a tag alone. Copies are tagged, not
            # `cache[key]` itself, because another feed succeeding sets `dirty`,
            # which would write the tag into the cache file as if permanent.
            if hit:
                events.extend(dict(e, **{STALE_TAG: True})
                              for e in hit.get("events", []))
            failed.append(url)

    if dirty:
        try:
            with open(CACHE_PATH, "w", encoding="utf-8", newline="\n") as fh:
                # Entries for URLs no longer configured are dropped, or a feed
                # removed from config.json would keep answering out of the
                # cache file forever.
                json.dump({k: v for k, v in cache.items() if k in wanted},
                          fh, ensure_ascii=False)
        except Exception:
            pass
    return events, failed


def lookup(subject: str, date_text: str) -> dict:
    urls, why = load_urls()
    if not urls:
        return {"status": "UNCHECKED", "reason": why}

    # An unparseable --date is not the same as no --date. Falling through to
    # the title-only branch would answer FOUND off the title alone, so a typo
    # like 2026-02-31 could cancel a todo by matching a different month's
    # event with the same name.
    target = None
    if date_text:
        target = event_date(date_text)
        if target is None:
            return {"status": "UNCHECKED",
                    "reason": "could not read --date " + date_text}

    events, failed = fetch_all(urls)

    if not events and failed:
        return {"status": "UNCHECKED", "reason": "all calendar feeds failed",
                "failedFeeds": len(failed)}

    stale_hit: dict | None = None
    for ev in events:
        if not titles_match(subject, ev.get("SUMMARY", "")):
            continue
        if target is None:
            matched_on = "title only"
        else:
            start = event_date(ev.get("DTSTART", ""))
            if not (start == target or rrule_covers(ev, target)):
                continue
            matched_on = "title and date"
        # Remember and keep scanning rather than returning. A later event may
        # match from a live feed, and one live match is enough for presence.
        if ev.get(STALE_TAG):
            stale_hit = stale_hit or ev
            continue
        return {"status": "FOUND", "summary": ev.get("SUMMARY", ""),
                "dtstart": ev.get("DTSTART", ""), "matchedOn": matched_on}

    if stale_hit is not None:
        # Presence needs one live source; an expired copy is not one. Answering
        # FOUND here would cancel the todo for an event the user may already
        # have deleted, and nothing would ever say so.
        return {"status": "UNCHECKED",
                "reason": "only an expired cache copy matched",
                "failedFeeds": len(failed),
                "summary": stale_hit.get("SUMMARY", "")}

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
