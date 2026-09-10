"""Deterministic state machine for the Gmail important-mail check.

The scheduled task is a fresh LLM session every run. Anything that has to be
*correct* rather than *judged* lives here, so a cold read cannot get it wrong.
The LLM keeps only two jobs, calling the Gmail MCP and deciding importance.

Coverage is proved from query bounds alone. A saturated window is bisected on
its own [lo, hi], never on an email's displayed timestamp, because Gmail filters
on internalDate while the displayed Date header lags it by an unbounded amount
(measured 2-10s, but delayed delivery and forwarding remove any upper bound, and
this mailbox is a forwarding hub).

stdout is always pure ASCII JSON. conda run relays it through a cp950 console
that raises UnicodeEncodeError on non-ASCII, so status is reported as codes and
INSTRUCTIONS.md maps each code to behaviour.

Subcommands
    begin   start a round, return debts to settle and the first query
    step    record one search result, return the next query or done
    commit  apply the LLM's findings from round.json and persist atomically
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import time
from typing import Any, NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
ROUND_PATH = os.path.join(HERE, "round.json")
PROGRESS_PATH = os.path.join(HERE, "round-progress.json")
# The GUI is the only writer of the three request files below and only ever
# reads state.json and the archive, so no file has two writers and no
# cross-process lock is needed. Each one is a verb the user aimed at a row:
# ticked means done, restore means un-archive, promote means the notified mail
# in the GUI's 重要事項 section is really a todo. All three are read here.
CHECKED_PATH = os.path.join(HERE, "todos-checked.json")
ARCHIVE_PATH = os.path.join(HERE, "todos-archive.json")
RESTORE_PATH = os.path.join(HERE, "todos-restore.json")
PROMOTE_PATH = os.path.join(HERE, "todos-promote.json")
CONFIG_PATH = os.path.join(HERE, "config.json")

MAX_RESULTS = 40
MAX_SEARCHES_PER_ROUND = 5
BODY_BUDGET = 3
DEBT_BODY_BUDGET = 2
BOUNDARY_SLACK = 900
# Months a bare MM-DD may lag the current month before it is read as next year
# rather than as recently overdue. Mirrors the viewer's own constant.
BARE_DEADLINE_LOOKBACK = 3
FIRST_RUN_LOOKBACK = 24 * 3600
JUDGE_WAIT_LIMIT_ROUNDS = 2
# How many rounds a notice stays reviewable before it is swept into the archive,
# where ARCHIVE_TTL disposes of it. One means "until the next round", which is
# what makes leaving a notice alone a decision instead of a growing backlog.
NOTICE_REVIEW_ROUNDS = 1
BACKLOG_NOTICE_EVERY = 3
NOTIFIED_KEEP = 100
# How long an archived todo stays recoverable. Three days to notice a mis-tick,
# then it is gone for good.
ARCHIVE_TTL = 3 * 86400
# Stands in for a rev that could not be read. Never equal to a real rev, so a
# compare-and-swap against it always refuses the write.
UNREADABLE_REV = "__unreadable__"
# Local-clock window in which a round may open the todo list. The 06:00
# round would otherwise pop a browser window while the user is asleep.
# End is exclusive; 23 still lets the 21:30 round through.
GUI_OPEN_HOURS = (9, 23)


def within_gui_hours(hour: int, window: tuple[int, int]) -> bool:
    """Whether `hour` falls in `window`, whose end is exclusive.

    `window` is a parameter rather than a GUI_OPEN_HOURS-bound default, since
    a default binds at definition time and would silently ignore both a
    test's override and any later edit to the constant.

    Wrap-around is handled so that moving the window across midnight narrows
    it instead of silently matching nothing.
    """
    lo, hi = window
    return lo <= hour < hi if lo <= hi else (hour >= lo or hour < hi)


def read_config() -> dict[str, Any]:
    """Health of config.json, reported by begin and remembered by commit.

    A bad config degrades silently otherwise: the LLM just never applies the
    identity rules and nothing says why. `missing` / `malformed` / `incomplete`
    stay distinct because each needs a different fix. Only the first two also
    disable calendar lookups; `incomplete` means `names` is empty and says
    nothing about whether `calendarIcsUrls` is usable.

    `calendarUrls` is informational only and never alerts: a count of 0 is the
    documented, deliberate way to switch calendar checking off, not a fault.
    """
    blank = {"names": 0, "calendarUrls": 0}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"status": "missing", **blank}
    except Exception:
        return {"status": "malformed", **blank}
    if not isinstance(data, dict):
        return {"status": "malformed", **blank}
    names = data.get("names") or {}
    if not isinstance(names, dict):
        names = {}
    count = sum(len(v) for v in names.values() if isinstance(v, list))
    urls = data.get("calendarIcsUrls") or []
    good = sum(1 for u in urls if isinstance(u, str) and u.startswith("https://"))
    return {"status": "present" if count else "incomplete",
            "names": count, "calendarUrls": good}


def excluded_senders() -> list[str]:
    """Addresses every search strips out, read from config.json.

    Lives in the gitignored config rather than in this file so that a real
    mailbox address stays off the public remote.

    An unreadable config yields no exclusion, which widens the search instead
    of narrowing it. The opposite default would silently drop mail for as long
    as the config stayed broken, and read_config already alerts on the fault.

    Deliberately not cached: re-reading per call is a few reads per round, and
    a cache bound at import would ignore both a config edit between rounds and
    a test's CONFIG_PATH override.
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("excludedSenders") or []
    if not isinstance(raw, list):
        return []
    return [s.strip() for s in raw if isinstance(s, str) and s.strip()]


def _usable_id(item: dict) -> str | None:
    """The queue key, or None when the item carries no usable id.

    Items without one must never share a key. Two id-less entries would
    otherwise collapse into a single record whose fields overwrite each other,
    and since coverage has already moved past both source messages neither can
    be recovered. Callers keep such items aside instead of merging or dropping.
    """
    raw = item.get("id")
    if raw is None:
        return None
    key = str(raw).strip()
    return key if key and key.lower() not in ("none", "null") else None


