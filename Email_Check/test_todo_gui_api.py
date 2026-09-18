"""HTTP tests for the GUI's request endpoints, against a temp data directory.

test_todo_gui.py only covers the pure helpers, so /api/archive and /api/restore
had no net at all. That is exactly where two comments drifted out of date
through three review rounds, because nowhere else could an assertion fail. The
promote endpoint behind the notice section arrives with one from the start.

Flask's test client is used rather than a real server, so nothing binds a port
and the watchdog never runs.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "todo_gui.py")
spec = importlib.util.spec_from_file_location("tg", SRC)
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)

work = tempfile.mkdtemp(prefix="guiapi.")
# HERE too, not just the paths: _atomic_write puts its temp file beside the
# target, so leaving it pointed at the project would litter the real directory.
tg.HERE = work
tg.STATE_PATH = os.path.join(work, "state.json")
tg.CHECKED_PATH = os.path.join(work, "todos-checked.json")
tg.ARCHIVE_PATH = os.path.join(work, "todos-archive.json")
tg.RESTORE_PATH = os.path.join(work, "todos-restore.json")
tg.PROMOTE_PATH = os.path.join(work, "todos-promote.json")
tg.app.config["TESTING"] = True
cli = tg.app.test_client()

fails = []


def check(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


def write(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def read(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def clear(*paths):
    for p in paths:
        if os.path.exists(p):
            os.unlink(p)


NOW = int(time.time())
DAY = 86400

# --- /api/todos serves notices alongside todos ---
write(tg.STATE_PATH, {
    "roundSeq": 12,
    "todos": [{"id": "old1", "subject": "older todo", "createdRound": 11,
               "from": "reg@school.edu", "mailbox": "me@school.edu",
               "received": "2026-10-01 08:14",
               "action": "do the thing", "deadline": "2026-10-02"},
              {"id": "new1", "subject": "newer todo", "createdRound": 12,
               "deadline": "2026-09-15", "uncertain": True}],
    "notices": [{"id": "nt1", "subject": "recruiter reply", "from": "a@b.c",
                 "mailbox": "scout", "summary": "reply by Friday",
                 "noticedRound": 12},
                {"id": "nt2", "summary": "album shared", "noticedRound": 12}],
})
d = cli.get("/api/todos").get_json()
check("notices come back in their own list",
      [n["id"] for n in d["notices"]] == ["nt1", "nt2"], d["notices"])
check("state.json order is preserved, oldest notice first",
      d["notices"][0]["id"] == "nt1", d["notices"])
check("a notice with no subject falls back to its summary",
      d["notices"][1]["subject"] == "album shared", d["notices"][1])
check("the sender is carried through for the row's meta line",
      d["notices"][0]["sender"] == "a@b.c", d["notices"][0])
check("the delivering mailbox rides along, so the row can say where to look",
      d["notices"][0]["mailbox"] == "scout", d["notices"][0])
check("a todo carries its mailbox too",
      [t["mailbox"] for t in d["todos"] if t["id"] == "old1"] == ["me@school.edu"],
      d["todos"])
check("a todo stored before the field existed serves an empty one, not a KeyError",
      [t["mailbox"] for t in d["todos"] if t["id"] == "new1"] == [""],
      d["todos"])
check("the received stamp rides along too, so the row can say when to look",
      [t["received"] for t in d["todos"] if t["id"] == "old1"]
      == ["2026-10-01 08:14"], d["todos"])
check("the mailbox is served as stored, alias and all, for the page to label",
      d["notices"][0]["mailbox"] == "scout", d["notices"][0])
check("nothing is pending before the user clicks",
      not any(n["pending"] for n in d["notices"]), d["notices"])
check("todos still sort by parsed deadline, not lexicographically",
      [t["id"] for t in d["todos"]] == ["new1", "old1"], d["todos"])
check("the new todo is flagged as new", d["todos"][0]["isNew"] is True, d["todos"][0])

# --- /api/promote queues, and says so ---
r = cli.post("/api/promote", json={"id": "nt1"})
check("promoting a live notice succeeds", r.status_code == 200 and r.get_json()["ok"],
      r.get_json())
check("the request lands in the promote file, nowhere else",
      read(tg.PROMOTE_PATH)["promoteIds"] == ["nt1"], read(tg.PROMOTE_PATH))
check("the promote file carries a rev like the other request files",
      bool(read(tg.PROMOTE_PATH).get("rev")), read(tg.PROMOTE_PATH))
check("state.json is untouched, so it keeps one writer",
      read(tg.STATE_PATH)["notices"][0]["id"] == "nt1")
check("the row now reports itself as pending",
      [n["pending"] for n in cli.get("/api/todos").get_json()["notices"]] == [True, False])

r = cli.post("/api/promote", json={"id": "nt2"})
check("a second promote is added, not substituted",
      read(tg.PROMOTE_PATH)["promoteIds"] == ["nt1", "nt2"], read(tg.PROMOTE_PATH))

# A click that lands after a round swept the notice must fail visibly. Queueing
# it would leave a request nothing ever consumes.
r = cli.post("/api/promote", json={"id": "swept-already"})
check("promoting a notice that is gone is refused",
      r.status_code == 409 and r.get_json()["gone"] is True, r.get_json())
check("and it is not queued", "swept-already" not in read(tg.PROMOTE_PATH)["promoteIds"])

r = cli.post("/api/promote", json={})
check("promoting with no id is a bad request", r.status_code == 400, r.status_code)

# --- consumed requests are pruned, or the files grow forever ---
# The state machine has promoted nt1 into a todo and swept nt2, so neither id
# is a live notice any more.
write(tg.STATE_PATH, {"roundSeq": 13,
                      "todos": [{"id": "nt1", "subject": "promoted", "createdRound": 13}],
                      "notices": []})
cli.get("/api/todos")
check("promote ids drop once their notice leaves state.json",
      read(tg.PROMOTE_PATH)["promoteIds"] == [], read(tg.PROMOTE_PATH))

write(tg.CHECKED_PATH, {"checkedIds": ["nt1", "already-archived"]})
write(tg.RESTORE_PATH, {"restoreIds": ["not-in-archive"]})
write(tg.ARCHIVE_PATH, {"archived": []})
cli.get("/api/todos")
check("a tick for a todo that is gone is pruned",
      read(tg.CHECKED_PATH)["checkedIds"] == ["nt1"], read(tg.CHECKED_PATH))
check("a restore for something no longer archived is pruned",
      read(tg.RESTORE_PATH)["restoreIds"] == [], read(tg.RESTORE_PATH))

# --- /api/check ---
r = cli.post("/api/check", json={"id": "nt1", "checked": True})
check("ticking a live todo succeeds", r.get_json()["ok"] is True, r.get_json())
check("the tick is on disk before the response returns",
      "nt1" in read(tg.CHECKED_PATH)["checkedIds"], read(tg.CHECKED_PATH))
r = cli.post("/api/check", json={"id": "nt1", "checked": False})
check("un-ticking removes it", read(tg.CHECKED_PATH)["checkedIds"] == [],
      read(tg.CHECKED_PATH))
r = cli.post("/api/check", json={"id": "archived-mid-session", "checked": True})
check("ticking a todo archived while the tab was open is refused as stale",
      r.status_code == 409 and r.get_json()["gone"] is True, r.get_json())
r = cli.post("/api/check", json={"checked": True})
check("ticking with no id is a bad request", r.status_code == 400, r.status_code)

# --- /api/archive: the days-left arithmetic and the ordering ---
write(tg.ARCHIVE_PATH, {"archived": [
    {"id": "fresh", "subject": "just archived", "archivedAt": NOW},
    {"id": "aging", "subject": "two and a half days", "action": "was a todo",
     "from": "reg@school.edu", "mailbox": "me@school.edu",
     "received": "2026-10-01 08:14", "archivedAt": NOW - int(2.5 * DAY)},
    {"id": "expired", "subject": "past the window", "archivedAt": NOW - 4 * DAY},
    {"id": "nostamp", "subject": "written before stamps existed"},
    {"subject": "no id at all", "archivedAt": NOW},
]})
a = cli.get("/api/archive").get_json()["archived"]
left = {r["id"]: r["daysLeft"] for r in a}
check("a fresh entry shows the full window", left["fresh"] == 3, left)
check("days left floors rather than rounds", left["aging"] == 1, left)
check("an over-age entry clamps at zero, never negative", left["expired"] == 0, left)
check("an unstamped entry shows the full window, not an imminent purge",
      left["nostamp"] == 3,
      "the state machine stamps it on its next run, so it has not started aging")
check("an archived row keeps every source field, the same as a live one",
      [(r["sender"], r["mailbox"], r["received"]) for r in a if r["id"] == "aging"]
      == [("reg@school.edu", "me@school.edu", "2026-10-01 08:14")], a)
check("soonest to purge sorts first", [r["id"] for r in a][0] == "expired",
      [r["id"] for r in a])
check("an id-less entry is served but marked unrestorable",
      [r["restorable"] for r in a if not r["id"]] == [False], a)

write(tg.ARCHIVE_PATH, {"archived": "not a list"})
a = cli.get("/api/archive").get_json()
check("a wrong-shaped archive reports an error instead of raising",
      a["archived"] == [] and a["error"] == "archive unreadable", a)

# --- /api/restore ---
write(tg.ARCHIVE_PATH, {"archived": [{"id": "back1", "subject": "mis-ticked",
                                      "archivedAt": NOW}]})
clear(tg.RESTORE_PATH)
r = cli.post("/api/restore", json={"id": "back1"})
check("restoring an archived todo is queued", r.get_json()["ok"] is True, r.get_json())
check("the request lands in the restore file",
      read(tg.RESTORE_PATH)["restoreIds"] == ["back1"], read(tg.RESTORE_PATH))
check("the archive itself is untouched, so it keeps one writer",
      read(tg.ARCHIVE_PATH)["archived"][0]["id"] == "back1")
check("the row reports itself as pending afterwards",
      cli.get("/api/archive").get_json()["archived"][0]["pending"] is True)
r = cli.post("/api/restore", json={"id": "purged-already"})
check("restoring something already purged is refused",
      r.status_code == 409 and r.get_json()["gone"] is True, r.get_json())
r = cli.post("/api/restore", json={})
check("restoring with no id is a bad request", r.status_code == 400, r.status_code)

# --- 重要事項 never shows a mail the todo list already carries ---
# Reported from the GUI: a mail with both a push and a next step got a row in
# each section at once. record_notices keeps the two queues disjoint now, so
# what is left to cover here is state written before that rule, and the promote
# endpoint agreeing with what the page renders.
clear(tg.CHECKED_PATH, tg.RESTORE_PATH, tg.PROMOTE_PATH)
write(tg.STATE_PATH, {
    "roundSeq": 20,
    "todos": [{"id": "dup", "subject": "PayPal statement", "createdRound": 20,
               "action": "log in and reconcile August"}],
    "notices": [{"id": "dup", "subject": "PayPal statement",
                 "summary": "August statement is out", "noticedRound": 20},
                {"id": "solo", "summary": "a person wrote to you",
                 "noticedRound": 20},
                {"subject": "no id at all", "noticedRound": 20}],
})
d = cli.get("/api/todos").get_json()
check("a notice whose message is a live todo is not served",
      [n["id"] for n in d["notices"]] == ["solo", ""], d["notices"])
check("the todo row is untouched, so the mail is still actionable",
      [t["id"] for t in d["todos"]] == ["dup"], d["todos"])
r = cli.post("/api/promote", json={"id": "dup"})
check("promoting it is refused, since the state machine would skip it anyway",
      r.status_code == 409 and r.get_json()["gone"] is True, r.get_json())
check("so no request file is created for a button the page never renders",
      not os.path.exists(tg.PROMOTE_PATH))

# --- a missing state.json must serve an empty list, not a 500 ---
clear(tg.STATE_PATH, tg.ARCHIVE_PATH, tg.CHECKED_PATH, tg.RESTORE_PATH, tg.PROMOTE_PATH)
d = cli.get("/api/todos").get_json()
check("a first run with no state file still serves",
      d["todos"] == [] and d["notices"] == [], d)
check("and no request file was created just by reading",
      not os.path.exists(tg.PROMOTE_PATH) and not os.path.exists(tg.CHECKED_PATH))

# --- the section has to actually be in the page, above 這次新增 ---
page = tg.index()
check("the page renders the notice section", "重要事項" in page)
check("it sits above the new-todo section", page.index("重要事項") < page.index("這次新增"),
      "the user asked for it above the new-todo section")
check("it starts hidden, so an empty one does not flash on load",
      'class="sec hidden" id=secnotice' in page)
check("the promote button posts to the promote endpoint", "'/api/promote'" in page)
check("an alias mailbox is labelled rather than left looking unresolved",
      "直收" in page and "includes('@')" in page)
check("every row kind renders the source line through one function",
      page.count("appendSource(") == 4, page.count("appendSource("))
# Caught by looking at the rendered page: the subject falls back to the summary,
# so a notice with no subject printed the same line twice.
check("a summary equal to the subject is not printed twice",
      "n.summary !== n.subject" in page)
check("a notice row explains what happens if it is ignored",
      "移到已封存" in page)

shutil.rmtree(work, ignore_errors=True)
print()
print("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails))
sys.exit(1 if fails else 0)
