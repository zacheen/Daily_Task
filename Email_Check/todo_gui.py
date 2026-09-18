"""On-demand todo list viewer for the Gmail check tasks.

Launch it, tick what you finished, close the tab. The process exits on its own
once no page is open, which is what lets an unattended scheduled run start it
without leaving a resident server nobody will ever close.

Liveness is tracked per page, not per connection, so a second tab is not
killed when the first one closes. A closing page's beacon drops its id
immediately; the ping timeout is only a backstop for closes that send no
beacon at all (crash, sleep, a discarded background tab).

Writer discipline is the whole design. This process writes the three request
files and nothing else, and only ever reads `state.json` and the archive.
statemachine.py is the reverse. With one writer per file plus atomic replace, a
reader always sees a complete old or complete new file and no lock is needed.

Each request file is one verb the user can aim at a row. A tick says done, a
restore says un-archive that, a promote says the notified mail in 重要事項 is
really a todo. None of them takes effect until the next scheduled round reads
it, which is why every button says 已排定 rather than claiming it is finished.

A tick is persisted on the request that carries it, not on window close, so
killing the process cannot lose one. The page reports per-row save state
because a stale tab against a stopped server otherwise looks identical to a
successful save.

Run with
    conda run -n ML python "D:\\dont_move\\git_save\\Daily_Task\\Email_Check\\todo_gui.py"
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import tempfile
import threading
import time
import webbrowser

from flask import Flask, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
CHECKED_PATH = os.path.join(HERE, "todos-checked.json")
ARCHIVE_PATH = os.path.join(HERE, "todos-archive.json")
RESTORE_PATH = os.path.join(HERE, "todos-restore.json")
PROMOTE_PATH = os.path.join(HERE, "todos-promote.json")
# Mirrors ARCHIVE_TTL in statemachine.py, for showing days remaining.
ARCHIVE_TTL_DAYS = 3
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

app = Flask(__name__)
_lock = threading.Lock()
_started = time.time()
_clients: dict[str, float] = {}
_seen_any = False


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
    fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".gui.", suffix=".tmp")
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


def load_todos() -> list[dict]:
    return _read_json(STATE_PATH, {}).get("todos", [])


def _next_rev() -> str:
    """Monotonic-enough revision marker for the tick file."""
    return f"{time.time_ns()}"


def load_checked() -> set[str]:
    return {str(x) for x in _read_json(CHECKED_PATH, {}).get("checkedIds", [])}


def load_promotes() -> set[str]:
    return {str(x) for x in _read_json(PROMOTE_PATH, {}).get("promoteIds", [])}


def reviewable_notices(todos: list[dict], notices: list[dict]) -> list[dict]:
    """The notices 重要事項 may show: pushed mail with no todo of its own.

    A message the todo list already carries is dropped rather than listed
    twice, and its 加到待辦 button would be dead anyway since the state machine
    skips promoting a live duplicate.

    record_notices now keeps both queues disjoint, so this only ever fires on
    state.json written before that rule, or hand-edited. It stays because this
    is the one place the rule is visible to the user.

    An id-less notice is kept: it can be matched against no todo at all.
    """
    owned = {k for k in (_usable_id(t) for t in todos if isinstance(t, dict))
             if k is not None}
    return [n for n in notices
            if isinstance(n, dict) and _usable_id(n) not in owned]


def prune_requests(live_ids: set[str],
                   notice_ids: set[str]) -> tuple[set[str], set[str]]:
    """Drop requests whose target is gone. Returns the ticks and promotes left.

    All three request files need this: each would otherwise grow forever, and a
    message id reused by a future todo, archive entry or notice would arrive
    already ticked, already flagged for restore, or already promoted.
    """
    archived_ids = {_usable_id(a) for a in _read_json(ARCHIVE_PATH, {}).get("archived", [])
                    if isinstance(a, dict)}
    wanted = load_restores()
    live_restores = wanted & archived_ids
    if live_restores != wanted:
        _atomic_write(RESTORE_PATH,
                      {"restoreIds": sorted(live_restores), "rev": _next_rev()})

    # A promote is dropped once its notice leaves state.json, whether that was
    # this promotion landing or the round sweeping the notice into the archive.
    # A click in the milliseconds between the round reading this file and
    # sweeping is therefore lost, and the item shows up in 已封存 instead; 復原
    # is the way back. /api/promote rejects the same click once the sweep is
    # visible, which is what keeps the window that small.
    promotes = load_promotes()
    live_promotes = promotes & notice_ids
    if live_promotes != promotes:
        _atomic_write(PROMOTE_PATH,
                      {"promoteIds": sorted(live_promotes), "rev": _next_rev()})

    checked = load_checked()
    pruned = checked & live_ids
    if pruned != checked:
        _atomic_write(CHECKED_PATH, {"checkedIds": sorted(pruned), "rev": _next_rev()})
    return pruned, live_promotes


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
    return PAGE.replace("__PING_MS__", str(PING_EVERY_MS))


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


@app.post("/api/promote")
def api_promote():
    """Queue "this notice is a todo". The state machine adds it next commit.

    Same shape as /api/restore, a GUI-owned request file rather than a write to
    state.json, so that file keeps exactly one writer.
    """
    body = request.get_json(silent=True) or {}
    tid = str(body.get("id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    with _lock:
        state = _read_json(STATE_PATH, {})
        notices = state.get("notices", [])
        if not isinstance(notices, list):
            return jsonify({"ok": False, "error": "state unreadable"}), 409
        shown = reviewable_notices(state.get("todos", []), notices)
        if tid not in {_usable_id(n) for n in shown}:
            # Swept into the archive by a round that ran while this tab was
            # open, or already on the todo list. Saying so beats queueing a
            # request nothing will consume; 復原 in 已封存 is the way back from
            # the archive, and a promote for a live todo is a no-op.
            return jsonify({"ok": False, "error": "gone", "gone": True}), 409
        wanted = load_promotes()
        wanted.add(tid)
        _atomic_write(PROMOTE_PATH, {"promoteIds": sorted(wanted), "rev": _next_rev()})
    return jsonify({"ok": True, "id": tid, "pending": True})


@app.get("/api/todos")
def api_todos():
    _touch(request.args.get("cid", ""))
    with _lock:
        state = _read_json(STATE_PATH, {})
        todos = state.get("todos", [])
        notices = reviewable_notices(todos, state.get("notices", []))
        live = {k for k in (_usable_id(t) for t in todos) if k}
        notice_ids = {k for k in (_usable_id(n) for n in notices) if k}
        checked, promoting = prune_requests(live, notice_ids)
    # "New" means the most recent round produced it. Comparing against roundSeq
    # rather than a timestamp keeps it aligned with the state machine, so a
    # paused app or a catch-up burst cannot mislabel a row.
    latest = int(state.get("roundSeq", 0))
    rows = []
    for t in todos:
        tid = _usable_id(t)
        rows.append({
            "id": tid or "",
            "tickable": tid is not None,
            "subject": t.get("subject") or "(no subject)",
            "sender": t.get("from") or "",
            "mailbox": t.get("mailbox") or "",
            "received": t.get("received") or "",
            "action": t.get("action") or "",
            "deadline": t.get("deadline") or "",
            "uncertain": bool(t.get("uncertain")),
            "checked": tid is not None and tid in checked,
            "isNew": int(t.get("createdRound", 0)) >= latest and latest > 0,
        })
    # Sort on a parsed date, not the raw string. Lexicographic order puts
    # "10-2" before "9-15", which shows a later deadline as the more urgent one.
    rows.sort(key=lambda r: (r["checked"], not r["deadline"],
                             _deadline_key(r["deadline"]), r["subject"]))
    # Left in state.json order, which is oldest notice first. Sorting by a
    # deadline would be wrong here: a notice has no deadline, only a push.
    notice_rows = [{
        "id": _usable_id(n) or "",
        "promotable": _usable_id(n) is not None,
        "subject": n.get("subject") or n.get("summary") or "(no subject)",
        "sender": n.get("from") or "",
        "mailbox": n.get("mailbox") or "",
        "received": n.get("received") or "",
        "summary": n.get("summary") or n.get("action") or "",
        "pending": _usable_id(n) is not None and _usable_id(n) in promoting,
    } for n in notices]
    archived = len(_read_json(ARCHIVE_PATH, {}).get("archived", []))
    return jsonify({"todos": rows, "notices": notice_rows,
                    "archivedTotal": archived, "roundSeq": latest})


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
 #secnotice .hd{color:#ffd479}
 #secnew .hd{color:#7fb2ff}
 #secarch .hd{color:#8b9096}
 .tog{float:right;font-weight:400;letter-spacing:0;text-transform:none;
      color:#7fb2ff;cursor:pointer;font-size:12px}
 .row.arch{opacity:.6}
 .row.note{border-color:#3d3626;background:#1e1c16}
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
  <div class="sec hidden" id=secnotice>
    <div class=hd>重要事項 <span class=n id=cntnotice></span></div>
    <div id=listnotice></div>
  </div>
  <div class=sec id=secnew>
    <div class=hd>這次新增 <span class=n id=cntnew></span></div>
    <div id=listnew></div>
  </div>
  <div class=sec id=secold>
    <div class=hd>之前的 <span class=n id=cntold></span></div>
    <div id=listold></div>
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
// One id per page. The server counts open pages, so a second tab must not
// reuse this one or closing either tab would look like closing both.
const CID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()));

async function load(){
  let d;
  try{ d = await (await fetch('/api/todos?cid='+CID)).json(); }
  catch(e){ document.getElementById('sub').textContent =
      '讀不到資料，伺服器已經關閉。重新啟動 todo_gui.py'; return; }
  const open = d.todos.filter(t=>!t.checked).length;
  document.getElementById('sub').textContent =
    d.todos.length ? `${open} 項待辦，${d.todos.length-open} 項已勾選待清理`
                   : '目前沒有待辦';
  document.getElementById('foot').textContent =
    `勾選後會留在畫面上，下次排程更新時才封存，所以還可以反悔。已封存 ${d.archivedTotal} 項。`;

  const fresh = d.todos.filter(t=>t.isNew);
  const older = d.todos.filter(t=>!t.isNew);
  fillNotices(d.notices || []);
  fill('listnew', 'cntnew', 'secnew', fresh, '這一輪沒有新增');
  fill('listold', 'cntold', 'secold', older, '沒有舊的待辦');
  // Hide the whole list only when both sections are empty, so an empty "new"
  // section still tells the user the run happened and found nothing.
  document.getElementById('secold').classList.toggle('hidden',
      !older.length && !d.todos.length);
  loadArchive();
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

// Hidden when empty, unlike 這次新增: an empty notice list says nothing, while
// an empty 這次新增 still tells the user the round ran and found nothing.
function fillNotices(items){
  document.getElementById('secnotice').classList.toggle('hidden', !items.length);
  const list = document.getElementById('listnotice');
  list.textContent = '';
  document.getElementById('cntnotice').textContent =
    items.length ? `${items.length} 項，沒有處理就會移到已封存` : '';
  for(const n of items) list.append(noticeRow(n));
}

// Everything that locates the original mail. scout is a forwarding hub, so
// the sender names neither the account holding it nor when that account
// took delivery. The return says whether anything landed, which is how the
// notice and archive rows avoid appending an empty meta div.
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

function noticeRow(n){
  const r = el('div','row note');
  const body = el('div','body');
  body.append(Object.assign(el('div','subj'),{textContent:n.subject}));
  // A notice with no subject falls back to its summary for the heading, so
  // rendering the summary again would print the same line twice.
  if(n.summary && n.summary !== n.subject)
    body.append(Object.assign(el('div','act'),{textContent:n.summary}));
  const nmeta = el('div','meta');
  if(appendSource(nmeta, n)) body.append(nmeta);
  const btn = el('button','btn');
  btn.textContent = n.pending ? '已排定加入' : '加到待辦';
  btn.disabled = n.pending || !n.promotable;
  if(!n.promotable) btn.title = '這筆缺少 message id，無法加入待辦';
  btn.onclick = async ()=>{
    btn.disabled = true; btn.textContent = '處理中';
    try{
      const res = await fetch('/api/promote',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:n.id})});
      const j = await res.json();
      if(!j.ok) throw new Error(j.error||'failed');
      // Queued, not done -- see the module docstring on why every button
      // says 已排定 rather than claiming it is finished.
      btn.textContent = '已排定加入';
    }catch(e){
      btn.textContent = '加入失敗'; btn.disabled = false;
    }
  };
  r.append(body, btn);
  return r;
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

function fill(listId, cntId, secId, items, emptyText){
  const list = document.getElementById(listId);
  list.textContent = '';
  document.getElementById(cntId).textContent = items.length ? `${items.length} 項` : '';
  if(!items.length){
    list.append(Object.assign(el('div','none'),{textContent:emptyText}));
    return;
  }
  for(const t of items) list.append(row(t));
}

function row(t){
  const r = el('div','row' + (t.checked?' done':''));
  const cb = el('input'); cb.type='checkbox'; cb.checked=t.checked;
  if(!t.tickable){ cb.disabled=true; cb.title='這筆缺少 message id，無法勾選'; }
  const body = el('div','body');
  body.append(Object.assign(el('div','subj'),{textContent:t.subject}));
  if(t.action) body.append(Object.assign(el('div','act'),{textContent:t.action}));
  const meta = el('div','meta');
  if(t.deadline){ const d=el('span','due'); d.textContent='期限 '+t.deadline;
                  meta.append(d, document.createTextNode('  ')); }
  if(t.uncertain){ const u=el('span','flag'); u.textContent='待確認 請自行開信';
                   meta.append(u, document.createTextNode('  ')); }
  if(!t.tickable){ const n=el('span','flag'); n.textContent='缺 id 無法勾選';
                   meta.append(n, document.createTextNode('  ')); }
  appendSource(meta, t);
  body.append(meta);
  const st = el('div','st');
  r.append(cb, body, st);

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
setInterval(load, 30000);
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

    A scheduled run launches this four times a day, so an already-open page
    is normal, not an error. The check must happen before the browser is
    scheduled to open, or a second tab would open against a server this
    process will never own; the existing page already polls every 30s and
    picks up new todos on its own.
    """
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}/"
    if port_is_taken(HOST, PORT):
        print("already open at " + url + ", nothing to do")
        raise SystemExit(0)
    print("Gmail todo list at " + url)
    print("Closing the page exits this process. Ctrl+C also works.")
    # Timer threads are not daemons, so this has to come after the port check
    # or a failed bind would still pop a tab before the process gave up.
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    # Bound to loopback only. This is a single-user local viewer, never exposed.
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