def resolve_deadline(text: str, today: dt.date | None = None) -> str:
    """A deadline as YYYY-MM-DD, resolved once so it can never be re-read.

    INSTRUCTIONS asks the LLM for a full date, so a bare MM-DD is a contract
    violation rather than an expected input. It still has to be handled, because
    leaving it bare means every render re-guesses the year against that day's
    date, and an unfinished todo could drift from "just overdue" to "next year"
    on its own. A bare date is read as the recent past for the same reason the
    viewer does: burying an overdue item is the failure this list exists to
    prevent. Unparseable text is returned untouched for the viewer to sort last.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    parts = [p for p in raw.replace("/", "-").split("-") if p.strip().isdigit()]
    nums = [int(p) for p in parts]
    if len(nums) >= 3:
        y, m, d = nums[0], nums[1], nums[2]
    elif len(nums) == 2:
        today = today or dt.date.today()
        m, d = nums[0], nums[1]
        y = today.year + 1 if today.month - m > BARE_DEADLINE_LOOKBACK else today.year
    else:
        return raw
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return raw


def _content_key(item: dict) -> str:
    """Stable key for an item with no usable id.

    Keeping id-less items apart is not the same as being idempotent. Keyed by
    position they gained one extra copy per replay of the same findings.
    """
    return json.dumps({k: v for k, v in item.items() if k != "firstDeferredRound"},
                      ensure_ascii=True, sort_keys=True, default=str)


def _read_archive() -> tuple[list, str, Any]:
    """Archived todos, or an empty list plus a reason.

    Valid JSON can still have the wrong shape, and callers iterate the result,
    so the shape check belongs here rather than at each call site.
    """
    if not os.path.exists(ARCHIVE_PATH):
        return [], "", None
    try:
        with open(ARCHIVE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("archived", [])
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            return [], "archive unreadable", None
        return items, "", data.get("rev")
    except Exception:
        return [], "archive unreadable", None


def _archive_disk_rev() -> tuple[bool, Any]:
    """Whether the archive exists, and the rev it currently carries.

    The counterpart of State._disk_rev. Both exist so a compare-and-swap can
    read the real file instead of going through the public reader, which a
    caller may have replaced.
    """
    if not os.path.exists(ARCHIVE_PATH):
        return False, None
    try:
        with open(ARCHIVE_PATH, encoding="utf-8") as fh:
            return True, json.load(fh).get("rev")
    except Exception:
        return True, UNREADABLE_REV


def _write_archive(items: list, expect_rev: Any) -> bool:
    """Replace the archive, but only if it still carries `expect_rev`.

    Without this check a round that read the archive, then lost the race to
    another round's append, would write its stale list back and erase an entry
    that no longer exists anywhere else. state.json has the same guard; the
    archive needed it too, and it is the only place archive writes happen so a
    caller cannot skip it.

    Returns False when the revision moved, so callers can abandon their change
    and retry next round rather than destroying someone else's.

    The revision comes from _archive_disk_rev rather than _read_archive, so the
    guard cannot be blinded by a caller that substituted the reader. That is not
    hypothetical: a race harness pinned _read_archive to a stale snapshot, which
    made the verification read stale too, and the write sailed through.
    """
    _, disk_rev = _archive_disk_rev()
    # Poison on both sides. Two unreadable revs would compare equal and let
    # the write through on no evidence, which is exactly what this guard
    # exists to stop.
    if UNREADABLE_REV in (disk_rev, expect_rev):
        return False
    # Only the revisions are compared, deliberately. An archive written before
    # this field existed has no rev, and _read_archive reports None for both
    # "absent" and "legacy", so pairing existence with rev-is-None would refuse
    # every write to a legacy file and wedge archiving permanently. Matching on
    # rev alone lets the first write migrate the file by stamping one, while a
    # concurrent write still shows up as a mismatch.
    if disk_rev != expect_rev:
        return False
    try:
        _atomic_json(ARCHIVE_PATH, {"archived": items, "rev": f"{time.time_ns()}"})
        return True
    except Exception:
        return False


def _read_id_request(path: str, key: str, fault: str) -> tuple[set[str], str, Any]:
    """Ids the GUI asked for, why they could not be read, and the file's rev.

    Every GUI-written request file goes through here, in the same (data, error,
    rev) shape as _read_archive, so the three of them cannot drift apart.

    A non-empty reason means "unknown", never "nothing requested": acting on a
    file that could not be read would archive, restore, or promote on no
    evidence. An absent file is not a fault, since the GUI only writes one
    once the user acts on something.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(x) for x in data.get(key, [])}, "", data.get("rev")
    except FileNotFoundError:
        return set(), "", None
    except Exception:
        return set(), fault, None


def _read_ticks() -> tuple[set[str], str, Any]:
    return _read_id_request(CHECKED_PATH, "checkedIds", "tick file unreadable")


def _merge(prior: dict, latest: dict) -> dict:
    """Field-level merge, newest reported value wins, always a fresh dict.

    A re-report that omits from/subject/snippet must not erase what was stored,
    because once coverage advances past a message it never appears in a query
    again and an id with no context cannot be turned into a notification. Those
    fields are immutable per message id, so falling back cannot mix versions.

    Stored snippets keep Gmail's raw preview text, which leaves
    quoted-printable soft breaks in place ("t= ime"), so never substring-match
    a snippet in code and strip the artifacts before quoting one to the user.

    Only `None` is skipped, not every falsy value. An omitted key never reaches
    `latest.items()` in the first place, so treating `""` as absent would delete
    legitimately blank fields such as an empty Gmail subject, and nothing would
    be left to fall back to on later rounds.
    """
    kept = dict(prior)
    kept.update({k: v for k, v in latest.items() if v is not None})
    return kept


def _atomic_json(path: str, payload: Any) -> None:
    """The only write path for every file this module owns.

    A reader always sees the complete old or complete new file, which is
    what lets the GUI read state.json with no lock, and what stops a
    half-written state.json from reading as corrupt and halting the task.
    """
    fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".sm.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class NoticeFiling(NamedTuple):
    """What record_notices did. Named because both halves are counts that reach
    the user as separate emitted fields, so a swapped pair would look plausible.
    """
    filed: int
    dropped: int


