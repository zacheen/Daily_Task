"""On-demand todo list viewer for the Gmail check tasks.

Launch it, tick what you finished, close the tab. The process exits on its own
once no page is open, which is what lets an unattended scheduled run start it
without leaving a resident server nobody will ever close.

Liveness is tracked per page, not per connection, so a second tab is not
killed when the first one closes. A closing page's beacon drops its id
immediately; the ping timeout is only a backstop for closes that send no
beacon at all (crash, sleep, a discarded background tab).

Writer discipline is the whole design. This process writes the four request
files, its own link cache and link log, and nothing else, and only ever reads
`state.json` and the archive. statemachine.py is the reverse. With one writer
per file plus atomic replace, a reader always sees a complete old or complete
new file and no lock is needed.

Each request file is one verb the user can aim at a row. A tick says done, and
待分類's and 追蹤中's 封存 are the same tick. A restore says un-archive that. A
triage says file this todo under a level, or send it back to 待分類. A follow
says move a filed todo to 追蹤中, or back to 待辦清單 at the level it left
with. None of them takes effect until the next scheduled round reads it, which
is why every button says 已排定 rather than claiming it is finished. A queued
level or follow shows at once anyway, since the row has to move to the section
it now belongs in.

A tick is persisted on the request that carries it, not on window close, so
killing the process cannot lose one. The page reports per-row save state
because a stale tab against a stopped server otherwise looks identical to a
successful save.

A subject links to the original mail in the mailbox that first received it,
so a reply goes out from that address. The link is looked up over IMAP by
gmail_mcp in a background thread, never by the scheduled LLM, and kept in the
link cache, so each todo is looked up once rather than on every launch.

Run with
    conda run -n ML python "D:\\dont_move\\git_save\\Daily_Task\\Email_Check\\task_list\\task_list_gui.py"
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import socket
import tempfile
import threading
import time
import webbrowser
from urllib.parse import unquote

from flask import Flask, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
# state.json lives one level up because statemachine.py owns it and the rest of
# the Email_Check task reads it too. Only the six files below are this
# folder's, which is also where _atomic_write puts its temp file.
STATE_PATH = os.path.join(os.path.dirname(HERE), "state.json")
CHECKED_PATH = os.path.join(HERE, "tasks-checked.json")
ARCHIVE_PATH = os.path.join(HERE, "tasks-archive.json")
RESTORE_PATH = os.path.join(HERE, "tasks-restore.json")
TRIAGE_PATH = os.path.join(HERE, "tasks-triage.json")
FOLLOW_PATH = os.path.join(HERE, "tasks-follow.json")
# Todo id to link, or null where a lookup found that no link can be built.
# Only this process reads it. Its links carry the origin mailbox's address,
# which is one more reason the folder's *.json stays out of git.
LINKS_PATH = os.path.join(HERE, "mail-links.json")
# Bump when origin_links would answer differently for an id it already
# answered (a new URL form or new None rules). A file of another version is
# ignored, including one an older open copy of this page rewrites.
LINKS_VERSION = 3
# Why a todo got no link: failed lookups and origins without credentials.
# The scheduled run starts this page under pythonw, so stdout reaches nobody
# and this file is the only place those reasons land. Trimmed to the last
# LINK_LOG_LINES lines on every write.
LINK_LOG_PATH = os.path.join(HERE, "mail-links.log")
LINK_LOG_LINES = 200
# Mirrors PRIORITIES in statemachine.py, most urgent first, which is the sort
# order of 待辦清單. A todo with none of these is 待分類.
PRIORITIES = ("urgent", "important", "normal")
# Mirrors ARCHIVE_TTL in statemachine.py, for showing days remaining.
ARCHIVE_TTL_DAYS = 3
# A 追蹤中 row this many days old turns red, since nobody has answered yet.
FOLLOW_ALERT_DAYS = 3
# Upper bound on how far 延後提醒 can push a red row out.
MAX_REMIND_DAYS = 60
HOST, PORT = "127.0.0.1", 8765
# Long enough for a cold browser start on a busy machine. Exceeded with no page
# ever connecting means the browser never came up, so exit rather than idle.
STARTUP_GRACE = 90
# Must clear a minute: Chrome throttles hidden-tab timers to about once a
# minute, so killing the server under a merely-backgrounded tab would look
# like the page broke.
CLIENT_TIMEOUT = 150
PING_EVERY_MS = 15000
WATCHDOG_TICK = 3
# Months a bare MM-DD may lag the current month before it is treated as next
# year rather than as recently overdue.
BARE_LOOKBACK = 3
# gmail_mcp is a sibling folder outside this repo. It is loaded on the first
# lookup, so the page and its tests run without it.
GMAIL_MCP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
                         "gmail_mcp", "server.py")
# A failed lookup (IMAP unreachable, a revoked app password) is retried once
# after this many seconds. If the retry fails too, the todos it covered get no
# link from this process, because the user would rather find the mail by hand
# than wait.
LINK_RETRY_DELAY = 5
# Every link origin_links builds starts with one of these. A cached value that
# does not is dropped, since the page renders it as an href.
GMAIL_WEB = "https://mail.google.com/"
# Mirrors OUTLOOK_SEARCH in gmail_mcp/server.py. Outlook on the web reads no
# search from its URL, so the row copies the AQS query after this prefix and
# opens the bare mailbox for pasting.
OUTLOOK_SEARCH = "https://outlook.office.com/mail/#q="
OUTLOOK_WEB = "https://outlook.office.com/mail/"
# How soon the page polls again while a lookup runs. One lookup over 7 todos
# took 10.5 s, mostly two IMAP logins and about 0.5 s per message.
LINK_POLL_MS = 3000

app = Flask(__name__)
_lock = threading.Lock()
_started = time.time()
_clients: dict[str, float] = {}
_seen_any = False
# The link cache in memory, None until the first poll loads LINKS_PATH.
_links: dict[str, str | None] | None = None
# Ids whose lookup failed twice. Never saved, so the next launch tries again.
_given_up: set[str] = set()
# Taken after _lock whenever both are held, never before it.
_link_lock = threading.Lock()
_link_job: threading.Thread | None = None
_gmail = None


def live_clients(now: float, clients: dict[str, float]) -> dict[str, float]:
    return {cid: t for cid, t in clients.items() if now - t <= CLIENT_TIMEOUT}


def exit_reason(now: float, started: float, clients: dict[str, float],
                seen_any: bool) -> str:
    """Why the process should stop, or "" to keep serving.

    Before the first page arrives the only exit is the startup grace running
    out. Treating "no clients yet" as "every page closed" would kill the server
    in the second before the browser finished launching.
    """
    if not seen_any:
        return "" if now - started <= STARTUP_GRACE else "browser never connected"
    return "" if live_clients(now, clients) else "page closed"


def _touch(cid: str) -> None:
    global _seen_any
    if not cid:
        return
    with _lock:
        _seen_any = True
        _clients[cid] = time.time()


def _watchdog() -> None:
    """Exit once no page is open.

    os._exit is deliberate: every tick is already fsynced by the request that
    carried it, so there is nothing to flush, and Flask's development server
    has no shutdown hook callable from outside a request.
    """
    while True:
        time.sleep(WATCHDOG_TICK)
        now = time.time()
        with _lock:
            for cid in [c for c, t in _clients.items() if now - t > CLIENT_TIMEOUT]:
                del _clients[cid]
            why = exit_reason(now, _started, _clients, _seen_any)
            if why:
                # Exiting while holding the lock is deliberate: every
                # _atomic_write runs under it, so releasing first risks
                # interrupting a request between mkstemp and os.replace,
                # leaving a .gui.*.tmp nothing cleans up. os.replace can't
                # be interrupted, so committed ticks were never at risk.
                print("exiting: " + why)
                os._exit(0)


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _atomic_write(path: str, payload) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _atomic_write_text(path: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".gui.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_todos() -> list[dict]:
    return _read_json(STATE_PATH, {}).get("todos", [])


def _next_rev() -> str:
    """Monotonic-enough revision marker for the tick file."""
    return f"{time.time_ns()}"


def load_checked() -> set[str]:
    return {str(x) for x in _read_json(CHECKED_PATH, {}).get("checkedIds", [])}


def load_triage() -> dict[str, str]:
    levels = _read_json(TRIAGE_PATH, {}).get("levels", {})
    return {str(k): str(v) for k, v in levels.items()} if isinstance(levels, dict) else {}


def _is_remind_at(value) -> bool:
    """Mirror of the state machine's check. True is an int in Python, so bool
    has to be ruled out by name."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_follow() -> dict[str, bool | int]:
    """True, false, or the epoch a postponed reminder is due, per todo id."""
    wanted = _read_json(FOLLOW_PATH, {}).get("follow", {})
    if not isinstance(wanted, dict):
        return {}
    return {str(k): v for k, v in wanted.items()
            if isinstance(v, bool) or _is_remind_at(v)}


