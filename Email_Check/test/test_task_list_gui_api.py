"""HTTP tests for the GUI's request endpoints, against a temp data directory.

test_task_list_gui.py only covers the pure helpers, so /api/archive and /api/restore
had no net at all. That is exactly where two comments drifted out of date
through three review rounds, because nowhere else could an assertion fail. The
triage endpoint behind 待分類 arrives with one from the start.

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
import urllib.parse

# Email_Check/, one level up from test/. The viewer sits under task_list/.
CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(CODE, "task_list", "task_list_gui.py")
spec = importlib.util.spec_from_file_location("tg", SRC)
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)

work = tempfile.mkdtemp(prefix="guiapi.")
# HERE too, not just the paths: _atomic_write puts its temp file beside the
# target, so leaving it pointed at the project would litter the real directory.
tg.HERE = work
tg.STATE_PATH = os.path.join(work, "state.json")
tg.CHECKED_PATH = os.path.join(work, "tasks-checked.json")
tg.ARCHIVE_PATH = os.path.join(work, "tasks-archive.json")
tg.RESTORE_PATH = os.path.join(work, "tasks-restore.json")
tg.TRIAGE_PATH = os.path.join(work, "tasks-triage.json")
tg.FOLLOW_PATH = os.path.join(work, "tasks-follow.json")
tg.LINKS_PATH = os.path.join(work, "mail-links.json")
tg.app.config["TESTING"] = True
cli = tg.app.test_client()
# Every /api/todos starts a link lookup, which would otherwise log in to the
# real mailboxes. The link section below swaps in stubs of its own.
tg._lookup_links = lambda ids: {}

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


def by_id(rows):
    return {r["id"]: r for r in rows}


NOW = int(time.time())
DAY = 86400

# --- /api/todos serves every todo with the level the page files it under ---
write(tg.STATE_PATH, {
    "roundSeq": 12,
    "todos": [{"id": "old1", "subject": "older todo", "priority": "normal",
               "from": "reg@school.edu", "mailbox": "me@school.edu",
               "received": "2026-10-01 08:14",
               "action": "do the thing", "deadline": "2026-10-02"},
              {"id": "new2", "subject": "second new mail", "deadline": "2026-10-20"},
              {"id": "new1", "subject": "new mail", "deadline": "2026-09-15",
               "uncertain": True},
              {"id": "urg1", "subject": "urgent one", "priority": "urgent",
               "deadline": "2026-12-01"},
              {"id": "imp1", "subject": "important one", "priority": "important"}],
})
d = cli.get("/api/todos").get_json()
rows = by_id(d["todos"])
check("an untriaged todo is served with no level, which is what puts it in 待分類",
      rows["new1"]["priority"] == "" and rows["new2"]["priority"] == "", d["todos"])
check("a filed todo carries its level", rows["urg1"]["priority"] == "urgent", rows["urg1"])
check("filed rows sort by level before deadline",
      [t["id"] for t in d["todos"] if t["priority"]] == ["urg1", "imp1", "old1"],
      [t["id"] for t in d["todos"]])
check("untriaged rows sort by parsed deadline among themselves",
      [t["id"] for t in d["todos"] if not t["priority"]] == ["new1", "new2"],
      [t["id"] for t in d["todos"]])
check("nothing is pending before the user clicks",
      not any(t["pendingLevel"] for t in d["todos"]), d["todos"])
check("a todo carries its mailbox", rows["old1"]["mailbox"] == "me@school.edu", rows["old1"])
check("a todo stored before the field existed serves an empty one, not a KeyError",
      rows["new1"]["mailbox"] == "", rows["new1"])
check("the received stamp rides along too, so the row can say when to look",
      rows["old1"]["received"] == "2026-10-01 08:14", rows["old1"])
check("the old notices list is no longer served", "notices" not in d, list(d))

# --- /api/triage queues, and the page shows the level at once ---
r = cli.post("/api/triage", json={"id": "new1", "level": "urgent"})
check("filing a live todo succeeds", r.status_code == 200 and r.get_json()["ok"],
      r.get_json())
check("the request lands in the triage file, nowhere else",
      read(tg.TRIAGE_PATH)["levels"] == {"new1": "urgent"}, read(tg.TRIAGE_PATH))
check("the triage file carries a rev like the other request files",
      bool(read(tg.TRIAGE_PATH).get("rev")), read(tg.TRIAGE_PATH))
check("state.json is untouched, so it keeps one writer",
      "priority" not in by_id(read(tg.STATE_PATH)["todos"])["new1"])
d = cli.get("/api/todos").get_json()
check("the queued level is served at once, so the row leaves 待分類 on the click",
      by_id(d["todos"])["new1"]["priority"] == "urgent", by_id(d["todos"])["new1"])
check("and marked pending until the round writes it",
      by_id(d["todos"])["new1"]["pendingLevel"] is True, by_id(d["todos"])["new1"])

r = cli.post("/api/triage", json={"id": "old1", "level": ""})
check("重新分類 queues an empty level", r.get_json()["ok"] is True, r.get_json())
check("and is added beside the first request, not substituted",
      read(tg.TRIAGE_PATH)["levels"] == {"new1": "urgent", "old1": ""},
      read(tg.TRIAGE_PATH))
check("the row is served back in 待分類 at once",
      by_id(cli.get("/api/todos").get_json()["todos"])["old1"]["priority"] == "")

r = cli.post("/api/triage", json={"id": "new2", "level": "someday"})
check("a level the state machine does not know is refused", r.status_code == 400,
      r.get_json())
check("and it is not queued", "new2" not in read(tg.TRIAGE_PATH)["levels"])
r = cli.post("/api/triage", json={"id": "archived-already", "level": "normal"})
check("filing a todo that is gone is refused",
      r.status_code == 409 and r.get_json()["gone"] is True, r.get_json())
check("and nothing is queued for it",
      "archived-already" not in read(tg.TRIAGE_PATH)["levels"])
r = cli.post("/api/triage", json={"level": "normal"})
check("filing with no id is a bad request", r.status_code == 400, r.status_code)

# --- consumed requests are pruned, or the files grow forever ---
# The state machine has applied both levels, and imp1 has left the list.
write(tg.STATE_PATH, {"roundSeq": 13, "todos": [
    {"id": "new1", "subject": "new mail", "priority": "urgent"},
    {"id": "old1", "subject": "older todo"},
    {"id": "new2", "subject": "second new mail"}]})
write(tg.TRIAGE_PATH, {"levels": {"new1": "urgent", "old1": "", "imp1": "normal",
                                  "new2": "important"}})
cli.get("/api/todos")
check("a level drops once state.json carries it, and so does one for a todo that left",
      read(tg.TRIAGE_PATH)["levels"] == {"new2": "important"}, read(tg.TRIAGE_PATH))

write(tg.CHECKED_PATH, {"checkedIds": ["new1", "already-archived"]})
write(tg.RESTORE_PATH, {"restoreIds": ["not-in-archive"]})
write(tg.ARCHIVE_PATH, {"archived": []})
cli.get("/api/todos")
check("a tick for a todo that is gone is pruned",
      read(tg.CHECKED_PATH)["checkedIds"] == ["new1"], read(tg.CHECKED_PATH))
check("a restore for something no longer archived is pruned",
      read(tg.RESTORE_PATH)["restoreIds"] == [], read(tg.RESTORE_PATH))

# --- /api/check, which 待分類's 封存 button also uses ---
r = cli.post("/api/check", json={"id": "new2", "checked": True})
check("ticking a live todo succeeds", r.get_json()["ok"] is True, r.get_json())
check("the tick is on disk before the response returns",
      "new2" in read(tg.CHECKED_PATH)["checkedIds"], read(tg.CHECKED_PATH))
r = cli.post("/api/check", json={"id": "new2", "checked": False})
check("un-ticking removes it", read(tg.CHECKED_PATH)["checkedIds"] == ["new1"],
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

# --- /api/follow and the 追蹤中 fields /api/todos serves ---
clear(tg.TRIAGE_PATH, tg.CHECKED_PATH, tg.FOLLOW_PATH)
write(tg.STATE_PATH, {"roundSeq": 20, "todos": [
    {"id": "w1", "subject": "waiting two days", "priority": "normal",
     "followSince": NOW - int(2.5 * DAY)},
    {"id": "w2", "subject": "waiting three days", "priority": "urgent",
     "followSince": NOW - 3 * DAY - 60},
    {"id": "w3", "subject": "bad stamp", "priority": "normal", "followSince": "soon"},
    {"id": "f1", "subject": "filed", "priority": "important"},
    {"id": "u1", "subject": "unfiled"}]})
rows = by_id(cli.get("/api/todos").get_json()["todos"])
check("a stamped filed todo is served as following, with whole days",
      (rows["w1"]["following"], rows["w1"]["followDays"], rows["w1"]["followAlert"])
      == (True, 2, False), rows["w1"])
check("three days turns it red", rows["w2"]["followAlert"] is True, rows["w2"])
check("an unusable stamp reads as fresh, never as overdue",
      (rows["w3"]["following"], rows["w3"]["followDays"]) == (True, 0), rows["w3"])
check("an unstamped todo is not following", rows["f1"]["following"] is False, rows["f1"])

r = cli.post("/api/follow", json={"id": "f1", "follow": True})
check("a filed todo can be followed", r.get_json()["ok"] is True, r.get_json())
check("the request lands in its own file",
      read(tg.FOLLOW_PATH)["follow"] == {"f1": True} and bool(read(tg.FOLLOW_PATH).get("rev")),
      read(tg.FOLLOW_PATH))
rows = by_id(cli.get("/api/todos").get_json()["todos"])
check("a queued follow moves the row at once, with no age until the round stamps it",
      (rows["f1"]["following"], rows["f1"]["pendingFollow"], rows["f1"]["followDays"])
      == (True, True, 0), rows["f1"])
r = cli.post("/api/follow", json={"id": "w2", "follow": False})
rows = by_id(cli.get("/api/todos").get_json()["todos"])
check("回到待辦 moves it back at once and keeps its level",
      (rows["w2"]["following"], rows["w2"]["priority"], rows["w2"]["followAlert"])
      == (False, "urgent", False), rows["w2"])
r = cli.post("/api/follow", json={"id": "u1", "follow": True})
check("an unfiled todo is refused", r.status_code == 409, r.get_json())
cli.post("/api/triage", json={"id": "u1", "level": "normal"})
r = cli.post("/api/follow", json={"id": "u1", "follow": True})
check("but one with a queued level is accepted", r.get_json()["ok"] is True, r.get_json())
cli.post("/api/triage", json={"id": "u1", "level": ""})
check("重新分類 withdraws a queued follow, or it would fire once the todo is re-filed",
      "u1" not in read(tg.FOLLOW_PATH)["follow"], read(tg.FOLLOW_PATH))
cli.post("/api/triage", json={"id": "u1", "level": "normal"})
cli.post("/api/follow", json={"id": "u1", "follow": True})
r = cli.post("/api/follow", json={"id": "f1", "follow": "yes"})
check("a non-boolean is a bad request", r.status_code == 400, r.status_code)
r = cli.post("/api/follow", json={"id": "nope", "follow": True})
check("following a todo that is gone is refused",
      r.status_code == 409 and r.get_json()["gone"] is True, r.get_json())

# --- 延後提醒 on a red row ---
clear(tg.FOLLOW_PATH)
write(tg.STATE_PATH, {"roundSeq": 22, "todos": [
    {"id": "red", "subject": "red", "priority": "normal", "followSince": NOW - 5 * DAY},
    {"id": "later", "subject": "postponed", "priority": "normal",
     "followSince": NOW - 5 * DAY, "followRemindAt": NOW + int(1.5 * DAY)},
    {"id": "due", "subject": "postponement ran out", "priority": "normal",
     "followSince": NOW - 9 * DAY, "followRemindAt": NOW - 60},
    {"id": "plain", "subject": "filed only", "priority": "normal"}]})
rows = by_id(cli.get("/api/todos").get_json()["todos"])
check("a future reminder keeps an old row out of red, and says when",
      (rows["later"]["followAlert"], rows["later"]["remindIn"], rows["later"]["followDays"])
      == (False, 2, 5), rows["later"])
check("a reminder that came due turns it red again",
      rows["due"]["followAlert"] is True and rows["due"]["remindIn"] == 0, rows["due"])
for bad in (0, 61, 2.5, True, "3"):
    r = cli.post("/api/remind", json={"id": "red", "days": bad})
    check(f"days={bad!r} is a bad request", r.status_code == 400, r.status_code)
r = cli.post("/api/remind", json={"id": "plain", "days": 3})
check("a row not in 追蹤中 cannot be postponed", r.status_code == 409, r.get_json())
before = int(time.time())
r = cli.post("/api/remind", json={"id": "red", "days": 4})
due_at = read(tg.FOLLOW_PATH)["follow"]["red"]
check("the due moment is computed at the click",
      before + 4 * DAY <= due_at <= int(time.time()) + 4 * DAY, due_at)
rows = by_id(cli.get("/api/todos").get_json()["todos"])
check("a queued postponement clears the red at once and is marked pending",
      (rows["red"]["followAlert"], rows["red"]["remindIn"], rows["red"]["pendingRemind"],
       rows["red"]["pendingFollow"], rows["red"]["following"])
      == (False, 4, True, False, True), rows["red"])
write(tg.STATE_PATH, {"roundSeq": 23, "todos": [
    {"id": "red", "subject": "red", "priority": "normal", "followSince": NOW - 5 * DAY,
     "followRemindAt": due_at}]})
cli.get("/api/todos")
check("a postponement drops once state.json carries it",
      read(tg.FOLLOW_PATH)["follow"] == {}, read(tg.FOLLOW_PATH))
clear(tg.FOLLOW_PATH)

# The round applied f1 and w2, and w1 left the list.
write(tg.STATE_PATH, {"roundSeq": 21, "todos": [
    {"id": "f1", "subject": "filed", "priority": "important", "followSince": NOW},
    {"id": "w2", "subject": "waiting three days", "priority": "urgent"},
    {"id": "u1", "subject": "unfiled"}]})
write(tg.FOLLOW_PATH, {"follow": {"f1": True, "w2": False, "w1": False, "u1": True}})
cli.get("/api/todos")
check("a follow drops once state.json carries it, and so does one for a todo that left",
      read(tg.FOLLOW_PATH)["follow"] == {"u1": True}, read(tg.FOLLOW_PATH))

# --- a missing state.json must serve an empty list, not a 500 ---
clear(tg.STATE_PATH, tg.ARCHIVE_PATH, tg.CHECKED_PATH, tg.RESTORE_PATH, tg.TRIAGE_PATH,
      tg.FOLLOW_PATH)
d = cli.get("/api/todos").get_json()
check("a first run with no state file still serves", d["todos"] == [], d)
check("and no request file was created just by reading",
      not os.path.exists(tg.TRIAGE_PATH) and not os.path.exists(tg.CHECKED_PATH))


# --- a subject links to the original mail once the background lookup ends ---
def settle():
    """Wait out the lookup a request started, so the next request sees it."""
    if tg._link_job is not None:
        tg._link_job.join(5)


def reset_links(lookup):
    """Start over as a new process would, reloading the cache file on first use."""
    settle()
    tg._links = None
    tg._given_up.clear()
    tg._lookup_links = lookup


LINK = "https://mail.google.com/mail/?authuser=me@gmail.example#all/1a0e"
TODOS = [{"id": "g1", "subject": "gmail origin"},
         {"id": "x1", "subject": "exchange origin"},
         {"subject": "no id"}]
tg.LINK_RETRY_DELAY = 0
asked = []


def answering(ids):
    asked.append(list(ids))
    return {"g1": LINK, "x1": None}


clear(tg.LINKS_PATH)
reset_links(answering)
write(tg.STATE_PATH, {"todos": TODOS})
d = cli.get("/api/todos").get_json()
settle()
check("the first poll serves at once, with no link yet and a lookup pending",
      d["linksPending"] is True and all(r["link"] == "" for r in d["todos"]), d)
check("the lookup is asked only for usable ids", asked == [["g1", "x1"]], asked)
d = cli.get("/api/todos").get_json()
rows = by_id(d["todos"])
check("the next poll carries the link, and none where the lookup had none",
      (rows["g1"]["link"], rows["x1"]["link"], d["linksPending"]) == (LINK, "", False), d)
check("an id already answered, None included, is never looked up again",
      len(asked) == 1, asked)
check("both answers are saved, None included, under the current version",
      read(tg.LINKS_PATH) == {"version": tg.LINKS_VERSION, "links": {"g1": LINK, "x1": None}},
      read(tg.LINKS_PATH))

reset_links(answering)
d = cli.get("/api/todos").get_json()
check("a new process serves the saved link on its first poll, with no lookup at all",
      by_id(d["todos"])["g1"]["link"] == LINK and d["linksPending"] is False
      and len(asked) == 1, (asked, d))

write(tg.STATE_PATH, {"todos": TODOS[:1]})
cli.get("/api/todos")
check("a todo that left the list is dropped from the saved cache",
      read(tg.LINKS_PATH)["links"] == {"g1": LINK}, read(tg.LINKS_PATH))

asked.clear()
write(tg.LINKS_PATH, {"links": {"g1": LINK, "x1": None}})
reset_links(answering)
write(tg.STATE_PATH, {"todos": TODOS})
cli.get("/api/todos")
settle()
check("a file from another version is ignored, so every todo is looked up again",
      asked == [["g1", "x1"]], asked)

asked.clear()
write(tg.LINKS_PATH, {"version": tg.LINKS_VERSION,
                      "links": {"g1": "javascript:alert(1)", "x1": None}})
reset_links(answering)
write(tg.STATE_PATH, {"todos": TODOS})
d = cli.get("/api/todos").get_json()
settle()
check("a saved value that is not a Gmail link is never served, and is looked up again",
      by_id(d["todos"])["g1"]["link"] == "" and asked == [["g1"]], (asked, d))

calls = []


def broken(ids):
    calls.append(list(ids))
    raise OSError("imap down")


clear(tg.LINKS_PATH)
reset_links(broken)
cli.get("/api/todos")
settle()
d = cli.get("/api/todos").get_json()
check("a failed lookup is retried once, then those todos stay plain with nothing pending",
      calls == [["g1", "x1"], ["g1", "x1"]] and all(r["link"] == "" for r in d["todos"])
      and d["linksPending"] is False, (calls, d))
check("ids given up on are not saved, so the next launch tries them again",
      read(tg.LINKS_PATH) is None, read(tg.LINKS_PATH))
write(tg.STATE_PATH, {"todos": TODOS + [{"id": "n1", "subject": "arrived later"}]})
cli.get("/api/todos")
settle()
check("a todo arriving later gets its own two tries, and the given-up ones get none",
      calls[2:] == [["n1"], ["n1"]], calls)

flaky_calls = []


def flaky(ids):
    flaky_calls.append(list(ids))
    if len(flaky_calls) == 1:
        raise OSError("blip")
    return {"g1": LINK, "x1": None}


reset_links(flaky)
write(tg.STATE_PATH, {"todos": TODOS})
cli.get("/api/todos")
settle()
d = cli.get("/api/todos").get_json()
check("a lookup that fails once and then succeeds still links",
      len(flaky_calls) == 2 and by_id(d["todos"])["g1"]["link"] == LINK, (flaky_calls, d))

QUERY = 'Subject:"Co-op: Access Granted!" AND From:a@b.example AND received:9/14/2026'
SEARCH = tg.OUTLOOK_SEARCH + urllib.parse.quote(QUERY, safe="")
clear(tg.LINKS_PATH)
reset_links(lambda ids: {"g1": LINK, "x1": SEARCH})
cli.get("/api/todos")
settle()
rows = by_id(cli.get("/api/todos").get_json()["todos"])
check("an Outlook search row opens the bare mailbox and carries the query to copy",
      (rows["x1"]["link"], rows["x1"]["outlookQuery"]) == (tg.OUTLOOK_WEB, QUERY), rows["x1"])
check("a Gmail row carries no query", rows["g1"]["outlookQuery"] == "", rows["g1"])
reset_links(lambda ids: {})
check("an Outlook search survives a reload from the cache file",
      by_id(cli.get("/api/todos").get_json()["todos"])["x1"]["outlookQuery"] == QUERY)
reset_links(lambda ids: {})
clear(tg.STATE_PATH, tg.LINKS_PATH)

# --- the page: 待分類 above 待辦清單, and only 待分類 offers the levels ---
page = tg.index()
check("both sections are in the page, 待分類 first",
      "待分類" in page and "待辦清單" in page and page.index("待分類") < page.index("待辦清單"))
check("the retired section names are gone",
      not any(name in page for name in ("重要事項", "這次新增", "之前的")))
check("the level buttons post to the triage endpoint", "'/api/triage'" in page)
check("the four choices are all there", all(f"'{w}'" in page for w in ("緊急", "重要", "普通"))
      and "button('封存'" in page)
check("the level buttons are built in one place only, which is 待分類",
      page.count("for(const [lv, name] of LEVELS)") == 1)
check("a filed row offers 重新分類 instead", "button('重新分類'" in page)
check("追蹤中 sits between 待辦清單 and 已封存",
      page.index("待辦清單 <span") < page.index("追蹤中 <span") < page.index("已封存 <span"))
check("轉追蹤 and 回到待辦 both post to the follow endpoint",
      "button('轉追蹤'" in page and "button('回到待辦'" in page
      and page.count("'/api/follow'") == 2)
check("the red threshold is the server's constant, not a second copy",
      "__FOLLOW_DAYS__" not in page
      and f"const FOLLOW_DAYS = {tg.FOLLOW_ALERT_DAYS};" in page)
check("and so is the postponement cap",
      "__MAX_REMIND__" not in page and f"const MAX_REMIND = {tg.MAX_REMIND_DAYS};" in page)
check("the postpone control posts to the remind endpoint", "'/api/remind'" in page)
check("an alias mailbox is labelled rather than left looking unresolved",
      "直收" in page and "includes('@')" in page)
check("every row kind renders the source line through one function",
      page.count("appendSource(") == 3, page.count("appendSource("))
check("an Outlook row copies its query synchronously in the click itself",
      "copyNow(t.outlookQuery)" in page and "document.execCommand('copy')" in page)
check("the quick re-poll interval is the server's constant",
      "__LINK_POLL_MS__" not in page and f"setTimeout(refresh, {tg.LINK_POLL_MS})" in page)

shutil.rmtree(work, ignore_errors=True)
print()
print("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails))
sys.exit(1 if fails else 0)