class StaleStateError(Exception):
    """state.json changed under us, so this round's writes are not safe.

    Atomic replace stops a half-written file; it does nothing about one round
    overwriting another's changes. Four tasks share this file and all four fire
    together when the app reopens after being closed past several slots.
    """


class StateError(Exception):
    """State file exists but cannot be trusted. Never guess a watermark."""


class Coverage:
    """Which spans of time have been searched.

    `intervals` holds half-open [lo, hi) spans still needing a search, disjoint
    and sorted ascending. `horizon` is the newest epoch ever queued. Everything
    below the lowest unscanned interval is therefore searched, which makes
    `covered_through` a derived value rather than a counter that could be
    advanced past a gap.
    """

    def __init__(self, horizon: int, intervals: list[list[int]]):
        self.horizon = horizon
        self.intervals = intervals
        self._normalize()

    @property
    def covered_through(self) -> int:
        return min(lo for lo, _ in self.intervals) if self.intervals else self.horizon

    def _normalize(self) -> None:
        """Merges only genuinely overlapping spans. Adjacent ones must stay
        separate even though [a,b) + [b,c) == [a,c), because bisect() produces
        exactly that shape and merging would undo every split and livelock."""
        spans = sorted(iv for iv in self.intervals if iv[1] > iv[0])
        merged: list[list[int]] = []
        for lo, hi in spans:
            if merged and lo < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        self.intervals = merged

    def extend_to(self, scan_end: int, slack: int = 0) -> None:
        """Queue everything from the horizon up to scan_end.

        `slack` backs the new interval's start up to absorb clock skew and Gmail
        index lag. Applying it here, once, is what lets bisection genuinely
        narrow the searched range.
        """
        if scan_end > self.horizon:
            self.intervals.append([max(0, self.horizon - slack), scan_end])
            self.horizon = scan_end
            self._normalize()

    def newest(self, skip: list[list[int]]) -> list[int] | None:
        """Newest first, so a long catch-up backlog never starves today's mail."""
        candidates = [iv for iv in self.intervals if iv not in skip]
        return candidates[-1] if candidates else None

    def retire(self, lo: int, hi: int) -> None:
        self.intervals = [iv for iv in self.intervals if iv != [lo, hi]]
        self._normalize()

    def bisect(self, lo: int, hi: int) -> bool:
        """Split a saturated window. False means it cannot be split, i.e. more
        than MAX_RESULTS messages share a single second."""
        if hi - lo <= 1:
            return False
        mid = lo + (hi - lo) // 2
        self.intervals = [iv for iv in self.intervals if iv != [lo, hi]]
        self.intervals.extend([[lo, mid], [mid, hi]])
        self._normalize()
        return True