def _follow_applied(todo: dict, want) -> bool:
    if want is False:
        return "followSince" not in todo
    if want is True:
        return "followSince" in todo
    return "followSince" in todo and todo.get("followRemindAt") == want


def prune_requests(todos: list[dict]) -> tuple[set[str], dict[str, str], dict[str, bool | int]]:
    """Drop requests that are done or aimed at nothing. Returns the ticks,
    levels and follows left.

    All four request files need this: each would otherwise grow forever, and a
    message id reused by a future todo or archive entry would arrive already
    ticked, already flagged for restore, already filed or already followed.

    A level or follow is dropped once state.json carries it, which is how the
    page stops showing it as 已排定. Nothing is dropped before that, so a click
    the round has not read yet cannot be lost here.
    """
    rows = [t for t in todos if isinstance(t, dict) and _usable_id(t)]
    live_ids = {_usable_id(t) for t in rows}
    stored = {_usable_id(t): str(t.get("priority") or "") for t in rows}
    by_id = {_usable_id(t): t for t in rows}

    archived_ids = {_usable_id(a) for a in _read_json(ARCHIVE_PATH, {}).get("archived", [])
                    if isinstance(a, dict)}
    wanted = load_restores()
    live_restores = wanted & archived_ids
    if live_restores != wanted:
        _atomic_write(RESTORE_PATH,
                      {"restoreIds": sorted(live_restores), "rev": _next_rev()})

    levels = load_triage()
    live_levels = {tid: lvl for tid, lvl in levels.items()
                   if tid in live_ids and stored[tid] != lvl}
    if live_levels != levels:
        _atomic_write(TRIAGE_PATH, {"levels": live_levels, "rev": _next_rev()})

    follows = load_follow()
    live_follows = {tid: want for tid, want in follows.items()
                    if tid in live_ids and not _follow_applied(by_id[tid], want)}
    if live_follows != follows:
        _atomic_write(FOLLOW_PATH, {"follow": live_follows, "rev": _next_rev()})

    checked = load_checked()
    pruned = checked & live_ids
    if pruned != checked:
        _atomic_write(CHECKED_PATH, {"checkedIds": sorted(pruned), "rev": _next_rev()})
    return pruned, live_levels, live_follows


def _usable_id(item: dict) -> str | None:
    """Mirror of the state machine's id policy.

    Id-less todos must not share the key "None" here either. In the UI that
    would give every one of them a single shared tick state, and ticking one
    would archive all of them, unfinished ones included.
    """
    raw = item.get("id")
    if raw is None:
        return None
    key = str(raw).strip()
    return key if key and key.lower() not in ("none", "null") else None