class State:
    """Everything state.json holds: coverage, the four debt queues, and the
    bookkeeping that decides whether a notification has already been sent."""

    def __init__(self, data: dict[str, Any]):
        self.coverage = Coverage(int(data["horizon"]),
                                 [[int(a), int(b)] for a, b in data.get("intervals", [])])
        self.pendingNotify: list[dict] = list(data.get("pendingNotify", []))
        self.pendingJudge: list[dict] = list(data.get("pendingJudge", []))
        self.todos: list[dict] = list(data.get("todos", []))
        # What was pushed recently, waiting for the user to promote it to a todo
        # or leave it. Unlike the other three queues this one has a deadline:
        # sweep_notices empties it, so nothing here survives without a decision.
        self.notices: list[dict] = list(data.get("notices", []))
        self.notifiedIds: list[str] = list(data.get("notifiedIds", []))
        self.roundSeq: int = int(data.get("roundSeq", 0))
        # The revision this instance was loaded at. save() refuses to write if
        # the file on disk has moved on, which is what makes a lost update an
        # explicit abort rather than silent data loss.
        self.rev: str | None = data.get("rev")
        self.lastBacklogNoticeRound: int = int(data.get("lastBacklogNoticeRound", -99))
        # Empty means never observed, so a first run with a broken config still
        # alerts instead of comparing equal to some assumed-healthy default.
        self.lastConfigStatus: str = str(data.get("lastConfigStatus", ""))

    @classmethod
    def load_or_init(cls, now: int) -> tuple["State", bool]:
        if not os.path.exists(STATE_PATH):
            return cls({"horizon": now - FIRST_RUN_LOOKBACK}), True
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            if "horizon" not in data:
                raise ValueError("missing horizon")
            return cls(data), False
        except Exception as exc:
            raise StateError(str(exc)) from exc

    @staticmethod
    def _disk_rev() -> tuple[bool, Any]:
        """Whether state.json exists, and the rev it currently carries."""
        if not os.path.exists(STATE_PATH):
            return False, None
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                return True, json.load(fh).get("rev")
        except Exception:
            # Unreadable is not "unchanged". Refusing to write is the safe move,
            # and cmd_* already has a path for a corrupt state file.
            return True, UNREADABLE_REV

    def save(self) -> None:
        """Temp file + os.replace, guarded by a revision check.

        The check closes the round-length window where another task reads the
        same state, mutates it and writes back. It is a stale-write detector,
        not a lock: a few microseconds still separate the read below from the
        replace. That residue is acceptable because the window it replaces
        spanned the whole round, including every Gmail call and LLM decision.
        """
        _, disk = self._disk_rev()
        # Revisions only, deliberately, for the same reason _write_archive
        # compares revisions only: a state.json written before this field
        # existed carries no rev, so pairing existence with rev-is-None refuses
        # every write to it and no round can ever save again. Matching on rev
        # alone lets the first write migrate the file by stamping one, while a
        # concurrent write still shows up as a mismatch because every
        # successful save stamps a real one.
        if UNREADABLE_REV in (disk, self.rev) or disk != self.rev:
            raise StaleStateError(
                f"state.json rev is {disk!r}, this round loaded {self.rev!r}")

        self.rev = f"{time.time_ns()}"
        payload = {
            "horizon": self.coverage.horizon,
            "intervals": self.coverage.intervals,
            "coveredThrough": self.coverage.covered_through,
            "pendingNotify": self.pendingNotify,
            "pendingJudge": self.pendingJudge,
            "todos": self.todos,
            "notices": self.notices,
            "notifiedIds": self.notifiedIds,
            "roundSeq": self.roundSeq,
            "lastBacklogNoticeRound": self.lastBacklogNoticeRound,
            "lastConfigStatus": self.lastConfigStatus,
            "rev": self.rev,
        }
        _atomic_json(STATE_PATH, payload)

    def reconcile_notify(self, failed: list[dict], notified: set[str]) -> None:
        """Keep anything not explicitly resolved.

        The LLM only ever sees a budget-limited slice of these queues, so
        treating "not re-listed this round" as "resolved" drops debt it was
        never shown. A freshly reported summary supersedes the stored one.
        """
        kept: dict[str, dict] = {}
        orphans: dict[str, dict] = {}
        for item in list(self.pendingNotify) + list(failed):
            key = _usable_id(item)
            if key is None:
                orphans[_content_key(item)] = dict(item)
            elif key not in notified:
                kept[key] = _merge(kept.get(key, {}), item)
        self.pendingNotify = list(kept.values()) + list(orphans.values())

    def reconcile_judge(self, deferred: list[dict], resolved: set[str]) -> None:
        """Same keep-by-default rule as reconcile_notify.

        firstDeferredRound is carried over from whichever copy already had one,
        so a re-defer cannot reset the wait clock and strand an item short of
        the overdue escape hatch.
        """
        kept: dict[str, dict] = {}
        orphans: dict[str, dict] = {}
        for item in list(self.pendingJudge) + list(deferred):
            key = _usable_id(item)
            if key is None:
                orphans[_content_key(item)] = dict(item)
                continue
            if key in resolved:
                continue
            prior = kept.get(key, {})
            merged = _merge(prior, item)
            merged["firstDeferredRound"] = (
                prior.get("firstDeferredRound")
                or item.get("firstDeferredRound")
                or self.roundSeq)
            kept[key] = merged
        self.pendingJudge = list(kept.values()) + list(orphans.values())

    def reconcile_todos(self, reported: list[dict], drop: set[str]) -> None:
        """Upsert reported todos by message id, keep everything else.

        A todo is the only surviving record of its message. Once coverage moves
        past it the message never appears in a query again, so the list can only
        ever be updated in place, never rebuilt from a rescan. That is why the
        LLM reports deltas and this layer owns the whole list.

        `checked` / `checkedRound` / `checkedAt` are never taken from a report,
        so a message resurfacing from a park queue cannot undo a completion.

        Nothing writes those fields today. Tick state lives only in
        todos-checked.json and consume_checked never stamps them back, so this
        is a guard for a field that does not yet exist rather than live logic.
        It stays because the invariant it protects is the expensive one, and
        because any future writer would otherwise silently lose it.
        """
        kept: dict[str, dict] = {}
        orphans: dict[str, dict] = {}
        for item in list(self.todos) + list(reported):
            key = _usable_id(item)
            if key is None:
                orphans[_content_key(item)] = dict(item)
                continue
            if key in drop:
                continue
            prior = kept.get(key, {})
            merged = _merge(prior, item)
            if prior:
                for owned in ("checked", "checkedRound", "checkedAt"):
                    if owned in prior:
                        merged[owned] = prior[owned]
                    else:
                        merged.pop(owned, None)
            merged.setdefault("createdRound", self.roundSeq)
            if "deadline" in merged:
                merged["deadline"] = resolve_deadline(merged["deadline"])
            kept[key] = merged
        self.todos = list(kept.values()) + list(orphans.values())

    def record_notices(self, pushed: list[dict], now: int,
                       incoming_todos: set[str]) -> NoticeFiling:
        """File what was just pushed as a reviewable notice.

        Returns how many were filed, plus how many the todo list already owns.

        Upserts by message id rather than appending, so mail notified again in
        a later round refreshes its text and restarts its review clock instead
        of appearing twice.

        A message that is also a todo is never filed: 重要事項 is only for
        pushed mail the todo list will never show, so a duplicate row would
        be pure noise (its 加到待辦 button would be a no-op anyway, since
        consume_promotes already skips a live duplicate). `incoming_todos` is
        needed on top of self.todos because reconcile_todos runs after this, so
        this round's reported todos are not on the list yet.

        A notice already filed is dropped, not archived, the round its message
        turns into a todo. Sweeping it would hand back a 復原 button for work the
        user can see on the list, and the todo is the surviving record regardless.

        Must run before sweep_notices in the same commit. The other order
        sweeps a notice into the archive and then re-files it here, leaving one
        message in both places at once.

        Queue bookkeeping from wherever the entry came from is dropped, so a
        promoted notice does not carry another queue's clock into the todo list.
        """
        owned = incoming_todos | {k for k in (_usable_id(t) for t in self.todos)
                                  if k is not None}
        kept: dict[str, dict] = {}
        # An id-less entry is unreachable from the GUI, which can only send ids
        # back, so it can never be promoted. It is carried through anyway, the
        # same keep-by-default rule the other queues follow, and sweep_notices
        # is what eventually disposes of it. It cannot be matched against a todo
        # either, so the todo-owns-it rule never reaches one.
        orphans = [n for n in self.notices if _usable_id(n) is None]
        dropped: set[str] = set()
        for item in list(self.notices) + list(pushed):
            key = _usable_id(item)
            if key is None:
                continue
            if key in owned:
                dropped.add(key)
                continue
            kept[key] = _merge(kept.get(key, {}), item)
        fresh = {k for k in (_usable_id(i) for i in pushed)
                 if k is not None} - owned
        for key in fresh:
            for foreign in ("firstDeferredRound", "archivedAt", "createdRound"):
                kept[key].pop(foreign, None)
            kept[key]["noticedRound"] = self.roundSeq
            kept[key]["noticedAt"] = now
        self.notices = list(kept.values()) + orphans
        # Distinct messages, not entries: the same id normally arrives from
        # both pendingNotify and this round's report.
        return NoticeFiling(len(fresh), len(dropped))

    def consume_promotes(self) -> tuple[list[dict], str]:
        """Turn notices the user promoted in the GUI into todos.

        The request arrives in a GUI-owned file for the same reason ticks and
        restores do, so state.json keeps exactly one writer.

        Must run before sweep_notices, or a notice promoted in the hours before
        this round would be archived instead of becoming a todo.
        """
        wanted, err, _ = _read_id_request(PROMOTE_PATH, "promoteIds",
                                          "promote file unreadable")
        if err:
            return [], err
        moved = [n for n in self.notices if _usable_id(n) in wanted]
        if not moved:
            return [], ""
        live = {_usable_id(t) for t in self.todos}
        added = []
        for item in moved:
            clean = {k: v for k, v in item.items()
                     if k not in ("noticedRound", "noticedAt")}
            # Skipped rather than merged when a todo for the same message is
            # already live: the notice carries a one-line summary of the push,
            # which would overwrite the action the LLM wrote for the todo.
            if _usable_id(clean) in live:
                continue
            # Set here, not left to reconcile_todos, so a promotion shows up
            # under 這次新增 in the round the user asked for it.
            clean["createdRound"] = self.roundSeq
            self.todos.append(clean)
            added.append(clean)
        # An id-less notice survives, since `wanted` only ever holds real ids.
        self.notices = [n for n in self.notices if _usable_id(n) not in wanted]
        # What actually reached the list, not everything the request matched.
        # The count goes into a push, so counting a skipped duplicate would tell
        # the user a todo was added that they will not find.
        return added, ""

    def settle_notices(self, pushed: list[dict], now: int,
                       incoming_todos: set[str]) -> dict[str, Any]:
        """Run the whole notice lifecycle for this round, in the one safe order.

        Both dependencies are real, and both were proven by execution:
        promoting after the sweep silently discards the request, and sweeping
        before filing archives a re-notified message and then re-files it,
        leaving it in two places. Grouping them here is what stops cmd_commit
        from having to remember that.

        Called before the reconcile_* pass, so a promoted notice goes through
        reconcile_todos like any reported todo and picks up its deadline
        normalisation. That is also why `incoming_todos` has to be handed in
        rather than read off self.todos -- see record_notices.
        """
        promoted, promote_err = self.consume_promotes()
        filing = self.record_notices(pushed, now, incoming_todos)
        swept, sweep_err = self.sweep_notices(now)
        return {"promoted": promoted, "promoteBlocked": promote_err,
                "newNotices": filing.filed, "skippedAsTodo": filing.dropped,
                "swept": swept, "sweepBlocked": sweep_err}

    def sweep_notices(self, now: int) -> tuple[int, str]:
        """Archive notices the user left alone. Returns how many, plus a fault.

        Taking no action is the third choice next to promoting and ignoring:
        the notice lands in the archive, where 復原 can still pull it back and
        the existing ARCHIVE_TTL purge disposes of it three days later.

        Sweeping is never urgent, so a refused archive write leaves the notices
        in place and the next round retries, rather than dropping them.
        """
        stale, keep = [], []
        for item in self.notices:
            try:
                age = self.roundSeq - int(item["noticedRound"])
            except (KeyError, TypeError, ValueError):
                # Missing or unusable: sweep rather than stick. The archive is
                # recoverable for three days, while a notice nothing can age
                # out is not recoverable from at all.
                age = NOTICE_REVIEW_ROUNDS
            (stale if age >= NOTICE_REVIEW_ROUNDS else keep).append(item)
        if not stale:
            return 0, ""
        prev, err, rev = _read_archive()
        if err:
            return 0, err
        already = {_usable_id(a) for a in prev}
        add = [dict(n, archivedAt=now) for n in stale
               if _usable_id(n) not in already]
        if add and not _write_archive(prev + add, rev):
            return 0, "archive not writable"
        self.notices = keep
        return len(stale), ""

    def orphan_count(self) -> int:
        """Queue entries with no usable id, surfaced so they cannot pile up unseen."""
        return sum(1 for q in (self.pendingNotify, self.pendingJudge,
                               self.todos, self.notices)
                   for item in q if _usable_id(item) is None)

    def consume_checked(self) -> tuple[list[dict], str]:
        """Archive todos the user ticked and drop their notices too. Returns
        the archived todos plus why none were.

        Only items already ticked when this runs are archived. Anything ticked
        during the round survives to the next one, so a box does not vanish the
        instant it is clicked.

        The reason comes back in the tuple, not on the instance, since State
        only mirrors state.json and this is round-local. "" covers both
        success and nothing-to-archive; the three faults below never clear on
        their own, so an unreported one just leaves ticked rows stuck forever.
        """
        ticked, tick_err, rev = _read_ticks()
        if tick_err:
            return [], tick_err
        # _usable_id, not str(id). Every id-less todo would otherwise share the
        # key "None", so ticking one would archive all of them and take
        # unfinished ones with it. An id-less todo simply cannot be ticked.
        done = [t for t in self.todos if _usable_id(t) in ticked]
        if not done:
            return [], ""

        # Archive first, remove second. The old order removed the todo and then
        # swallowed any archive failure, so a mis-tick could end up with no copy
        # anywhere while its source message sat behind the watermark.
        prev, prev_err, prev_rev = _read_archive()
        if prev_err:
            return [], prev_err
        already = {_usable_id(t) for t in prev}
        # Idempotent by message id. The write happens before the re-read below,
        # so a mid-flight un-tick correctly keeps the todo in the list while a
        # copy has already landed here; without this filter, a later round
        # that does stick would append the same entry again and inflate the
        # GUI's archived count. Swapping the two would also stop the
        # duplicate, but would decouple the removal from the re-read guarding
        # it, risking loss of a todo the user just un-ticked instead.
        if not _write_archive(
                prev + [dict(t, archivedAt=int(time.time()))
                        for t in done if _usable_id(t) not in already],
                prev_rev):
            return [], "archive not writable"

        # Re-read and compare before committing the removal. The user can
        # un-tick between the first read and here, and the GUI will already have
        # told them it saved. Honouring that beats archiving this round, so any
        # change defers the whole batch to the next one.
        again, again_err, again_rev = _read_ticks()
        if again_err or (again, again_rev) != (ticked, rev):
            return [], ""

        self.todos = [t for t in self.todos if _usable_id(t) not in ticked]
        # The notice goes too. consume_promotes decides "is this already a todo"
        # by looking at self.todos, which no longer holds this id, so a promote
        # click still sitting in the request file would otherwise re-add the
        # message as a fresh unticked todo. reconcile_todos happens to drop it
        # again because `ticked` is its drop set, but that only holds while
        # promotion runs first, which nothing enforces.
        self.notices = [n for n in self.notices if _usable_id(n) not in ticked]
        return done, ""


    def consume_restores(self) -> tuple[list[dict], str]:
        """Put todos the user un-archived back on the list.

        The request arrives in a GUI-owned file rather than the GUI writing
        state.json, so no file gains a second writer. Restoring also clears the
        id from notifiedIds, or the todo would be live again while the pipeline
        still treated it as settled and never re-notified it.
        """
        wanted, err, _ = _read_id_request(RESTORE_PATH, "restoreIds",
                                          "restore file unreadable")
        if err:
            return [], err
        if not wanted:
            return [], ""

        archived, err, rev = _read_archive()
        if err:
            return [], err
        back = [a for a in archived if _usable_id(a) in wanted]
        if not back:
            return [], ""

        live = {_usable_id(t) for t in self.todos}
        for item in back:
            clean = {k: v for k, v in item.items() if k != "archivedAt"}
            if _usable_id(clean) not in live:
                self.todos.append(clean)
            self.notifiedIds = [x for x in self.notifiedIds
                                if x != str(_usable_id(clean))]

        if not _write_archive([a for a in archived
                               if _usable_id(a) not in wanted], rev):
            # The todo is already back on the list. A duplicate restore next
            # round is a no-op, so leaving the archive alone is safe.
            return back, "archive not writable"
        return back, ""

    def purge_archive(self, now: int) -> int:
        """Drop archive entries past ARCHIVE_TTL. Returns how many went.

        An entry with no timestamp gets stamped instead of purged, so anything
        archived before this field existed still gets its full three days
        rather than disappearing on the first run that notices it.
        """
        archived, err, rev = _read_archive()
        if err or not archived:
            return 0
        keep, dropped, stamped = [], 0, False
        for item in archived:
            if "archivedAt" not in item:
                item["archivedAt"] = now
                stamped = True
            try:
                age = now - int(item["archivedAt"])
            except (TypeError, ValueError):
                item["archivedAt"] = now
                stamped, age = True, 0
            if age > ARCHIVE_TTL:
                dropped += 1
            else:
                keep.append(item)
        if dropped or stamped:
            if not _write_archive(keep, rev):
                # Someone else changed the archive since the read above, so this
                # keep-list is stale. Purging is never urgent; next round retries.
                return 0
        return dropped

    def split_debt(self, ticked: set[str] | None = None) -> dict[str, Any]:
        """Old debt may take at most DEBT_BODY_BUDGET reads so new mail keeps a
        slot. Items past the wait limit are surfaced, never dropped, because
        running out of budget is not evidence a message is unimportant.

        Age is counted in rounds via roundSeq, not elapsed seconds, so a paused
        app or a catch-up burst cannot distort it.

        `ticked` withholds mail whose todo the user has already ticked. begin
        hands out notifyNow before commit consumes the ticks, so without it a
        round pushes a toast for work that was finished hours ago. Only the
        push is withheld: the entry stays in pendingNotify and commit still
        clears it, so an uncommitted round loses nothing.
        """
        ticked = ticked or set()
        overdue, fresh = [], []
        for item in self.pendingJudge:
            # An id-less entry cannot be fetched or resolved by the LLM, so it
            # must not consume body-read budget or sit in a bucket forever
            # pretending to be actionable debt. orphan_count is how it surfaces.
            if _usable_id(item) is None:
                continue
            first = int(item.get("firstDeferredRound", self.roundSeq))
            bucket = overdue if self.roundSeq - first >= JUDGE_WAIT_LIMIT_ROUNDS else fresh
            bucket.append(item)
        return {
            "notifyNow": [i for i in self.pendingNotify
                          if _usable_id(i) not in ticked],
            "judgeOverdue": overdue,
            "judgeNow": fresh[:DEBT_BODY_BUDGET],
            "bodyBudgetTotal": BODY_BUDGET,
            "bodyBudgetForDebt": min(DEBT_BODY_BUDGET, len(fresh)),
        }


class Progress:
    """Per-round scratch so step and commit agree on what begin set up."""

    def __init__(self, scan_end: int, searches: int = 0, stuck: list[list[int]] | None = None,
                 token: str | None = None, config_seen: str = ""):
        self.scan_end = scan_end
        self.searches = searches
        self.stuck = stuck or []
        self.token = token or f"{scan_end}-{os.getpid()}"
        # Stores only the observation, not the alert decision; only commit
        # writes lastConfigStatus, so it re-derives the decision from both
        # rather than trusting a stale copy. Defaulting to "" reads a
        # pre-upgrade file as drift, so the alert stays owed, not cleared.
        self.config_seen = config_seen

    @classmethod
    def load(cls) -> "Progress":
        with open(PROGRESS_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return cls(int(d["scanEnd"]), int(d.get("searches", 0)),
                   [list(x) for x in d.get("stuck", [])], d.get("token"),
                   str(d.get("configSeen", "")))

    def save(self) -> None:
        """Same temp-file discipline as State.save. A torn write here used to be
        survivable because nothing read this file defensively; now that an
        unreadable round aborts, half a file would throw a round away for
        nothing."""
        _atomic_json(PROGRESS_PATH, {
            "scanEnd": self.scan_end, "searches": self.searches,
            "stuck": self.stuck, "token": self.token,
            "configSeen": self.config_seen})

    @staticmethod
    def clear() -> None:
        for path in (PROGRESS_PATH, ROUND_PATH):
            if os.path.exists(path):
                os.unlink(path)


def build_query(lo: int, hi: int) -> str:
    """The interval bounds verbatim, plus the configured sender exclusions.

    The clock-skew overlap is applied once when the tail interval is created,
    not here. Re-widening every sub-query by BOUNDARY_SLACK meant a bisected
    one-second interval still searched a fifteen-minute window, so 40 messages
    anywhere in that overlap kept it saturated and it was then mislabelled as
    "more than 40 in a single second". Bisection could never shrink the range
    actually being queried.

    Exclusions widen the result set when absent, so a broken config costs
    tokens rather than coverage. See excluded_senders.
    """
    q = f"after:{max(0, lo)} before:{hi}"
    for addr in excluded_senders():
        q += f" -from:{addr}"
    return q


class FindingsError(Exception):
    """round.json exists but could not be trusted for this round."""


def _read_findings(token: str | None = None) -> dict[str, Any]:
    """Findings for this round, or raise.

    A missing file is a valid empty result. A corrupt file, an unreadable file,
    or a file stamped with a different round's token is not, and must never be
    flattened into "this batch found nothing" -- cmd_step retires the interval
    right after, so a false empty means the watermark moves past mail that was
    never recorded anywhere.

    The token check is what stops four concurrent schedules from cross-talking
    through one shared file. Another round overwriting round.json between this
    round's write and its step would otherwise attach its findings, or nothing
    at all, to an interval this round is about to retire.
    """
    if not os.path.exists(ROUND_PATH):
        return {}
    try:
        with open(ROUND_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        raise FindingsError("unreadable round.json: " + str(exc)) from exc
    if not isinstance(data, dict):
        raise FindingsError("round.json is not an object")
    stamped = data.get("roundToken")
    if token is not None and stamped is not None and str(stamped) != token:
        raise FindingsError("round.json belongs to round " + str(stamped))
    return data


def plan_query(state: State, prog: Progress) -> dict[str, Any] | None:
    if prog.searches >= MAX_SEARCHES_PER_ROUND:
        return None
    iv = state.coverage.newest(prog.stuck)
    if iv is None:
        return None
    return {"lo": iv[0], "hi": iv[1], "max_results": MAX_RESULTS,
            "query": build_query(iv[0], iv[1])}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def abort_if_stale(fn):
    """Turn a lost update into a documented status code instead of data loss."""
    def wrapped(args) -> int:
        try:
            return fn(args)
        except StaleStateError as exc:
            emit({"status": "STATE_CHANGED_ABORT", "detail": str(exc)})
            return 2
    return wrapped


def load_or_abort(now: int) -> tuple[State | None, bool]:
    """A corrupt state file must produce the documented status code for a cold
    reader, never a traceback. All three subcommands share this contract."""
    try:
        return State.load_or_init(now)
    except StateError as exc:
        emit({"status": "ABORT_STATE_CORRUPT", "detail": str(exc)})
        return None, False


def load_progress_or_abort() -> Progress | None:
    """The round file, or None after emitting the documented status code.

    Missing is not an exotic case: another task's commit calls Progress.clear(),
    and the four scheduled tasks share this directory with no lock. Aborting is
    the only safe answer, because without the round token there is no way to
    tell whose findings round.json holds. It happens before coverage is
    touched, so the interval survives and the next round searches it again.
    """
    try:
        return Progress.load()
    except Exception as exc:
        emit({"status": "NO_ROUND_IN_PROGRESS", "detail": str(exc)})
        return None


@abort_if_stale
def cmd_begin(_args) -> int:
    now = int(time.time())
    state, first_run = load_or_abort(now)
    if state is None:
        return 2

    state.roundSeq += 1
    state.coverage.extend_to(now, BOUNDARY_SLACK)
    # begin owns "a round starts here". Leaving a dead round's findings on disk
    # would let this round's commit read them, and the only reason that is
    # currently harmless is a delicate argument about every field being
    # idempotent. Deleting it removes the argument.
    if os.path.exists(ROUND_PATH):
        os.unlink(ROUND_PATH)
    # Only the transition into a fault alerts, so a config left broken does not
    # push the same message four times a day. The status itself is reported
    # every round regardless, which is what keeps the fault visible in between.
    cfg = read_config()
    cfg["alert"] = (cfg["status"] != "present"
                    and cfg["status"] != state.lastConfigStatus)
    prog = Progress(now, config_seen=cfg["status"])
    prog.save()
    state.save()

    out = {"status": "PROCEED", "firstRun": first_run, "scanEnd": now,
           "roundSeq": state.roundSeq, "roundToken": prog.token,
           "notifiedIds": state.notifiedIds}
    # Read, never written, so the GUI keeps sole ownership of the tick file.
    # An unreadable one falls back to pushing: a duplicate toast is recoverable,
    # a notification that is never sent is not.
    ticked_now, tick_err, _ = _read_ticks()
    out.update(state.split_debt(set() if tick_err else ticked_now))
    out["config"] = cfg
    out["nextQuery"] = plan_query(state, prog)
    emit(out)
    return 0


@abort_if_stale
def cmd_step(args) -> int:
    state, _ = load_or_abort(int(time.time()))
    if state is None:
        return 2
    prog = load_progress_or_abort()
    if prog is None:
        return 2
    prog.searches += 1

    if args.failed:
        prog.save()
        emit({"status": "SEARCH_FAILED"})
        return 0

    # Merge every finding before touching coverage, so the interval and
    # everything it produced land in one atomic save. cmd_step retires intervals
    # immediately, so anything that only lived in round.json until cmd_commit
    # would be lost outright if the round died in between, with the watermark
    # already past the message. Nothing else can ever surface it again.
    #
    # "Judged important" is what gets persisted here; "notified successfully" is
    # what removes it at commit. That ordering is what makes a crash between the
    # push and the commit safe.
    try:
        f = _read_findings(prog.token)
    except FindingsError as exc:
        # Keeping the interval costs one repeated search. Retiring it on a bad
        # read costs the mail outright.
        prog.save()
        emit({"status": "FINDINGS_UNREADABLE", "detail": str(exc)})
        return 2
    state.reconcile_notify(f.get("important", []) + f.get("failedNotify", []), set())
    state.reconcile_judge(f.get("defer", []), set())
    state.reconcile_todos(f.get("todos", []), set())

    if args.count >= MAX_RESULTS:
        if not state.coverage.bisect(args.lo, args.hi):
            prog.stuck.append([args.lo, args.hi])
    else:
        state.coverage.retire(args.lo, args.hi)

    state.save()
    prog.save()
    nq = plan_query(state, prog)
    emit({"status": "SEARCH" if nq else "DONE", "nextQuery": nq,
          "stuck": prog.stuck, "backlogIntervals": len(state.coverage.intervals)})
    return 0


@abort_if_stale
def cmd_commit(_args) -> int:
    now = int(time.time())
    state, _ = load_or_abort(now)
    if state is None:
        return 2
    prog = load_progress_or_abort()
    if prog is None:
        return 2

    try:
        findings = _read_findings(prog.token)
    except FindingsError as exc:
        emit({"status": "FINDINGS_UNREADABLE", "detail": str(exc)})
        return 2

    notified = {str(x) for x in findings.get("notifiedIds", [])}
    failed = [x for x in findings.get("important", []) + findings.get("failedNotify", [])
              if str(x.get("id")) not in notified]
    # judgedIds is how the LLM says "looked at it, nothing further needed".
    # Without it, keep-by-default would strand every message judged unimportant.
    # A contradictory report (same id in both judgedIds and defer) resolves
    # toward keeping, since dropping it would be the one unrecoverable outcome.
    deferred_ids = {str(x.get("id")) for x in findings.get("defer", [])}
    judged = {str(x) for x in findings.get("judgedIds", [])} - deferred_ids
    resolved = notified | judged | {str(x.get("id")) for x in failed}

    # The shape the queues hold, for everything actually pushed this round, so
    # 重要事項 can show what the toast said. pendingNotify comes first so a fresh
    # report wins the field-level merge in record_notices.
    #
    # This has to stay above `notified |= ticked`. Once the ticks are folded in,
    # a message that was only ticked and never pushed also passes the test
    # below, and would be filed as a notice for work just finished.
    pushed = [x for x in (list(state.pendingNotify)
                          + findings.get("important", [])
                          + findings.get("failedNotify", []))
              if str(x.get("id")) in notified]

    restored, restore_blocked = state.consume_restores()
    purged = state.purge_archive(now)
    archived, archive_blocked = state.consume_checked()
    ticked = {str(t.get("id")) for t in archived}
    # After consume_checked, so a tick has already withdrawn its notice and a
    # stale promote click cannot resurrect the message.
    #
    # The filter belongs here rather than in `pushed` itself, and catches the
    # opposite case: a retry can genuinely succeed while the user ticks the same
    # message between begin and commit, so the push is real and `pushed` rightly
    # holds it. Filing a notice anyway archives the work and hands back a button
    # that resurrects it.
    #
    # `ticked` is deliberately not subtracted from reported_todos:
    # reconcile_todos already drops a ticked id from the todo list, and
    # `pushed` above is already filtered by it, so a message in both sets
    # files no notice either way.
    reported_todos = {k for k in (_usable_id(t) for t in findings.get("todos", []))
                      if k is not None}
    notice = state.settle_notices(
        [p for p in pushed if str(p.get("id")) not in ticked], now, reported_todos)
    # A tick is the authoritative end of that message. Without clearing the park
    # queues too, the next round would re-report it as debt and resurrect a todo
    # the user already completed.
    notified |= ticked
    resolved |= ticked

    state.notifiedIds = (state.notifiedIds + sorted(notified))[-NOTIFIED_KEEP:]
    state.reconcile_notify(failed, notified)
    state.reconcile_judge(findings.get("defer", []), resolved)
    state.reconcile_todos(findings.get("todos", []), ticked)

    announce = (bool(state.coverage.intervals) and
                state.roundSeq - state.lastBacklogNoticeRound >= BACKLOG_NOTICE_EVERY)
    if announce:
        state.lastBacklogNoticeRound = state.roundSeq

    # Advancing lastConfigStatus silences the alert next round, following the
    # mail queues' keep-by-default rule: an unconfirmed push keeps the alert
    # owed, since committing anyway would otherwise silence the fault for good
    # until it happened to become a different one.
    #
    # `healed` is unconditional: a resolved fault has nothing left to announce.
    # `drifted` guards against config.json changing between begin and commit,
    # so a fault begin never saw can't consume an alert nobody was shown.
    new_todos = sum(1 for t in state.todos
                    if int(t.get("createdRound", 0)) == state.roundSeq)
    config_status = read_config()["status"]
    owed = (prog.config_seen != "present"
            and prog.config_seen != state.lastConfigStatus)
    healed = config_status == "present"
    drifted = config_status != prog.config_seen
    acked = not owed or bool(findings.get("configAlerted"))
    if healed or (acked and not drifted):
        state.lastConfigStatus = config_status

    state.save()
    Progress.clear()
    emit({"status": "COMMITTED",
          "coveredThrough": state.coverage.covered_through,
          "backlogIntervals": len(state.coverage.intervals),
          "stuck": prog.stuck,
          "shouldAnnounceBacklog": announce,
          "configStatus": config_status,
          "configAlertStillOwed": state.lastConfigStatus != config_status,
          "pendingNotify": len(state.pendingNotify),
          "pendingJudge": len(state.pendingJudge),
          "todos": len(state.todos),
          "newTodosThisRound": new_todos,
          # Both halves are decided here rather than left to the round's own
          # arithmetic, so a cold reader cannot get the quiet-hours window
          # wrong and pop a browser at 06:00.
          "shouldOpenTodoList": (new_todos > 0 or notice["newNotices"] > 0)
          and within_gui_hours(time.localtime(now).tm_hour, GUI_OPEN_HOURS),
          "notices": len(state.notices),
          "newNoticesThisRound": notice["newNotices"],
          "noticesSkippedAsTodo": notice["skippedAsTodo"],
          "promotedThisRound": len(notice["promoted"]),
          "promoteBlocked": notice["promoteBlocked"],
          "sweptToArchive": notice["swept"],
          "sweepBlocked": notice["sweepBlocked"],
          "archivedThisRound": len(archived),
          "restoredThisRound": len(restored),
          "restoreBlocked": restore_blocked,
          "purgedFromArchive": purged,
          "archiveBlocked": archive_blocked,
          "itemsMissingId": state.orphan_count()})
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Gmail check state machine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("begin").set_defaults(func=cmd_begin)

    sp = sub.add_parser("step")
    sp.add_argument("--lo", type=int, default=0)
    sp.add_argument("--hi", type=int, default=0)
    sp.add_argument("--count", type=int, default=0)
    sp.add_argument("--failed", action="store_true")
    sp.set_defaults(func=cmd_step)

    sub.add_parser("commit").set_defaults(func=cmd_commit)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