def _deadline_key(text: str, today: dt.date | None = None) -> tuple:
    """Sortable (year, month, day) from the free-form deadline the LLM wrote.

    Accepts M-D, MM-DD, M/D and YYYY-MM-DD. INSTRUCTIONS asks for a full date,
    so the bare forms are a fallback.

    A bare month and day is genuinely ambiguous: on 2026-09-08, `08-30` is
    either nine days overdue or eleven months away. It is read as the recent
    past, because burying an overdue item at the bottom of the list is the
    failure this list exists to prevent, while showing a far-future item too
    early merely looks odd. Only months more than BARE_LOOKBACK behind wrap to
    next year, which still keeps `01-05` sorting after `12-30`.

    Anything unparseable sorts last rather than raising, because a malformed
    deadline must not hide a row.
    """
    today = today or dt.date.today()
    parts = [p for p in text.replace("/", "-").split("-") if p.strip().isdigit()]
    nums = [int(p) for p in parts]
    if len(nums) >= 3:
        return (nums[0], nums[1], nums[2])
    if len(nums) == 2:
        month, day = nums[0], nums[1]
        behind = today.month - month
        year = today.year + 1 if behind > BARE_LOOKBACK else today.year
        return (year, month, day)
    return (9999, 99, 99)


@app.get("/")
def index() -> str:
    return (PAGE.replace("__PING_MS__", str(PING_EVERY_MS))
                .replace("__FOLLOW_DAYS__", str(FOLLOW_ALERT_DAYS))
                .replace("__MAX_REMIND__", str(MAX_REMIND_DAYS))
                .replace("__LINK_POLL_MS__", str(LINK_POLL_MS)))


def load_restores() -> set[str]:
    return {str(x) for x in _read_json(RESTORE_PATH, {}).get("restoreIds", [])}


@app.get("/api/archive")
def api_archive():
    """Archived todos, soonest-to-purge first, with days left before the purge."""
    items = _read_json(ARCHIVE_PATH, {}).get("archived", [])
    if not isinstance(items, list):
        return jsonify({"archived": [], "error": "archive unreadable"})
    pending = load_restores()
    now = time.time()
    rows = []
    for a in items:
        if not isinstance(a, dict):
            continue
        aid = _usable_id(a)
        stamp = a.get("archivedAt")
        try:
            left = ARCHIVE_TTL_DAYS - int((now - int(stamp)) // 86400)
        except (TypeError, ValueError):
            # No usable stamp yet. The state machine stamps it on its next run,
            # so report the full window rather than implying it is about to go.
            left = ARCHIVE_TTL_DAYS
        rows.append({
            "id": aid or "",
            "restorable": aid is not None,
            "subject": a.get("subject") or "(no subject)",
            "sender": a.get("from") or "",
            "mailbox": a.get("mailbox") or "",
            "received": a.get("received") or "",
            "action": a.get("action") or "",
            "deadline": a.get("deadline") or "",
            "daysLeft": max(0, left),
            "pending": aid is not None and aid in pending,
        })
    rows.sort(key=lambda r: r["daysLeft"])
    return jsonify({"archived": rows})


@app.post("/api/restore")
def api_restore():
    """Queue a restore. The state machine performs it on its next commit.

    Writing state.json from here would give that file two writers, so the
    request is persisted and picked up, exactly like a tick.
    """
    body = request.get_json(silent=True) or {}
    tid = str(body.get("id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    with _lock:
        items = _read_json(ARCHIVE_PATH, {}).get("archived", [])
        if not isinstance(items, list):
            return jsonify({"ok": False, "error": "archive unreadable"}), 409
        if tid not in {_usable_id(a) for a in items if isinstance(a, dict)}:
            # Already restored or already purged; either way there is nothing
            # to queue and saying so beats leaving a request that never lands.
            return jsonify({"ok": False, "error": "gone", "gone": True}), 409
        wanted = load_restores()
        wanted.add(tid)
        _atomic_write(RESTORE_PATH, {"restoreIds": sorted(wanted), "rev": _next_rev()})
    return jsonify({"ok": True, "id": tid, "pending": True})


@app.post("/api/triage")
def api_triage():
    """Queue a level for a todo, or "" to send it back to 待分類.

    Same shape as /api/restore, a GUI-owned request file rather than a write to
    state.json, so that file keeps exactly one writer. The state machine
    applies it on its next commit.
    """
    body = request.get_json(silent=True) or {}
    tid = str(body.get("id", "")).strip()
    level = str(body.get("level", "")).strip()
    if not tid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    if level and level not in PRIORITIES:
        return jsonify({"ok": False, "error": "unknown level"}), 400
    with _lock:
        live = {k for k in (_usable_id(t) for t in load_todos()) if k}
        if tid not in live:
            # Archived by a round that ran while this tab was open. Saying so
            # beats queueing a request nothing will consume.
            return jsonify({"ok": False, "error": "gone", "gone": True}), 409
        levels = load_triage()
        levels[tid] = level
        _atomic_write(TRIAGE_PATH, {"levels": levels, "rev": _next_rev()})
        # A queued follow the round will now refuse for want of a level would
        # never prune, and would fire unasked once the todo is filed again.
        follows = load_follow()
        if not level and follows.pop(tid, None) is not None:
            _atomic_write(FOLLOW_PATH, {"follow": follows, "rev": _next_rev()})
    return jsonify({"ok": True, "id": tid, "level": level, "pending": True})


@app.post("/api/follow")
def api_follow():
    """Queue a move to 追蹤中 (true) or back to 待辦清單 (false).

    Its own request file, like /api/triage, so state.json keeps one writer. A
    todo with no level, stored or queued, is refused, because the state
    machine refuses it too and the click would otherwise sit 已排定 forever.
    """
    body = request.get_json(silent=True) or {}
    tid = str(body.get("id", "")).strip()
    want = body.get("follow")
    if not tid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    if not isinstance(want, bool):
        return jsonify({"ok": False, "error": "follow must be true or false"}), 400
    with _lock:
        todos = {_usable_id(t): t for t in load_todos() if isinstance(t, dict)}
        todos.pop(None, None)
        if tid not in todos:
            return jsonify({"ok": False, "error": "gone", "gone": True}), 409
        levels = load_triage()
        level = levels[tid] if tid in levels else str(todos[tid].get("priority") or "")
        if want and level not in PRIORITIES:
            return jsonify({"ok": False, "error": "not filed"}), 409
        follows = load_follow()
        follows[tid] = want
        _atomic_write(FOLLOW_PATH, {"follow": follows, "rev": _next_rev()})
    return jsonify({"ok": True, "id": tid, "follow": want, "pending": True})


@app.post("/api/remind")
def api_remind():
    """Postpone a 追蹤中 row's reminder by a number of days from now.

    Queued in the follow file as the due epoch, computed here at the click so
    the wait for the next round does not stretch the delay the user typed.
    """
    body = request.get_json(silent=True) or {}
    tid = str(body.get("id", "")).strip()
    days = body.get("days")
    if not tid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    if isinstance(days, bool) or not isinstance(days, int) \
            or not 1 <= days <= MAX_REMIND_DAYS:
        return jsonify({"ok": False, "error": f"days must be 1 to {MAX_REMIND_DAYS}"}), 400
    with _lock:
        todos = {_usable_id(t): t for t in load_todos() if isinstance(t, dict)}
        todos.pop(None, None)
        if tid not in todos:
            return jsonify({"ok": False, "error": "gone", "gone": True}), 409
        follows = load_follow()
        queued = follows.get(tid)
        if queued is False or (queued is None and "followSince" not in todos[tid]):
            return jsonify({"ok": False, "error": "not following"}), 409
        follows[tid] = int(time.time()) + days * 86400
        _atomic_write(FOLLOW_PATH, {"follow": follows, "rev": _next_rev()})
    return jsonify({"ok": True, "id": tid, "remindAt": follows[tid], "pending": True})


def _remind_at(todo: dict) -> int | None:
    """When a stamped 追蹤中 row turns red, or None when its stamp is unusable,
    which keeps a bad value from painting a row red."""
    if _is_remind_at(todo.get("followRemindAt")):
        return todo["followRemindAt"]
    try:
        return int(todo["followSince"]) + FOLLOW_ALERT_DAYS * 86400
    except (KeyError, TypeError, ValueError):
        return None


def _follow_days(stamp, now: float) -> int:
    """Whole days since followSince. An unusable stamp reads as fresh rather
    than as overdue, so a bad value cannot paint a row red."""
    try:
        return max(0, int((now - int(stamp)) // 86400))
    except (TypeError, ValueError):
        return 0


def _lookup_links(ids: list[str]) -> dict[str, str | None]:
    """gmail_mcp's origin_links, loaded on first use. Tests replace this."""
    global _gmail
    if _gmail is None:
        spec = importlib.util.spec_from_file_location("gmail_mcp_server", GMAIL_MCP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _gmail = module
    return _gmail.origin_links(ids, warn=_log_link)


def _log_link(message: str) -> None:
    """Append one timestamped line to LINK_LOG_PATH, keeping the last
    LINK_LOG_LINES. A failed write is dropped, since a log must not break the
    lookup it reports on."""
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + message
    with _lock:
        try:
            with open(LINK_LOG_PATH, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        try:
            _atomic_write_text(LINK_LOG_PATH,
                               "\n".join((lines + [line])[-LINK_LOG_LINES:]) + "\n")
        except OSError:
            pass


def _redact(text: str) -> str:
    """Mask addresses in an exception message. An IMAP error can quote the
    login it failed on, and the hub's address must not reach any file."""
    return re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "<address>", text)


def _load_links() -> dict[str, str | None]:
    data = _read_json(LINKS_PATH, {})
    if not isinstance(data, dict) or data.get("version") != LINKS_VERSION:
        return {}
    stored = data.get("links")
    if not isinstance(stored, dict):
        return {}
    return {str(k): v for k, v in stored.items()
            if v is None or (isinstance(v, str)
                             and v.startswith((GMAIL_WEB, OUTLOOK_SEARCH)))}


def _save_links() -> None:
    """Holds _lock so the watchdog cannot exit mid-write, and takes the
    snapshot inside it so two saves cannot land out of order. A failed write
    only costs a lookup next launch."""
    with _lock:
        with _link_lock:
            snapshot = dict(_links)
        try:
            _atomic_write(LINKS_PATH, {"version": LINKS_VERSION, "links": snapshot})
        except OSError as exc:
            print("mail links not saved: " + type(exc).__name__)


def _resolve_links(ids: list[str]) -> None:
    found = None
    for attempt, delay in enumerate((0, LINK_RETRY_DELAY), 1):
        time.sleep(delay)
        try:
            found = _lookup_links(ids)
            break
        except Exception as exc:
            _log_link(f"lookup of {len(ids)} todo(s) failed, attempt {attempt} of 2: "
                      f"{type(exc).__name__}: {_redact(str(exc))}")
    if found is None:
        _log_link("gave up, no link while this page stays open for: " + ", ".join(ids))
    with _link_lock:
        if found is None:
            _given_up.update(ids)
            return
        # Every id asked about gets an entry, None included, so an id the
        # lookup skipped is not looked up again.
        _links.update({i: found.get(i) for i in ids})
    _save_links()


def links_for(ids: list[str]) -> tuple[dict[str, str], bool]:
    """The links known for these ids, and whether a lookup is still running.

    Ids never looked up start one in the background, because a lookup takes
    seconds and the list must not wait on it. Entries for todos that left the
    list are dropped, which keeps the cache from growing forever.
    """
    global _link_job, _links
    live = set(ids)
    with _link_lock:
        if _links is None:
            _links = _load_links()
        stale = [i for i in _links if i not in live]
        for i in stale:
            del _links[i]
        missing = [i for i in ids if i not in _links and i not in _given_up]
        running = _link_job is not None and _link_job.is_alive()
        if missing and not running:
            _link_job = threading.Thread(target=_resolve_links, args=(missing,), daemon=True)
            _link_job.start()
            running = True
        known = {i: _links[i] for i in ids if _links.get(i)}
    # After releasing _link_lock, because _save_links takes _lock first.
    if stale:
        _save_links()
    return known, running


def _link_fields(link: str) -> dict[str, str]:
    """The row's link, with an Outlook search split into the mailbox to open
    and the query to copy."""
    if link.startswith(OUTLOOK_SEARCH):
        return {"link": OUTLOOK_WEB, "outlookQuery": unquote(link[len(OUTLOOK_SEARCH):])}
    return {"link": link, "outlookQuery": ""}


@app.get("/api/todos")
def api_todos():
    _touch(request.args.get("cid", ""))
    with _lock:
        state = _read_json(STATE_PATH, {})
        todos = [t for t in state.get("todos", []) if isinstance(t, dict)]
        checked, levels, follows = prune_requests(todos)
    links, linking = links_for([tid for tid in map(_usable_id, todos) if tid])
    now = time.time()
    rows = []
    for t in todos:
        tid = _usable_id(t)
        stored = str(t.get("priority") or "")
        # A queued level shows at once, so a filed row leaves 待分類 on the
        # click instead of on the next round.
        level = levels[tid] if tid in levels else stored
        stamped = "followSince" in t
        want = follows.get(tid) if tid is not None else None
        # Same for a queued follow. The level check mirrors consume_follow,
        # which refuses a todo with none, so no row lands in 追蹤中 unfiled.
        following = ((want is not False) if want is not None else stamped) \
            and level in PRIORITIES
        # A queued follow has no stamp yet, so it shows no age until the round.
        days = _follow_days(t.get("followSince"), now) if following and stamped else 0
        # A queued postponement counts at once, so the row stops being red on
        # the click rather than on the next round.
        due = want if _is_remind_at(want) else (_remind_at(t) if stamped else None)
        alert = bool(following and stamped and due is not None and now >= due)
        postponed = _is_remind_at(want) or _is_remind_at(t.get("followRemindAt"))
        remind_in = -int((now - due) // 86400) if following and postponed \
            and due is not None and due > now else 0
        rows.append({
            "id": tid or "",
            "tickable": tid is not None,
            # str(), because this is a sort key below and a non-string subject
            # would raise TypeError against a string one. statemachine refuses
            # such a todo at ingestion; this covers a hand-edited state.json.
            "subject": str(t.get("subject") or "(no subject)"),
            **_link_fields(links.get(tid or "", "")),
            "sender": t.get("from") or "",
            "mailbox": t.get("mailbox") or "",
            "received": t.get("received") or "",
            "action": t.get("action") or "",
            "deadline": t.get("deadline") or "",
            "uncertain": bool(t.get("uncertain")),
            "checked": tid is not None and tid in checked,
            "priority": level if level in PRIORITIES else "",
            "pendingLevel": tid is not None and tid in levels,
            "following": following,
            "pendingFollow": isinstance(want, bool),
            "pendingRemind": _is_remind_at(want),
            "followDays": days,
            "followAlert": alert,
            "remindIn": remind_in,
        })
    # Level first, then a parsed date rather than the raw string, because
    # lexicographic order puts "10-2" before "9-15" and shows a later deadline
    # as the more urgent one. 待分類 rows all rank last and the page splits them
    # off, so their order is the date order alone.
    rank = {p: i for i, p in enumerate(PRIORITIES)}
    rows.sort(key=lambda r: (r["checked"], rank.get(r["priority"], len(PRIORITIES)),
                             not r["deadline"], _deadline_key(r["deadline"]),
                             r["subject"]))
    archived = len(_read_json(ARCHIVE_PATH, {}).get("archived", []))
    return jsonify({"todos": rows, "archivedTotal": archived,
                    "roundSeq": int(state.get("roundSeq", 0)),
                    "linksPending": linking})


@app.post("/api/ping")
def api_ping():
    _touch(request.args.get("cid", ""))
    return jsonify({"ok": True})


@app.post("/api/bye")
def api_bye():
    """Final beacon from a closing page. Drops only that page's id, so a second
    tab still open keeps the server alive."""
    with _lock:
        _clients.pop(request.args.get("cid", ""), None)
    return jsonify({"ok": True})


@app.post("/api/check")
def api_check():
    body = request.get_json(silent=True) or {}
    tid = str(body.get("id", ""))
    want = bool(body.get("checked"))
    if not tid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    with _lock:
        live = {k for k in (_usable_id(t) for t in load_todos()) if k}
        if tid not in live:
            # Already archived by a run that happened while this tab was open.
            return jsonify({"ok": False, "error": "stale", "gone": True}), 409
        checked = load_checked() & live
        checked.add(tid) if want else checked.discard(tid)
        # rev changes on every write so the state machine can tell that a tick
        # moved between its read and its save, and defer archiving rather than
        # removing a todo the user just un-ticked.
        _atomic_write(CHECKED_PATH, {"checkedIds": sorted(checked), "rev": _next_rev()})
    return jsonify({"ok": True, "id": tid, "checked": want})


PAGE = """<!doctype html>
<meta charset="utf-8"><title>Gmail 待辦</title>
<style>
 :root{color-scheme:dark}
 body{font:15px/1.55 -apple-system,"Segoe UI",system-ui,sans-serif;
      background:#14161a;color:#e8eaed;margin:0;padding:28px 20px;}
 .wrap{max-width:860px;margin:0 auto}
 h1{font-size:19px;margin:0 0 4px;font-weight:600}
 .sub{color:#9aa0a6;font-size:13px;margin-bottom:22px}
 .row{display:flex;gap:13px;padding:13px 15px;border:1px solid #2a2e35;
      border-radius:10px;margin-bottom:9px;background:#1b1e23;align-items:flex-start}
 .row.done{opacity:.42}
 .row.done .subj{text-decoration:line-through}
 input[type=checkbox]{width:19px;height:19px;margin:2px 0 0;cursor:pointer;flex:none}
 .body{flex:1;min-width:0}
 .subj{font-weight:600;margin-bottom:3px;overflow-wrap:anywhere}
 .subj a{color:inherit;text-decoration:none;border-bottom:1px dotted #6a7480}
 .subj a:hover{color:#9cc3ff;border-bottom-color:#9cc3ff}
 .act{color:#c9cdd3;font-size:14px;overflow-wrap:anywhere}
 .meta{color:#8b9096;font-size:12px;margin-top:5px}
 .due{color:#ffb26b;font-weight:600}
 .box{color:#9fb8cc}
 .flag{color:#7fb2ff;font-weight:600}
 .st{font-size:11px;min-width:52px;text-align:right;padding-top:3px;flex:none}
 .saving{color:#8b9096}.saved{color:#6bd68a}.failed{color:#ff7a7a;font-weight:600}
 .sec{margin-bottom:26px}
 .sec.hidden{display:none}
 /* Bare .hidden too: the archive list carries only this class, so a
    .sec-qualified rule would never fire and the toggle would do
    nothing while looking like it worked. */
 .hidden{display:none}
 .hd{font-size:12px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;
     color:#8b9096;padding-bottom:9px;margin-bottom:11px;border-bottom:1px solid #262a31}
 #secnew .hd{color:#7fb2ff}
 #secarch .hd{color:#8b9096}
 .tog{float:right;font-weight:400;letter-spacing:0;text-transform:none;
      color:#7fb2ff;cursor:pointer;font-size:12px}
 .row.arch{opacity:.6}
 .acts{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;flex:none;
       max-width:300px}
 .lv{font-size:11px;font-weight:600;border-radius:5px;padding:1px 7px;margin-right:8px}
 .lv.urgent{background:#4a1f22;color:#ff8b8b}
 .lv.important{background:#43351a;color:#ffc46b}
 .lv.normal{background:#262a31;color:#aab0b7}
 .pend{color:#8b9096;font-size:11px;padding-top:6px}
 .age{color:#8b9096}
 .row.stale{border-color:#8a3035;background:#2a1b1e}
 .age.stale{color:#ff7a7a;font-weight:600}
 #secfollow .hd{color:#c9a0ff}
 .days{width:46px;background:#23272e;border:1px solid #383d45;color:#e8eaed;
       border-radius:7px;padding:4px 6px;font-size:12px}
 .btn.urgent{border-color:#6b2c30;color:#ff9a9a}
 .btn.important{border-color:#6b5226;color:#ffcf85}
 .btn{background:#23272e;border:1px solid #383d45;color:#cfd4da;border-radius:7px;
      padding:5px 11px;font-size:12px;cursor:pointer;flex:none}
 .btn:hover{background:#2b3038;color:#fff}
 .btn:disabled{opacity:.45;cursor:default}
 .left{color:#8b9096;font-size:11px;white-space:nowrap;padding-top:6px;flex:none}
 .soon{color:#ffb26b;font-weight:600}
 .n{font-weight:400;letter-spacing:0;text-transform:none;color:#6a6f76}
 .none{color:#6a6f76;font-size:13px;padding:2px 0 6px}
 .empty{color:#9aa0a6;text-align:center;padding:44px 0}
 .foot{color:#8b9096;font-size:12px;margin-top:20px;text-align:center}
</style>
<div class=wrap>
  <h1>Gmail 待辦</h1>
  <div class=sub id=sub>載入中</div>
  <div class=sec id=secnew>
    <div class=hd>待分類 <span class=n id=cntnew></span></div>
    <div id=listnew></div>
  </div>
  <div class=sec id=secold>
    <div class=hd>待辦清單 <span class=n id=cntold></span></div>
    <div id=listold></div>
  </div>
  <div class=sec id=secfollow>
    <div class=hd>追蹤中 <span class=n id=cntfollow></span></div>
    <div id=listfollow></div>
  </div>
  <div class=sec id=secarch>
    <div class=hd>已封存 <span class=n id=cntarch></span>
      <span class=tog id=togarch>展開</span></div>
    <div id=listarch class=hidden></div>
  </div>
  <div class=foot id=foot></div>
</div>
<script>
const el = (t,c)=>{const e=document.createElement(t); if(c)e.className=c; return e;};
const ARCH_DAYS = 3;
const FOLLOW_DAYS = __FOLLOW_DAYS__;
const MAX_REMIND = __MAX_REMIND__;
// One id per page. The server counts open pages, so a second tab must not
// reuse this one or closing either tab would look like closing both.
const CID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()));

async function load(){
  let d;
  try{ d = await (await fetch('/api/todos?cid='+CID)).json(); }
  catch(e){ document.getElementById('sub').textContent =
      '讀不到資料，伺服器已經關閉。重新啟動 task_list_gui.py'; return; }
  const fresh = d.todos.filter(t=>!t.priority);
  const filed = d.todos.filter(t=>t.priority && !t.following);
  // Longest wait first, so the red rows lead. The sort is stable, so ties
  // keep the server's level-then-deadline order.
  const tracked = d.todos.filter(t=>t.following)
    .sort((a,b)=>(a.checked-b.checked) || (b.followAlert-a.followAlert)
                 || (b.followDays-a.followDays));
  const waiting = fresh.filter(t=>!t.checked).length;
  const open = filed.filter(t=>!t.checked).length;
  const watching = tracked.filter(t=>!t.checked).length;
  const done = d.todos.filter(t=>t.checked).length;
  document.getElementById('sub').textContent =
    `${waiting} 項待分類，${open} 項待辦，${watching} 項追蹤中`
    + (done ? `，${done} 項已排定封存` : '');
  document.getElementById('foot').textContent =
    `分類、勾選、追蹤或封存都在下次排程更新時才生效，在那之前都還可以反悔。追蹤滿 ${FOLLOW_DAYS} 天會變紅，變紅後可以延後提醒。已封存 ${d.archivedTotal} 項。`;

  fill('listnew', 'cntnew', fresh, '沒有待分類的信', triageRow);
  fill('listold', 'cntold', filed, '目前沒有待辦', row);
  fill('listfollow', 'cntfollow', tracked, '沒有追蹤中的項目', followRow);
  loadArchive();
  // Links arrive from a background lookup after the list is served, so check
  // back soon rather than at the 30 s refresh.
  if(d.linksPending) setTimeout(refresh, __LINK_POLL_MS__);
}

// A reload rebuilds every row, which would wipe a day count mid-typing.
function refresh(){
  if(!document.activeElement.classList.contains('days')) load();
}

const LEVELS = [['urgent','緊急'], ['important','重要'], ['normal','普通']];

async function post(path, payload){
  const res = await fetch(path,{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const j = await res.json();
  if(!j.ok) throw new Error(j.error||'failed');
  return j;
}

// Reloads on success rather than patching the row, because a level moves the
// row to the other section and the server is what knows where it now sorts.
function button(label, action, st, cls){
  const b = el('button', cls || 'btn');
  b.textContent = label;
  b.onclick = async ()=>{
    b.disabled = true; st.className='st saving'; st.textContent='儲存中';
    try{ await action(); await load(); }
    catch(e){ st.className='st failed'; st.textContent='未儲存'; b.disabled = false; }
  };
  return b;
}

let archOpen = false;

document.getElementById('togarch').onclick = ()=>{
  archOpen = !archOpen;
  document.getElementById('listarch').classList.toggle('hidden', !archOpen);
  document.getElementById('togarch').textContent = archOpen ? '收起' : '展開';
};

async function loadArchive(){
  let d;
  try{ d = await (await fetch('/api/archive')).json(); }
  catch(e){ return; }
  const list = document.getElementById('listarch');
  const items = d.archived || [];
  document.getElementById('cntarch').textContent =
    items.length ? `${items.length} 項，${ARCH_DAYS} 天後清除` : '目前沒有';
  list.textContent = '';
  if(!items.length){
    list.append(Object.assign(el('div','none'),{textContent:'沒有已封存的項目'}));
    return;
  }
  for(const a of items) list.append(archRow(a));
}

// Everything that locates the original mail. scout is a forwarding hub, so
// the sender names neither the account holding it nor when that account
// took delivery. The return says whether anything landed, which is how the
// archive rows avoid appending an empty meta div.
function appendSource(meta, item){
  if(item.received) meta.append(Object.assign(el('span','box'),
      {textContent:'收信 ' + item.received}), document.createTextNode('  '));
  if(item.sender) meta.append(document.createTextNode(item.sender));
  if(item.mailbox){
    const b = el('span','box');
    // No @ means the account alias, which is what gets stored for mail
    // nobody forwarded. Left bare, it reads like a value that failed to
    // resolve, which is exactly what it looked like while it was one.
    const direct = !item.mailbox.includes('@');
    b.textContent = (item.sender ? '  ' : '') + '收件 ' + item.mailbox
                  + (direct ? ' 直收' : '');
    if(direct) b.title = '信直接寄到轉信中心，沒有經過其他信箱，原信只在這個帳號裡';
    meta.append(b);
  }
  return meta.childNodes.length > 0;
}

function archRow(a){
  const r = el('div','row arch');
  const body = el('div','body');
  body.append(Object.assign(el('div','subj'),{textContent:a.subject}));
  if(a.action) body.append(Object.assign(el('div','act'),{textContent:a.action}));
  const ameta = el('div','meta');
  if(appendSource(ameta, a)) body.append(ameta);
  const left = el('div', 'left' + (a.daysLeft <= 1 ? ' soon' : ''));
  left.textContent = a.daysLeft <= 0 ? '即將清除' : `剩 ${a.daysLeft} 天`;
  const btn = el('button','btn');
  btn.textContent = a.pending ? '已排定復原' : '復原';
  btn.disabled = a.pending || !a.restorable;
  if(!a.restorable) btn.title = '這筆缺少 message id，無法復原';
  btn.onclick = async ()=>{
    btn.disabled = true; btn.textContent = '處理中';
    try{
      const res = await fetch('/api/restore',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:a.id})});
      const j = await res.json();
      if(!j.ok) throw new Error(j.error||'failed');
      // Queued, not done -- see the module docstring on why every button
      // says 已排定 rather than claiming it is finished.
      btn.textContent = '已排定復原';
    }catch(e){
      btn.textContent = '復原失敗'; btn.disabled = false;
    }
  };
  r.append(body, left, btn);
  return r;
}

function fill(listId, cntId, items, emptyText, make){
  const list = document.getElementById(listId);
  list.textContent = '';
  document.getElementById(cntId).textContent = items.length ? `${items.length} 項` : '';
  if(!items.length){
    list.append(Object.assign(el('div','none'),{textContent:emptyText}));
    return;
  }
  for(const t of items) list.append(make(t));
}

// Synchronous on purpose: navigator.clipboard.writeText settles after the click
// opened the new tab, and Chrome refuses a write from an unfocused page.
function copyNow(text){
  const ta = Object.assign(el('textarea'), {value:text});
  ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.append(ta);
  ta.select();
  let ok = false;
  try{ ok = document.execCommand('copy'); }catch(e){}
  ta.remove();
  return ok;
}

function cardBody(t){
  const body = el('div','body');
  const subj = el('div','subj');
  if(t.link){
    const a = Object.assign(el('a'), {href:t.link, target:'_blank', rel:'noopener',
        textContent:t.subject, title:'在原收件信箱開信，可以直接回信'});
    subj.append(a);
    if(t.outlookQuery){
      a.title = '複製搜尋條件並開啟 Outlook，在搜尋框按 Ctrl+V 再按 Enter';
      const tip = el('span','pend');
      // The default action still opens the mailbox in the new tab.
      a.onclick = ()=>{
        tip.textContent = copyNow(t.outlookQuery)
          ? '  已複製搜尋條件，到 Outlook 搜尋框貼上後按 Enter'
          : '  複製失敗，請手動搜尋 ' + t.outlookQuery;
      };
      subj.append(tip);
    }
  }else subj.textContent = t.subject;
  body.append(subj);
  if(t.action) body.append(Object.assign(el('div','act'),{textContent:t.action}));
  const meta = el('div','meta');
  if(t.priority){
    const name = (LEVELS.find(([lv])=>lv===t.priority) || [,''])[1];
    meta.append(Object.assign(el('span','lv '+t.priority),
        {textContent: name + (t.pendingLevel ? ' 已排定' : '')}));
  }
  if(t.deadline){ const d=el('span','due'); d.textContent='期限 '+t.deadline;
                  meta.append(d, document.createTextNode('  ')); }
  if(t.uncertain){ const u=el('span','flag'); u.textContent='待確認 請自行開信';
                   meta.append(u, document.createTextNode('  ')); }
  if(!t.tickable){ const n=el('span','flag'); n.textContent='缺 id 無法操作';
                   meta.append(n, document.createTextNode('  ')); }
  if(t.pendingFollow){
    meta.append(Object.assign(el('span','pend'),
        {textContent: t.following ? '已排定追蹤' : '已排定回到待辦'}),
      document.createTextNode('  '));
  }else if(t.following){
    const a = el('span','age' + (t.followAlert ? ' stale' : ''));
    a.textContent = (t.followDays ? `追蹤 ${t.followDays} 天` : '今天開始追蹤')
      + (t.remindIn ? `，${t.remindIn} 天後提醒` + (t.pendingRemind ? ' 已排定' : '') : '');
    meta.append(a, document.createTextNode('  '));
  }
  appendSource(meta, t);
  body.append(meta);
  return body;
}

// 待分類 gets the four choices and no checkbox. 封存 is the same queued tick
// as 已完成, so until the round reads it the row stays here and can be undone.
function triageRow(t){
  const r = el('div','row' + (t.checked?' done':''));
  const acts = el('div','acts');
  const st = el('div','st');
  if(t.tickable && t.checked){
    acts.append(Object.assign(el('span','pend'),{textContent:'已排定封存'}),
                button('取消', ()=>post('/api/check',{id:t.id,checked:false}), st));
  }else if(t.tickable){
    for(const [lv, name] of LEVELS)
      acts.append(button(name, ()=>post('/api/triage',{id:t.id,level:lv}), st,
                         'btn ' + lv));
    acts.append(button('封存', ()=>post('/api/check',{id:t.id,checked:true}), st));
  }
  r.append(cardBody(t), acts, st);
  return r;
}

// 追蹤中 mirrors 待分類: no checkbox, and 封存 is the same queued tick, so the
// row stays here with 取消 until the round reads it. 回到待辦 keeps the level.
function followRow(t){
  const r = el('div','row' + (t.checked?' done':'') + (t.followAlert?' stale':''));
  const acts = el('div','acts');
  const st = el('div','st');
  if(t.tickable && t.checked){
    acts.append(Object.assign(el('span','pend'),{textContent:'已排定封存'}),
                button('取消', ()=>post('/api/check',{id:t.id,checked:false}), st));
  }else if(t.tickable){
    // Only a red row can be postponed, so the input is not noise on the rest.
    if(t.followAlert){
      const n = el('input','days');
      Object.assign(n, {type:'number', min:1, max:MAX_REMIND, value:FOLLOW_DAYS,
                        title:'幾天後再提醒'});
      acts.append(n, button('天後提醒', ()=>{
        const days = Number(n.value);
        if(!Number.isInteger(days) || days < 1 || days > MAX_REMIND)
          return Promise.reject(new Error('bad days'));
        return post('/api/remind',{id:t.id,days});
      }, st));
    }
    acts.append(button('回到待辦', ()=>post('/api/follow',{id:t.id,follow:false}), st),
                button('封存', ()=>post('/api/check',{id:t.id,checked:true}), st));
  }
  r.append(cardBody(t), acts, st);
  return r;
}

// 待辦清單 offers only 已完成, 轉追蹤 and 重新分類, never the three levels, so a
// filed row cannot be re-filed by a stray click. 重新分類 sends it back to 待分類.
function row(t){
  const r = el('div','row' + (t.checked?' done':''));
  const cb = el('input'); cb.type='checkbox'; cb.checked=t.checked;
  cb.title = '已完成';
  if(!t.tickable){ cb.disabled=true; cb.title='這筆缺少 message id，無法勾選'; }
  const acts = el('div','acts');
  const st = el('div','st');
  if(t.tickable)
    acts.append(button('轉追蹤', ()=>post('/api/follow',{id:t.id,follow:true}), st),
                button('重新分類', ()=>post('/api/triage',{id:t.id,level:''}), st));
  acts.classList.toggle('hidden', t.checked);
  r.append(cb, cardBody(t), acts, st);

  cb.onchange = async ()=>{
    const want = cb.checked;
    st.className='st saving'; st.textContent='儲存中';
    cb.disabled = true;
    try{
      const res = await fetch('/api/check',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:t.id,checked:want})});
      const j = await res.json();
      if(!j.ok) throw new Error(j.error||'failed');
      st.className='st saved'; st.textContent='已儲存';
      r.classList.toggle('done', want);
      // A row queued as done has nothing left to re-file.
      acts.classList.toggle('hidden', want);
      setTimeout(()=>{st.textContent='';},1400);
    }catch(e){
      // Never leave the box showing a state that is not on disk.
      cb.checked = !want;
      st.className='st failed'; st.textContent='未儲存';
    }finally{ cb.disabled = false; }
  };
  return r;
}
load();
setInterval(refresh, 30000);
setInterval(()=>fetch('/api/ping?cid='+CID,{method:'POST'}).catch(()=>{}), __PING_MS__);
// pagehide, not beforeunload: beforeunload is for confirmation dialogs and
// is unreliable on mobile, while pagehide is the unload-side event browsers
// still allow a beacon from. It does not cover a discarded tab (Chrome runs
// no callbacks on discard), which is what the ping timeout is the backstop
// for. Without this, every close would wait that timeout out.
addEventListener('pagehide', ()=>{
  navigator.sendBeacon('/api/bye?cid='+CID);
});
</script>
"""


def port_is_taken(host: str, port: int) -> bool:
    """Whether something already answers on the port.

    A scheduled run launches this many times a day, so an already-open page
    is normal, not an error. The check must happen before the browser is
    scheduled to open, or a second tab would open against a server this
    process will never own; the existing page already polls every 30s and
    picks up new todos on its own.

    Occupancy only. Nothing here identifies the listener, so any other process
    holding the port also stops this one, silently and with no todo list.
    """
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}/"
    if port_is_taken(HOST, PORT):
        print("port " + str(PORT) + " is already in use, not starting. "
              + "If the todo list is the listener it is at " + url)
        raise SystemExit(0)
    print("Gmail todo list at " + url)
    print("Closing the page exits this process. Ctrl+C also works.")
    # Timer threads are not daemons, so this has to come after the port check
    # or a failed bind would still pop a tab before the process gave up.
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    # Bound to loopback only. This is a single-user local viewer, never exposed.
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
