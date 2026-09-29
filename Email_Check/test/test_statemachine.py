"""End-to-end scenario tests for the state machine, run against a temp copy."""
import importlib.util, json, os, shutil, sys, tempfile

# One level up, because the tests live in test/ and the code does not.
CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(CODE, "statemachine.py")
work = tempfile.mkdtemp(prefix="smtest.")
shutil.copy(SRC, os.path.join(work, "statemachine.py"))
spec = importlib.util.spec_from_file_location("sm", os.path.join(work, "statemachine.py"))
sm = importlib.util.module_from_spec(spec); spec.loader.exec_module(sm)

_real_save = sm.State.save


def _test_save(self):
    """Let a hand-built State overwrite, since it never had a rev to match."""
    if self.rev is None and os.path.exists(sm.STATE_PATH):
        os.unlink(sm.STATE_PATH)
    return _real_save(self)


sm.State.save = _test_save
_real_load = sm.State.load_or_init

fails = []


def _clear(*paths):
    for p in paths:
        if os.path.exists(p):
            os.unlink(p)
def check(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  " + str(extra)) if extra else ""))
    if not cond: fails.append(label)

# --- Coverage: bisection provably covers a saturated window ---
cov = sm.Coverage(1000, [])
cov.extend_to(2000)
check("extend_to queues [horizon, scanEnd]", cov.intervals == [[1000, 2000]], cov.intervals)
check("covered_through is the lowest unscanned lo", cov.covered_through == 1000)

cov.bisect(1000, 2000)
check("bisect splits in two", cov.intervals == [[1000, 1500], [1500, 2000]], cov.intervals)
check("newest returns the upper half", cov.newest([]) == [1500, 2000], cov.newest([]))
check("gap keeps covered_through pinned", cov.covered_through == 1000)

cov.retire(1500, 2000)
check("retiring the newer half leaves the gap", cov.intervals == [[1000, 1500]], cov.intervals)
check("covered_through still pinned at the gap", cov.covered_through == 1000,
      "regression guard: an unscanned interval must never fall under the watermark")
cov.retire(1000, 1500)
check("all retired -> covered_through jumps to horizon", cov.covered_through == 2000)

# --- single-second saturation cannot bisect, must report stuck ---
c2 = sm.Coverage(0, [[500, 501]])
check("cannot bisect a one-second window", c2.bisect(500, 501) is False)

# --- newest-first ordering means catch-up never starves new mail ---
c3 = sm.Coverage(9000, [[1000, 2000], [8000, 9000]])
check("newest-first picks the recent interval", c3.newest([]) == [8000, 9000], c3.newest([]))
check("skip list is honoured", c3.newest([[8000, 9000]]) == [1000, 2000])

# --- firstDeferredRound survives a re-defer ---
sm.STATE_PATH = os.path.join(work, "state.json")
sm.ROUND_PATH = os.path.join(work, "round.json")
sm.PROGRESS_PATH = os.path.join(work, "round-progress.json")
sm.HERE = work
# The scenarios below run rounds back to back within milliseconds, which the
# live-round guard would read as overlap. Its own section turns it back on.
LIVE_ROUND_REAL = sm.LIVE_ROUND_SECONDS
sm.LIVE_ROUND_SECONDS = 0

st = sm.State({"horizon": 100, "roundSeq": 5,
               "pendingJudge": [{"id": "a", "firstDeferredRound": 2}]})
st.save()
sm.Progress(200).save()
json.dump({"defer": [{"id": "a", "subject": "x"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
after = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("firstDeferredRound preserved on re-defer",
      after["pendingJudge"][0]["firstDeferredRound"] == 2,
      after["pendingJudge"])

# --- overdue items are surfaced, never silently dropped ---
st2 = sm.State({"horizon": 100, "roundSeq": 9,
                "pendingJudge": [{"id": "old", "firstDeferredRound": 2},
                                 {"id": "new", "firstDeferredRound": 9}]})
d = st2.split_debt()
check("overdue item surfaced", [x["id"] for x in d["judgeOverdue"]] == ["old"], d["judgeOverdue"])
check("fresh item stays in judgeNow", [x["id"] for x in d["judgeNow"]] == ["new"])
check("no read budget is advertised", [k for k in d if "udget" in k] == [], list(d))

# --- notified wins over failed for the same id ---
st3 = sm.State({"horizon": 100, "roundSeq": 1})
st3.save(); sm.Progress(200).save()
json.dump({"notifiedIds": ["m1"], "failedNotify": [{"id": "m1", "summary": "s"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a3 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("successfully notified id not left in pendingNotify", a3["pendingNotify"] == [], a3["pendingNotify"])
check("notified id recorded", "m1" in a3["notifiedIds"])

# --- the whole judge queue is handed out, and unreported debt still survives ---
# No round-level read cap, so every fresh item must be visible; keep-by-default
# still matters, since a round can die after reporting only part of what it
# judged, and wholesale-replacing the queue at commit would drop the rest.
st4 = sm.State({"horizon": 100, "roundSeq": 7,
                "pendingJudge": [{"id": "old-A", "firstDeferredRound": 7},
                                 {"id": "old-B", "firstDeferredRound": 7},
                                 {"id": "old-C", "firstDeferredRound": 7}]})
st4.save(); sm.Progress(200).save()
shown = [x["id"] for x in st4.split_debt()["judgeNow"]]
check("every fresh item is handed out", shown == ["old-A", "old-B", "old-C"], shown)
# A round that crashed mid-report only re-defers part of what it was shown.
json.dump({"defer": [{"id": "old-A"}, {"id": "old-B"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a4 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
ids4 = sorted(x["id"] for x in a4["pendingJudge"])
check("judge debt left off the report survives commit", ids4 == ["old-A", "old-B", "old-C"], ids4)

# --- judgedIds is how an item leaves the judge queue when it was not
# --- promoted to the notify queue by being reported important ---
st5 = sm.State({"horizon": 100, "roundSeq": 8,
                "pendingJudge": [{"id": "j1", "firstDeferredRound": 8},
                                 {"id": "j2", "firstDeferredRound": 8}]})
st5.save(); sm.Progress(200).save()
json.dump({"judgedIds": ["j1"]}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a5 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
ids5 = sorted(x["id"] for x in a5["pendingJudge"])
check("judged item removed, unjudged one kept", ids5 == ["j2"], ids5)

# --- an omitted pendingNotify entry must not vanish ---
st6 = sm.State({"horizon": 100, "roundSeq": 9,
                "pendingNotify": [{"id": "retry-X", "summary": "x"},
                                  {"id": "retry-Y", "summary": "y"}]})
st6.save(); sm.Progress(200).save()
json.dump({"notifiedIds": ["retry-X"]}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a6 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
ids6 = sorted(x["id"] for x in a6["pendingNotify"])
check("un-reported notify debt survives", ids6 == ["retry-Y"], ids6)

# --- a contradictory report resolves toward keeping the item ---
st7 = sm.State({"horizon": 100, "roundSeq": 4,
                "pendingJudge": [{"id": "c1", "firstDeferredRound": 3,
                                  "subject": "old-subj"}]})
st7.save(); sm.Progress(200).save()
json.dump({"judgedIds": ["c1"], "defer": [{"id": "c1", "subject": "new-subj"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a7 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("defer outranks judgedIds for the same id",
      [x["id"] for x in a7["pendingJudge"]] == ["c1"], a7["pendingJudge"])

# --- a partial re-defer must not erase stored context ---
st8 = sm.State({"horizon": 100, "roundSeq": 4,
                "pendingJudge": [{"id": "y", "firstDeferredRound": 3,
                                  "from": "hr@acme.com", "subject": "Interview slot",
                                  "snippet": "Are you free Thursday 3pm?"}]})
st8.save(); sm.Progress(200).save()
json.dump({"defer": [{"id": "y"}]}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a8 = json.load(open(sm.STATE_PATH, encoding="utf-8"))["pendingJudge"][0]
check("partial re-defer keeps from/subject/snippet",
      a8.get("from") == "hr@acme.com" and a8.get("snippet", "").startswith("Are you"), a8)
check("partial re-defer keeps the original wait clock", a8["firstDeferredRound"] == 3)

# --- a re-report with new content replaces the old value, single entry ---
st9 = sm.State({"horizon": 100, "roundSeq": 4,
                "pendingNotify": [{"id": "z", "summary": "old summary, stale"}]})
st9.save(); sm.Progress(200).save()
json.dump({"failedNotify": [{"id": "z", "summary": "new summary, this round"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a9 = json.load(open(sm.STATE_PATH, encoding="utf-8"))["pendingNotify"]
check("re-reported notify entry stays single", len(a9) == 1, a9)
check("re-reported notify entry takes the new summary",
      a9[0]["summary"] == "new summary, this round", a9)

# --- an empty round.json must leave both queues untouched ---
st10 = sm.State({"horizon": 100, "roundSeq": 4,
                 "pendingNotify": [{"id": "n1", "summary": "s"}],
                 "pendingJudge": [{"id": "j1", "firstDeferredRound": 4}]})
st10.save(); sm.Progress(200).save()
json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a10 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("empty findings preserve both queues",
      [x["id"] for x in a10["pendingNotify"]] == ["n1"] and
      [x["id"] for x in a10["pendingJudge"]] == ["j1"], a10)

# --- a legitimately blank field must stay blank, not vanish ---
# Treating "" as "field omitted" deleted the key outright, leaving nothing to
# fall back to on later rounds.
st11 = sm.State({"horizon": 100, "roundSeq": 5,
                 "pendingJudge": [{"id": "blank1", "firstDeferredRound": 5,
                                   "from": "a@b.com", "subject": "", "snippet": "hi"}]})
st11.save(); sm.Progress(200).save()
json.dump({"defer": [{"id": "blank1", "subject": ""}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a11 = json.load(open(sm.STATE_PATH, encoding="utf-8"))["pendingJudge"][0]
check("blank subject survives as an empty string", a11.get("subject") == "", a11)
check("blank re-defer keeps the other fields",
      a11.get("from") == "a@b.com" and a11.get("snippet") == "hi", a11)

# --- todo queue: upsert by id, user completion is never overwritten ---
sm.CHECKED_PATH = os.path.join(work, "tasks-checked.json")
sm.ARCHIVE_PATH = os.path.join(work, "tasks-archive.json")
# Every request path must be rebound, not just the two above. One was missed
# until task_list/ made the write fail; before then, a test below quietly wrote
# a real request file the next scheduled round would have consumed.
sm.TRIAGE_PATH = os.path.join(work, "tasks-triage.json")
sm.FOLLOW_PATH = os.path.join(work, "tasks-follow.json")

st12 = sm.State({"horizon": 100, "roundSeq": 6})
st12.reconcile_todos([{"id": "t1", "subject": "OA link", "action": "do the OA"}], set())
check("todo created with context", st12.todos[0]["subject"] == "OA link", st12.todos)
check("createdRound stamped", st12.todos[0]["createdRound"] == 6)

# The GUI owns these three fields; a park-queue replay must not clear them.
st12.todos[0]["checked"] = True
st12.todos[0]["checkedRound"] = 6
st12.reconcile_todos([{"id": "t1", "subject": "OA link", "checked": False}], set())
check("a replay cannot un-complete a todo", st12.todos[0]["checked"] is True, st12.todos)

# --- a tick is consumed, archived, and clears the park queues ---
st13 = sm.State({"horizon": 100, "roundSeq": 7,
                 "todos": [{"id": "d1", "subject": "done one"},
                           {"id": "d2", "subject": "still open"}],
                 "pendingNotify": [{"id": "d1", "summary": "s"}],
                 "pendingJudge": [{"id": "d1", "firstDeferredRound": 7}]})
st13.save(); sm.Progress(200).save()
json.dump({"checkedIds": ["d1"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a13 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("ticked todo removed", [t["id"] for t in a13["todos"]] == ["d2"], a13["todos"])
check("ticked id purged from pendingNotify", a13["pendingNotify"] == [], a13["pendingNotify"])
check("ticked id purged from pendingJudge", a13["pendingJudge"] == [], a13["pendingJudge"])
check("ticked id recorded as done", "d1" in a13["notifiedIds"], a13["notifiedIds"])
arch = json.load(open(sm.ARCHIVE_PATH, encoding="utf-8"))["archived"]
check("ticked todo archived not destroyed", [t["id"] for t in arch] == ["d1"], arch)

# --- an unticked todo is never touched by cleanup ---
st14 = sm.State({"horizon": 100, "roundSeq": 8,
                 "todos": [{"id": "k1", "subject": "keep me"}]})
st14.save(); sm.Progress(200).save()
json.dump({"checkedIds": []}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a14 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("unticked todo survives cleanup", [t["id"] for t in a14["todos"]] == ["k1"], a14["todos"])

# --- step delivers todos atomically with retiring the interval ---
# cmd_step retires coverage immediately, so a batch whose todos only landed at
# commit would be lost outright if the round died in between.
st15 = sm.State({"horizon": 3000, "intervals": [[1000, 2000]], "roundSeq": 9})
st15.save(); sm.Progress(2000).save()
json.dump({"todos": [{"id": "s1", "subject": "from a batch"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_step(type("A", (), {"failed": False, "lo": 1000, "hi": 2000, "count": 3})())
a15 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("step persisted the batch todo", [t["id"] for t in a15["todos"]] == ["s1"], a15["todos"])
check("step retired the interval in the same save", a15["intervals"] == [], a15["intervals"])

# --- a ticked id re-reported in the same round must not resurrect ---
# The tick, the batch report and the cleanup all touch this id in one round.
# This is the seam between GUI, step and commit, and nothing locked it before.
st16 = sm.State({"horizon": 100, "roundSeq": 10,
                 "todos": [{"id": "r1", "subject": "already done"}]})
st16.save(); sm.Progress(200).save()
json.dump({"checkedIds": ["r1"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
json.dump({"todos": [{"id": "r1", "subject": "already done", "action": "still here"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a16 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("a ticked id reported again does not come back", a16["todos"] == [], a16["todos"])

# --- id-less queue entries must neither merge nor disappear ---
# Two entries both keyed "None" used to collapse into one Frankenstein record,
# destroying one message's context for good.
st17 = sm.State({"horizon": 100, "roundSeq": 11})
st17.reconcile_judge([{"subject": "first, no id", "snippet": "a"},
                      {"subject": "second, no id", "snippet": "b"}], set())
subs = sorted(x.get("subject", "") for x in st17.pendingJudge)
check("two id-less judge entries stay separate",
      subs == ["first, no id", "second, no id"], st17.pendingJudge)

st17.reconcile_todos([{"subject": "todo without id"}], set())
check("an id-less todo is kept, not silently dropped",
      len(st17.todos) == 1, st17.todos)
check("orphan_count surfaces them", st17.orphan_count() == 3, st17.orphan_count())

st17.reconcile_notify([{"summary": "no id A"}, {"summary": "no id B"}], set())
check("two id-less notify entries stay separate",
      len(st17.pendingNotify) == 2, st17.pendingNotify)

# --- a tick for an id that is no longer a todo is a no-op ---
st18 = sm.State({"horizon": 100, "roundSeq": 12,
                 "todos": [{"id": "live1", "subject": "open"}]})
st18.save(); sm.Progress(200).save()
json.dump({"checkedIds": ["ghost"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
before_arch = os.path.exists(sm.ARCHIVE_PATH) and open(sm.ARCHIVE_PATH, encoding="utf-8").read()
sm.cmd_commit(None)
a18 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
after_arch = os.path.exists(sm.ARCHIVE_PATH) and open(sm.ARCHIVE_PATH, encoding="utf-8").read()
check("a stale tick leaves the live todo alone",
      [t["id"] for t in a18["todos"]] == ["live1"], a18["todos"])
check("a stale tick archives nothing", before_arch == after_arch)

# --- begin clears a dead round's findings ---
st19 = sm.State({"horizon": 100, "roundSeq": 13})
st19.save()
json.dump({"todos": [{"id": "stale-from-dead-round"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_begin(None)
check("begin deletes leftover round.json", not os.path.exists(sm.ROUND_PATH))

# --- important lands at step, and only a successful notify removes it ---
# This is the crash window: step retires the interval, so anything judged
# important must already be durable before that happens.
st20 = sm.State({"horizon": 3000, "intervals": [[1000, 2000]], "roundSeq": 14})
st20.save(); sm.Progress(2000).save()
json.dump({"important": [{"id": "imp1", "summary": "recruiter reply"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_step(type("A", (), {"failed": False, "lo": 1000, "hi": 2000, "count": 2})())
a20 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("step lands important into pendingNotify",
      [x["id"] for x in a20["pendingNotify"]] == ["imp1"], a20["pendingNotify"])
check("step retired the interval in the same save", a20["intervals"] == [])
check("summary is durable, not just the id",
      a20["pendingNotify"][0]["summary"] == "recruiter reply", a20["pendingNotify"])

# Crash before commit: the next round must still see it as debt to retry.
d20 = sm.State.load_or_init(9999)[0].split_debt()
check("an un-notified important item comes back as notifyNow",
      [x["id"] for x in d20["notifyNow"]] == ["imp1"], d20["notifyNow"])

# A successful notify is what clears it.
sm.Progress(2000).save()
json.dump({"notifiedIds": ["imp1"]}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a21 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("a successful notify clears pendingNotify", a21["pendingNotify"] == [], a21["pendingNotify"])
check("the notified id is recorded", "imp1" in a21["notifiedIds"])

# An important item that never becomes a todo still survives. This is the
# "personal correspondence" exception, important but deliberately not a todo.
st22 = sm.State({"horizon": 3000, "intervals": [[1000, 2000]], "roundSeq": 15})
st22.save(); sm.Progress(2000).save()
json.dump({"important": [{"id": "pers1", "summary": "friend asked something"}],
           "todos": []}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_step(type("A", (), {"failed": False, "lo": 1000, "hi": 2000, "count": 1})())
a22 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("an important non-todo is still durable",
      [x["id"] for x in a22["pendingNotify"]] == ["pers1"], a22["pendingNotify"])
check("it did not become a todo", a22["todos"] == [], a22["todos"])

# --- an unreadable round.json must keep the interval, not retire it ---
# Same failure family as "search failure is not zero results": flattening a bad
# read into "this batch found nothing" retires coverage over unrecorded mail.
st23 = sm.State({"horizon": 3000, "intervals": [[1000, 2000]], "roundSeq": 16})
st23.save(); sm.Progress(2000).save()
open(sm.ROUND_PATH, "w", encoding="utf-8").write("{ truncated")
rc = sm.cmd_step(type("A", (), {"failed": False, "lo": 1000, "hi": 2000, "count": 5})())
a23 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("a corrupt round.json aborts the step", rc == 2, rc)
check("the interval survives a corrupt round.json",
      a23["intervals"] == [[1000, 2000]], a23["intervals"])

# --- findings stamped for another round must not attach to this one ---
st24 = sm.State({"horizon": 3000, "intervals": [[1000, 2000]], "roundSeq": 17})
st24.save()
prog = sm.Progress(2000); prog.token = "round-A"; prog.save()
json.dump({"roundToken": "round-B", "todos": [{"id": "wrong-round"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
rc = sm.cmd_step(type("A", (), {"failed": False, "lo": 1000, "hi": 2000, "count": 5})())
a24 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("a foreign roundToken aborts the step", rc == 2, rc)
check("foreign findings are not applied", a24["todos"] == [], a24["todos"])
check("the interval survives a foreign token",
      a24["intervals"] == [[1000, 2000]], a24["intervals"])

# A matching token is accepted.
prog = sm.Progress(2000); prog.token = "round-A"; prog.save()
json.dump({"roundToken": "round-A", "todos": [{"id": "right-round"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_step(type("A", (), {"failed": False, "lo": 1000, "hi": 2000, "count": 5})())
a25 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("a matching roundToken is applied",
      [t["id"] for t in a25["todos"]] == ["right-round"], a25["todos"])

# --- an un-tick between read and save defers archiving ---
# The GUI has already told the user it saved, so honouring the un-tick beats
# archiving this round.
st26 = sm.State({"horizon": 100, "roundSeq": 18,
                 "todos": [{"id": "race1", "subject": "do not archive me"}]})
st26.save()
json.dump({"checkedIds": ["race1"], "rev": "v1"},
          open(sm.CHECKED_PATH, "w", encoding="utf-8"))

real_read = sm._read_ticks
calls = {"n": 0}


def flaky_read():
    """(ids, error, rev), matching _read_archive's shape."""
    calls["n"] += 1
    if calls["n"] == 1:
        return ({"race1"}, "", "v1")
    return set(), "", "v2"        # the user un-ticked in between


sm._read_ticks = flaky_read
done26, why26 = st26.consume_checked()
sm._read_ticks = real_read
check("a changed tick file defers the archive", done26 == [], done26)
check("a deferred archive is not reported as a fault", why26 == "", why26)
check("the un-ticked todo is still there",
      [t["id"] for t in st26.todos] == ["race1"], st26.todos)

# --- both GUI-file readers report failure with the same shape ---
# A third reader of a GUI-written file should have one obvious shape to copy,
# and an unreadable file must never look like "nothing there".
# This section rewrites two shared files, so it snapshots them first and
# restores them at the end. Later sections build on rows earlier ones
# archived; clearing them once made a failure look like a bug, not test
# interference.
_snap_arch = (open(sm.ARCHIVE_PATH, encoding="utf-8").read()
              if os.path.exists(sm.ARCHIVE_PATH) else None)
_snap_tick = (open(sm.CHECKED_PATH, encoding="utf-8").read()
              if os.path.exists(sm.CHECKED_PATH) else None)

_clear(sm.CHECKED_PATH)
check("an absent tick file is not a fault", sm._read_ticks() == (set(), "", None),
      sm._read_ticks())
open(sm.CHECKED_PATH, "w", encoding="utf-8").write("{ broken")
check("an unreadable tick file gives a reason",
      sm._read_ticks() == (set(), "tick file unreadable", None), sm._read_ticks())
_clear(sm.CHECKED_PATH, sm.ARCHIVE_PATH)
check("an absent archive is not a fault", sm._read_archive() == ([], "", None),
      sm._read_archive())
open(sm.ARCHIVE_PATH, "w", encoding="utf-8").write("{ broken")
check("an unreadable archive gives a reason",
      sm._read_archive() == ([], "archive unreadable", None), sm._read_archive())

# The archive's disk-rev read mirrors State._disk_rev, including the sentinel
# that can never equal a real rev.
check("an unreadable archive rev is the sentinel",
      sm._archive_disk_rev() == (True, sm.UNREADABLE_REV), sm._archive_disk_rev())
check("a write against the sentinel is refused",
      not sm._write_archive([], sm.UNREADABLE_REV))
_clear(sm.ARCHIVE_PATH)
check("an absent archive reports no rev", sm._archive_disk_rev() == (False, None),
      sm._archive_disk_rev())
check("writing to an absent archive expects no rev", sm._write_archive([], None))
_, _, seeded = sm._read_archive()
check("that write stamped a rev", bool(seeded), seeded)
check("a stale expectation is refused", not sm._write_archive([], "old-rev"))
# A legacy archive has no rev at all. _read_archive reports None for that and
# for "absent" alike, so the first write has to be allowed through to stamp one.
json.dump({"archived": [{"id": "legacy1"}]},
          open(sm.ARCHIVE_PATH, "w", encoding="utf-8"))
items, err, rev = sm._read_archive()
check("a legacy archive reads with no rev", (len(items), err, rev) == (1, "", None),
      (items, err, rev))
check("a legacy archive accepts its first write", sm._write_archive(items, rev))
check("that write stamps a rev, migrating the file",
      bool(sm._read_archive()[2]), sm._read_archive()[2])
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH)
for _p, _snap in ((sm.ARCHIVE_PATH, _snap_arch), (sm.CHECKED_PATH, _snap_tick)):
    if _snap is not None:
        open(_p, "w", encoding="utf-8").write(_snap)


def archived_ids():
    """Ids in tasks-archive.json. Other cases above already put rows here, so
    these checks count one id rather than compare the whole list."""
    with open(sm.ARCHIVE_PATH, encoding="utf-8") as fh:
        return [t["id"] for t in json.load(fh)["archived"]]


# The archive write happens before the re-read, so the deferred round already
# left a copy; only the id filter stops the retry from appending a second,
# which would inflate the GUI's archived count.
check("the deferred round still left a copy in the archive",
      archived_ids().count("race1") == 1, archived_ids())
json.dump({"checkedIds": ["race1"], "rev": "v9"},
          open(sm.CHECKED_PATH, "w", encoding="utf-8"))
done26b, _ = st26.consume_checked()
check("the retry archives it for real",
      [t["id"] for t in done26b] == ["race1"] and st26.todos == [], st26.todos)
check("but does not archive it twice",
      archived_ids().count("race1") == 1, archived_ids())

# --- a corrupt archive is a reported fault, never a traceback ---
# All three are valid JSON, so json.load succeeds; _read_archive's own
# isinstance guard (list of dicts) catches every shape before a row is ever
# touched. The try/except is a regression guard against that check loosening,
# since a row reaching _usable_id would raise uncaught with no JSON on stdout.
for shape in ('{"archived": "not a list"}', '{"archived": [1, 2, 3]}',
              '{"archived": {}}'):
    st_bad = sm.State({"horizon": 100, "roundSeq": 19,
                       "todos": [{"id": "safe1", "subject": "must survive"}]})
    json.dump({"checkedIds": ["safe1"], "rev": "v1"},
              open(sm.CHECKED_PATH, "w", encoding="utf-8"))
    open(sm.ARCHIVE_PATH, "w", encoding="utf-8").write(shape)
    try:
        done_bad, why_bad = st_bad.consume_checked()
        check("a corrupt archive is reported, not raised  " + shape,
              (done_bad, why_bad) == ([], "archive unreadable"),
              (done_bad, why_bad))
    except Exception as exc:
        check("a corrupt archive is reported, not raised  " + shape,
              False, type(exc).__name__ + ": " + str(exc))
    check("and the todo survives it  " + shape,
          [t["id"] for t in st_bad.todos] == ["safe1"], st_bad.todos)
os.unlink(sm.ARCHIVE_PATH)

# --- an archive that cannot be written must not remove the todo ---
st27 = sm.State({"horizon": 100, "roundSeq": 19,
                 "todos": [{"id": "keepme", "subject": "archive will fail"}]})
json.dump({"checkedIds": ["keepme"], "rev": "v1"},
          open(sm.CHECKED_PATH, "w", encoding="utf-8"))
real_atomic = sm._atomic_json


def boom(path, payload):
    raise OSError("disk full")


sm._atomic_json = boom
done27, why27 = st27.consume_checked()
sm._atomic_json = real_atomic
check("a failed archive returns nothing", done27 == [], done27)
check("a failed archive says why", why27 == "archive not writable", why27)
check("a failed archive leaves the todo in place",
      [t["id"] for t in st27.todos] == ["keepme"], st27.todos)

# --- the clock-skew overlap belongs to the interval, not to every sub-query ---
# Re-widening each sub-query kept a bisected one-second interval searching a
# fifteen-minute window, so it stayed saturated and was mislabelled as stuck.
sm.CONFIG_PATH = os.path.join(work, "query-config.json")
q = sm.build_query(2000, 2001)
check("build_query uses the bounds verbatim", "after:2000 before:2001" in q, q)

# --- sender exclusions come from config.json, never from the source ---
# An unreadable config widens the search (excludes nobody): the opposite
# default would silently drop that sender's mail while the config stays
# broken, and downstream cannot tell a filtered result from an empty one.
check("no config means no exclusion", q == "after:2000 before:2001", q)
with open(sm.CONFIG_PATH, "w", encoding="utf-8") as fh:
    json.dump({"excludedSenders": ["  a@x.com  ", "", 7, "b@y.com"]}, fh)
q_ex = sm.build_query(2000, 2001)
check("every configured sender is excluded, blanks and non-strings dropped",
      q_ex == "after:2000 before:2001 -from:a@x.com -from:b@y.com", q_ex)
with open(sm.CONFIG_PATH, "w", encoding="utf-8") as fh:
    fh.write("{ not json")
q_bad = sm.build_query(2000, 2001)
check("a malformed config excludes nobody", q_bad == "after:2000 before:2001", q_bad)

# --- label exclusions honour the tags the user applied in Gmail ---
# A tagged message is one the user has already decided about, so fetching it
# again every round only re-derives an answer that exists. Same safe default
# as the sender list: no config means no exclusion.
with open(sm.CONFIG_PATH, "w", encoding="utf-8") as fh:
    json.dump({"excludedLabels": ["ignore", "  ", 7, "old stuff"]}, fh)
q_lab = sm.build_query(2000, 2001)
check("every configured label is excluded, blanks and non-strings dropped",
      q_lab == 'after:2000 before:2001 -label:ignore -label:"old stuff"', q_lab)

# A multi-word label must be quoted. Unquoted, Gmail would end the operator at
# the space and read the remainder as a free-text term, which narrows the
# search rather than widening it, so the failure would hide mail.
check("a multi-word label is quoted", '-label:"old stuff"' in q_lab, q_lab)

with open(sm.CONFIG_PATH, "w", encoding="utf-8") as fh:
    json.dump({"excludedSenders": ["a@x.com"], "excludedLabels": ["ignore"]}, fh)
q_both = sm.build_query(2000, 2001)
check("senders and labels are both applied",
      q_both == "after:2000 before:2001 -from:a@x.com -label:ignore", q_both)

with open(sm.CONFIG_PATH, "w", encoding="utf-8") as fh:
    fh.write("{ not json")
check("a malformed config excludes no label",
      sm.build_query(2000, 2001) == "after:2000 before:2001", sm.build_query(2000, 2001))
os.unlink(sm.CONFIG_PATH)

cov = sm.Coverage(5000, [])
cov.extend_to(6000, sm.BOUNDARY_SLACK)
check("extend_to applies the slack once",
      cov.intervals == [[5000 - sm.BOUNDARY_SLACK, 6000]], cov.intervals)

# --- replaying the same id-less item must not accumulate copies ---
st28 = sm.State({"horizon": 100, "roundSeq": 20})
for _ in range(5):
    st28.reconcile_todos([{"subject": "no id, replayed"}], set())
check("an id-less todo does not multiply on replay", len(st28.todos) == 1, st28.todos)
for _ in range(3):
    st28.reconcile_judge([{"subject": "no id defer"}], set())
check("an id-less defer does not multiply on replay",
      len(st28.pendingJudge) == 1, st28.pendingJudge)

# --- config.json health: reported every round, alerts once per fault ---
# A broken config is a persistent state and the task runs many times a day, so
# alerting on the transition is what stops it becoming a stream of identical pushes,
# and reporting the status unconditionally is what keeps it visible in between.
sm.CONFIG_PATH = os.path.join(work, "config.json")
CFG = sm.CONFIG_PATH
emitted = []
real_emit = sm.emit
sm.emit = emitted.append


def begin_cfg(last):
    emitted.clear()
    sm.State({"horizon": 100, "roundSeq": 30, "lastConfigStatus": last}).save()
    sm.cmd_begin(None)
    return emitted[-1]["config"]


def commit_cfg(last, seen, findings=None):
    """lastConfigStatus after a commit. `seen` is what begin observed."""
    sm.State({"horizon": 100, "roundSeq": 30, "lastConfigStatus": last}).save()
    sm.Progress(200, config_seen=seen).save()
    json.dump(findings or {}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
    emitted.clear()
    sm.cmd_commit(None)
    return json.load(open(sm.STATE_PATH, encoding="utf-8"))["lastConfigStatus"]


c = begin_cfg("")
check("a missing config.json reports missing", c["status"] == "missing", c)
check("a never-observed fault alerts", c["alert"] is True, c)
c = begin_cfg("missing")
check("an already-reported fault does not alert again", c["alert"] is False, c)

sm.State({"horizon": 100, "roundSeq": 30}).save()
emitted.clear()
sm.cmd_begin(None)
check("begin does not consume the transition",
      json.load(open(sm.STATE_PATH, encoding="utf-8"))["lastConfigStatus"] == "",
      "only commit records it, so a failed push re-alerts next round")
check("commit records an acknowledged status",
      commit_cfg("", "missing", {"configAlerted": True}) == "missing")

open(CFG, "w", encoding="utf-8").write(
    json.dumps({"names": {"latin": ["A"]}, "calendarIcsUrls": ["https://x/a.ics"]}))
c = begin_cfg("missing")
check("a fixed config reports present", c["status"] == "present", c)
check("a healthy config never alerts", c["alert"] is False, c)
check("usable feeds are counted", c["calendarUrls"] == 1, c)
check("commit records the recovery", commit_cfg("missing", "present") == "present")

os.unlink(CFG)
check("a fault after a recovery alerts again", begin_cfg("present")["alert"] is True)

open(CFG, "w", encoding="utf-8").write("{ not json")
c = begin_cfg("missing")
check("unreadable JSON is malformed, not missing", c["status"] == "malformed", c)
check("one fault turning into another alerts", c["alert"] is True,
      "missing and malformed need different fixes, so the change is news")

open(CFG, "w", encoding="utf-8").write(json.dumps({"calendarIcsUrls": []}))
c = begin_cfg("")
check("a config with no names is incomplete", c["status"] == "incomplete", c)
check("an empty calendarIcsUrls is not a fault by itself",
      c["calendarUrls"] == 0 and c["alert"] is True,
      "the alert here is the missing names, not the empty feed list")

open(CFG, "w", encoding="utf-8").write(json.dumps({"names": {"han": ["X"]}}))
c = begin_cfg("")
check("names alone is enough to be present", c["status"] == "present", c)
check("no calendar urls still reports zero and stays quiet",
      c["calendarUrls"] == 0 and c["alert"] is False, c)

os.unlink(CFG)

# An owed alert follows the same keep-by-default rule as the mail queues. A
# push that failed while the round still reached commit must not silence the
# fault: that was the whole point of not recording the status in begin.
check("an unacknowledged alert is not consumed", commit_cfg("", "missing") == "",
      "otherwise a failed push silences the fault until it becomes another fault")
check("commit says the alert is still owed",
      emitted[-1]["configAlertStillOwed"] is True, emitted[-1])
check("an acknowledged alert is consumed",
      commit_cfg("", "missing", {"configAlerted": True}) == "missing")
check("commit says nothing is owed once acknowledged",
      emitted[-1]["configAlertStillOwed"] is False, emitted[-1])
check("an ack cannot consume an alert nobody was asked to send",
      commit_cfg("", "present", {"configAlerted": True}) == "",
      "begin saw a healthy config, so the fault commit reads was never announced")

# Fixed mid-round: there is nothing left to announce, so the round must not
# stay stuck owing an alert for a fault that no longer exists.
open(CFG, "w", encoding="utf-8").write(json.dumps({"names": {"latin": ["A"]}}))
check("a fault fixed mid-round is consumed without an ack",
      commit_cfg("", "missing") == "present")
os.unlink(CFG)

# config.json edited between begin and commit. The fault commit reads was never
# the one begin evaluated, so recording it would consume an alert the user never
# saw and leave the new fault permanently silent.
open(CFG, "w", encoding="utf-8").write("{ not json")
check("a fault that drifted into another fault is not consumed",
      commit_cfg("missing", "missing") == "missing",
      "next round compares malformed against missing and alerts")
check("a config that broke after begin is not consumed",
      commit_cfg("present", "present") == "present")
# An ack covers the fault begin announced, not whatever config.json says by the
# time commit runs. Deriving configAlertStillOwed from the result is what keeps
# the never-announced fault flagged here.
check("an ack does not cover a fault that appeared after it",
      commit_cfg("present", "missing", {"configAlerted": True}) == "present")
check("commit still reports the drifted fault as owed",
      emitted[-1]["configAlertStillOwed"] is True, emitted[-1])
os.unlink(CFG)

# begin's observation never depends on lastConfigStatus: only commit combines
# the two into a decision (see Progress.config_seen for why).
for last, expect in (("", "missing"), ("missing", "missing")):
    sm.State({"horizon": 100, "roundSeq": 30, "lastConfigStatus": last}).save()
    emitted.clear()
    sm.cmd_begin(None)
    check(f"begin records what it saw, lastConfigStatus={last!r}",
          sm.Progress.load().config_seen == expect, sm.Progress.load().config_seen)

# A round-progress.json written before this field existed must read as drift,
# so the alert stays owed. This is the same safe direction as lastConfigStatus.
json.dump({"scanEnd": 200, "searches": 0, "stuck": [], "token": "t"},
          open(sm.PROGRESS_PATH, "w", encoding="utf-8"))
check("an older round-progress.json has no observation",
      sm.Progress.load().config_seen == "")
check("a missing observation keeps the alert owed",
      commit_cfg("present", "") == "present",
      "a pre-upgrade round must not clear a fault it never evaluated")

sm.emit = real_emit

# --- a missing or unreadable round file aborts with a code, not a traceback ---
# Reachable through the race the known-limitations section already accepts:
# another task's commit calls Progress.clear() mid-round. Coverage must
# survive it, since retiring an interval means trusting round.json belongs
# to this round, unconfirmable without the token.
grabbed = []
prior_emit = sm.emit
sm.emit = grabbed.append


def step_once():
    return sm.cmd_step(type("A", (), {"failed": False, "lo": 1000,
                                      "hi": 2000, "count": 2})())


def wipe_round_file():
    if os.path.exists(sm.PROGRESS_PATH):
        os.unlink(sm.PROGRESS_PATH)


def write_round_file(text):
    open(sm.PROGRESS_PATH, "w", encoding="utf-8").write(text)


for label, setup in (("missing", wipe_round_file),
                     ("unparseable", lambda: write_round_file("{ broken")),
                     ("scanEnd-less", lambda: write_round_file('{"token": "t"}'))):
    sm.State({"horizon": 3000, "intervals": [[1000, 2000]], "roundSeq": 40}).save()
    setup()
    grabbed.clear()
    rc = step_once()
    kept = json.load(open(sm.STATE_PATH, encoding="utf-8"))["intervals"]
    check(f"step aborts on a {label} round file",
          rc == 2 and grabbed[-1]["status"] == "NO_ROUND_IN_PROGRESS", grabbed[-1])
    check(f"the interval survives a {label} round file",
          kept == [[1000, 2000]], kept)

# commit used to catch only FileNotFoundError, so a torn file was a traceback.
sm.State({"horizon": 100, "roundSeq": 40}).save()
write_round_file("{ broken")
grabbed.clear()
check("commit aborts on an unparseable round file",
      sm.cmd_commit(None) == 2
      and grabbed[-1]["status"] == "NO_ROUND_IN_PROGRESS", grabbed[-1])

# Progress.save is the writer that could produce the file above.
wipe_round_file()
sm.Progress(4242, config_seen="present").save()
check("a saved round file round-trips", sm.Progress.load().scan_end == 4242)
check("saving leaves no temp file behind",
      not [f for f in os.listdir(work) if f.startswith(".sm.")], os.listdir(work))

# --- an archive fault is surfaced, not silently indistinguishable from idle ---
# Ticked rows would otherwise never disappear with nothing anywhere saying why.
st_ab = sm.State({"horizon": 100, "roundSeq": 41,
                  "todos": [{"id": "t1", "subject": "tick me"}]})
open(sm.CHECKED_PATH, "w", encoding="utf-8").write("{ not json")
done_ab, why_ab = st_ab.consume_checked()
check("an unreadable tick file archives nothing", done_ab == [], done_ab)
check("and says why", why_ab == "tick file unreadable", why_ab)
check("the todo is left alone", [t["id"] for t in st_ab.todos] == ["t1"], st_ab.todos)

os.unlink(sm.CHECKED_PATH)
st_ab2 = sm.State({"horizon": 100, "roundSeq": 41,
                   "todos": [{"id": "t1", "subject": "nobody ticked me"}]})
check("no ticks at all is not a fault",
      st_ab2.consume_checked() == ([], ""), st_ab2.consume_checked())
check("the reason never lands on State, which is only what state.json holds",
      not hasattr(st_ab2, "archiveBlocked"))

sm.emit = prior_emit

# --- the todo list opens whenever 待分類 holds something ---
# Nothing leaves 待分類 on its own, so a round keeps opening the list until the
# user files what is there. A filed or id-less todo does not hold it open.
held = []
was_emit = sm.emit
sm.emit = held.append


def commit_open(todos):
    sm.State({"horizon": 100, "roundSeq": 50, "todos": todos, "triageSince": 1}).save()
    sm.Progress(200, config_seen="present").save()
    json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
    held.clear()
    sm.cmd_commit(None)
    return held[-1]


out = commit_open([{"id": "w1", "subject": "not filed yet", "createdRound": 40}])
check("an untriaged todo opens the list, even one from an older round",
      out["shouldOpenTodoList"] is True and out["untriaged"] == 1, out)
out = commit_open([{"id": "f1", "subject": "filed", "priority": "normal",
                    "createdRound": 50}])
check("a filed todo alone does not open it, new this round or not",
      out["shouldOpenTodoList"] is False and out["untriaged"] == 0
      and out["newTodosThisRound"] == 1, out)
out = commit_open([{"subject": "no id at all"}])
check("an id-less todo does not hold it open, since it cannot be filed",
      out["shouldOpenTodoList"] is False and out["untriaged"] == 0, out)
out = commit_open([])
check("no todos at all does not open it", out["shouldOpenTodoList"] is False)

sm.emit = was_emit

# --- a deadline is resolved to a full date once, at ingest ---
# Left bare, the year would be re-guessed on every render, so the same
# unfinished todo could drift from "just overdue" to "next year" by itself.
import datetime as _dt
TODAY = _dt.date(2026, 9, 9)
rd = lambda t: sm.resolve_deadline(t, TODAY)

check("a full date passes through", rd("2026-09-15") == "2026-09-15", rd("2026-09-15"))
check("an empty deadline stays empty", rd("") == "")
check("a bare MM-DD gains the current year", rd("09-15") == "2026-09-15", rd("09-15"))
check("a recently overdue bare date stays in this year",
      rd("08-30") == "2026-08-30", rd("08-30"))
check("a far-behind bare date wraps to next year",
      rd("01-05") == "2027-01-05", rd("01-05"))
check("slashes are accepted", rd("9/15") == "2026-09-15", rd("9/15"))
check("unparseable text is returned untouched", rd("ASAP") == "ASAP", rd("ASAP"))
check("an impossible date is returned untouched", rd("2026-02-31") == "2026-02-31",
      rd("2026-02-31"))
check("resolving is idempotent", rd(rd("09-15")) == rd("09-15"))

# The state machine must store the resolved form, so the viewer never re-guesses.
st29 = sm.State({"horizon": 100, "roundSeq": 21})
st29.reconcile_todos([{"id": "dl1", "subject": "s", "deadline": "09-15"}], set())
check("reconcile stores a resolved deadline",
      st29.todos[0]["deadline"].count("-") == 2
      and st29.todos[0]["deadline"].startswith("20"), st29.todos[0])

# --- compare-and-swap on state.json ---
# Atomic replace stops a half-written file; it does nothing about one round
# overwriting another's changes. Both scheduled tasks fire together when the
# app reopens after being closed past their slots.
sm.State.save = _real_save          # these tests need the real guard

if os.path.exists(sm.STATE_PATH):
    os.unlink(sm.STATE_PATH)
first = sm.State({"horizon": 500, "roundSeq": 1})
first.save()
rev1 = json.load(open(sm.STATE_PATH, encoding="utf-8"))["rev"]
check("save stamps a rev", bool(rev1), rev1)

# Two rounds load the same state; the second write must be refused.
a, _ = sm.State.load_or_init(9999)
b, _ = sm.State.load_or_init(9999)
b.roundSeq = 99
b.save()
rev2 = json.load(open(sm.STATE_PATH, encoding="utf-8"))["rev"]
check("a second save moves the rev", rev2 != rev1, (rev1, rev2))
a.roundSeq = 50
try:
    a.save()
    check("a stale save is refused", False, "no exception")
except sm.StaleStateError:
    check("a stale save is refused", True)
check("the stale round did not overwrite",
      json.load(open(sm.STATE_PATH, encoding="utf-8"))["roundSeq"] == 99)

# Repeated saves inside one round are fine: each adopts the rev it just wrote.
c, _ = sm.State.load_or_init(9999)
c.save(); c.save(); c.save()
check("consecutive saves in one round all succeed", True)

# A state.json written before revs existed carries none. Refusing it would mean
# no round could ever save again, which is how the real file wedged the task
# until a local run surfaced it.
os.unlink(sm.STATE_PATH)
json.dump({"horizon": 500, "roundSeq": 3},
          open(sm.STATE_PATH, "w", encoding="utf-8"))
legacy, was_first = sm.State.load_or_init(9999)
check("a legacy state file is not a first run", not was_first and legacy.rev is None)
legacy.save()
check("a legacy state file accepts its first write, stamping a rev",
      bool(json.load(open(sm.STATE_PATH, encoding="utf-8")).get("rev")),
      json.load(open(sm.STATE_PATH, encoding="utf-8")).get("rev"))
check("the migrated file keeps its contents",
      json.load(open(sm.STATE_PATH, encoding="utf-8"))["roundSeq"] == 3)

# A first run expects no file at all, so finding one means someone raced us.
os.unlink(sm.STATE_PATH)
fresh, was_first = sm.State.load_or_init(9999)
check("a missing file is a first run", was_first and fresh.rev is None)
other, _ = sm.State.load_or_init(9999), None   # nothing on disk yet
sm.State.save = _test_save
sm.State({"horizon": 1}).save()      # another task creates it meanwhile
sm.State.save = _real_save
try:
    fresh.save()
    check("a first run refuses to clobber a file that appeared", False, "no exception")
except sm.StaleStateError:
    check("a first run refuses to clobber a file that appeared", True)

# An unreadable file is not "unchanged" either.
open(sm.STATE_PATH, "w", encoding="utf-8").write("{ broken")
d = sm.State({"horizon": 1, "rev": "whatever"})   # any rev; the read will fail
try:
    d.save()
    check("an unreadable state blocks the write", False, "no exception")
except sm.StaleStateError:
    check("an unreadable state blocks the write", True)

# cmd_* must report the abort rather than raise.
os.unlink(sm.STATE_PATH)
sm.State.save = _test_save
sm.State({"horizon": 500, "roundSeq": 1}).save()
sm.State.save = _real_save
stale, _ = _real_load(9999)
mover, _ = _real_load(9999)          # move the rev underneath the stale round
mover.roundSeq = 7
mover.save()
sm.State.load_or_init = staticmethod(lambda now: (stale, False))
rc = sm.cmd_begin(None)
check("cmd_begin reports STATE_CHANGED_ABORT instead of raising", rc == 2, rc)

sm.State.load_or_init = _real_load
sm.State.save = _test_save          # back to the shim for anything after this

# --- a begin that loses the rev race leaves the winner's round files alone ---
# Two catch-up begins landing within milliseconds can both pass the live-round
# guard, which the harness keeps off here. The loser has to be stopped before
# it touches the files the winner reads next, or both rounds die.
def round_files():
    return tuple(open(p, encoding="utf-8").read() if os.path.exists(p) else None
                 for p in (sm.PROGRESS_PATH, sm.ROUND_PATH))


_clear(sm.STATE_PATH, sm.ROUND_PATH, sm.PROGRESS_PATH)
sm.State({"horizon": 500, "roundSeq": 1}).save()
loser_view, _ = _real_load(9999)
sm.cmd_begin(None)
json.dump({"todos": [{"id": "winner"}]}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
winner_files = round_files()
sm.State.load_or_init = staticmethod(lambda now: (loser_view, False))
rc = sm.cmd_begin(None)
sm.State.load_or_init = _real_load
check("the begin that read state before the winner saved aborts", rc == 2, rc)
check("and leaves the winner's progress and findings untouched",
      round_files() == winner_files, round_files())

def _read_arch():
    if not os.path.exists(sm.ARCHIVE_PATH):
        return []
    return json.load(open(sm.ARCHIVE_PATH, encoding="utf-8")).get("archived", [])


# --- restore from the archive, and the three-day purge ---
sm.RESTORE_PATH = os.path.join(work, "tasks-restore.json")
NOW = 1789000000


def _write_archive(items, rev="seed"):
    """Seed the archive. rev=None reproduces a file written before revs existed."""
    payload = {"archived": items}
    if rev is not None:
        payload["rev"] = rev
    json.dump(payload, open(sm.ARCHIVE_PATH, "w", encoding="utf-8"))


# A restore puts the todo back and clears the id from the done set, or the
# pipeline would treat it as settled and never notify it again.
_clear(sm.RESTORE_PATH)
_write_archive([{"id": "r1", "subject": "mis-ticked", "action": "do it",
                 "archivedAt": NOW}])
st30 = sm.State({"horizon": 100, "roundSeq": 30, "notifiedIds": ["r1"]})
json.dump({"restoreIds": ["r1"]}, open(sm.RESTORE_PATH, "w", encoding="utf-8"))
back, err = st30.consume_restores()
check("a restore returns the item", [b["id"] for b in back] == ["r1"], back)
check("the todo is back on the list", [t["id"] for t in st30.todos] == ["r1"], st30.todos)
check("the restored id leaves the done set", "r1" not in st30.notifiedIds,
      st30.notifiedIds)
check("archivedAt is stripped on the way back",
      "archivedAt" not in st30.todos[0], st30.todos[0])
check("the archive no longer holds it", _read_arch() == [], _read_arch())
check("no error is reported", err == "", err)

# Restoring twice must not duplicate the todo.
_write_archive([{"id": "r1", "subject": "mis-ticked", "archivedAt": NOW}])
back2, _ = st30.consume_restores()
check("a second restore does not duplicate", len(st30.todos) == 1, st30.todos)

# An id that is no longer archived is simply nothing to do.
_write_archive([])
json.dump({"restoreIds": ["ghost"]}, open(sm.RESTORE_PATH, "w", encoding="utf-8"))
st31 = sm.State({"horizon": 100, "roundSeq": 31})
back3, err3 = st31.consume_restores()
check("restoring a purged id is a no-op", back3 == [] and st31.todos == [], back3)

# An unreadable request file must be reported, never read as "restore nothing".
open(sm.RESTORE_PATH, "w", encoding="utf-8").write("{ broken")
_, err4 = sm.State({"horizon": 100}).consume_restores()
check("an unreadable restore file says why", err4 == "restore file unreadable", err4)
_clear(sm.RESTORE_PATH)

# The purge drops entries past the TTL and keeps the rest.
st32 = sm.State({"horizon": 100, "roundSeq": 32})
_write_archive([
    {"id": "old1", "subject": "four days old", "archivedAt": NOW - 4 * 86400},
    {"id": "new1", "subject": "one day old", "archivedAt": NOW - 86400},
])
dropped = st32.purge_archive(NOW)
kept = [a["id"] for a in _read_arch()]
check("an entry past three days is purged", dropped == 1, dropped)
check("a fresher entry survives", kept == ["new1"], kept)

# An entry with no timestamp gets stamped, not purged: anything archived before
# the field existed still deserves its full three days.
_write_archive([{"id": "nostamp", "subject": "legacy entry"}])
dropped2 = st32.purge_archive(NOW)
after = _read_arch()
check("an unstamped entry is not purged", dropped2 == 0 and len(after) == 1, after)
check("it gets stamped instead", after[0].get("archivedAt") == NOW, after[0])
check("and is purged once it is genuinely old",
      st32.purge_archive(NOW + 4 * 86400) == 1)

# A garbage timestamp must not purge the entry either.
_write_archive([{"id": "bad", "subject": "junk stamp", "archivedAt": "yesterday"}])
check("a junk timestamp is re-stamped, not purged",
      st32.purge_archive(NOW) == 0, _read_arch())

# A ticked todo picks up archivedAt, which is what the TTL measures.
_clear(sm.ARCHIVE_PATH)
st33 = sm.State({"horizon": 100, "roundSeq": 33,
                 "todos": [{"id": "t33", "subject": "tick me"}]})
json.dump({"checkedIds": ["t33"], "rev": "v1"},
          open(sm.CHECKED_PATH, "w", encoding="utf-8"))
st33.consume_checked()
arch33 = _read_arch()
check("archiving stamps archivedAt", "archivedAt" in arch33[0], arch33)
_clear(sm.CHECKED_PATH)

import time as _time
time = _time


# --- the archive needs the same guard state.json has ---
# Found by execution during review: one round reads the archive, another round
# archives a freshly ticked todo, then the first round writes the list it
# computed from its stale read. The just-archived entry is erased, and it has
# already left the other round's todos, so nothing anywhere still holds it.
#
# A stale read is captured explicitly rather than raced for, which makes the
# interleaving deterministic. _write_archive reads the rev straight from the
# file, so pinning _read_archive here does not blind the guard.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH)
_write_archive([{"id": "P", "subject": "expired", "archivedAt": NOW - 4 * 86400}])
stale = sm._read_archive()

st34 = sm.State({"horizon": 100, "roundSeq": 34,
                 "todos": [{"id": "N", "subject": "just ticked"}]})
json.dump({"checkedIds": ["N"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
st34.consume_checked()
check("the other round really archived N",
      sorted(a["id"] for a in _read_arch()) == ["N", "P"], _read_arch())

_real_ra = sm._read_archive
sm._read_archive = lambda: stale
dropped34 = sm.State({"horizon": 100, "roundSeq": 34}).purge_archive(int(time.time()))
sm._read_archive = _real_ra
ids34 = sorted(a["id"] for a in _read_arch())
check("a stale purge cannot erase a concurrently archived todo", "N" in ids34, ids34)
check("the stale purge abandons its change instead", dropped34 == 0, dropped34)
check("the expired entry is still there for the next round to drop",
      "P" in ids34, ids34)

# The same guard on the restore path.
_clear(sm.CHECKED_PATH)
_write_archive([{"id": "R", "subject": "mis-ticked", "archivedAt": NOW}])
json.dump({"restoreIds": ["R"]}, open(sm.RESTORE_PATH, "w", encoding="utf-8"))
stale_b = sm._read_archive()

st35 = sm.State({"horizon": 100, "roundSeq": 35,
                 "todos": [{"id": "M", "subject": "just ticked"}]})
json.dump({"checkedIds": ["M"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
st35.consume_checked()

sm._read_archive = lambda: stale_b
st36 = sm.State({"horizon": 100, "roundSeq": 35})
back36, err36 = st36.consume_restores()
sm._read_archive = _real_ra
ids36 = sorted(a["id"] for a in _read_arch())
check("a stale restore cannot erase a concurrently archived todo", "M" in ids36, ids36)
check("the restore still puts the todo back on the list",
      [t["id"] for t in st36.todos] == ["R"], st36.todos)
check("and reports that the archive write was refused",
      err36 == "archive not writable", err36)
# Safe direction: the item is briefly both live and archived, which the next
# round's retry cleans up. Losing it would not be recoverable.
check("the entry stays archived until a clean round removes it", "R" in ids36, ids36)

# Sequential ordering, the normal case, must still work.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH)
_write_archive([{"id": "P2", "subject": "expired", "archivedAt": NOW - 4 * 86400}])
st37 = sm.State({"horizon": 100, "roundSeq": 37,
                 "todos": [{"id": "N2", "subject": "just ticked"}]})
json.dump({"checkedIds": ["N2"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
st37.consume_checked()
check("sequential purge drops only the expired entry",
      st37.purge_archive(int(time.time())) == 1
      and [a["id"] for a in _read_arch()] == ["N2"], _read_arch())
_clear(sm.CHECKED_PATH, sm.RESTORE_PATH)

# --- migration: state from before triage lands with everything as 普通 ---
# The user's call for existing mail. The old 重要事項 queue is folded into the
# list rather than dropped, since nothing else still shows those mails.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.TRIAGE_PATH)
nid = lambda q: sorted(str(x.get("id")) for x in q)

legacy = sm.State({"horizon": 100, "roundSeq": 40,
                   "todos": [{"id": "t1", "action": "pay the bill", "createdRound": 39},
                             {"id": "dup", "action": "the real next step"}],
                   "notices": [{"id": "n1", "subject": "a person wrote",
                                "summary": "reply by Friday", "noticedRound": 39,
                                "noticedAt": 5},
                               {"id": "dup", "summary": "pushed", "noticedRound": 39}]})
legacy.migrate_triage(NOW)
check("every todo already on the list becomes 普通",
      all(t["priority"] == "normal" for t in legacy.todos), legacy.todos)
check("a waiting notice joins the list instead of vanishing",
      nid(legacy.todos) == ["dup", "n1", "t1"], legacy.todos)
n1 = [t for t in legacy.todos if t["id"] == "n1"][0]
check("with the toast's summary as its action, and as 普通",
      n1["action"] == "reply by Friday" and n1["priority"] == "normal", n1)
check("and without the notice's own clock",
      "noticedRound" not in n1 and "noticedAt" not in n1, n1)
check("a notice for a message that is already a todo adds no second row",
      [t["action"] for t in legacy.todos if t["id"] == "dup"] == ["the real next step"],
      legacy.todos)
check("the migration is stamped", legacy.triageSince == NOW, legacy.triageSince)
legacy.todos[0]["priority"] = "urgent"
legacy.migrate_triage(NOW + 1)
check("and runs only once, so a later level is never reset",
      legacy.todos[0]["priority"] == "urgent" and legacy.triageSince == NOW)

# Through the real loader, which every subcommand goes through.
json.dump({"horizon": 100, "roundSeq": 40, "todos": [{"id": "t9", "action": "x"}],
           "notices": [{"id": "n9", "summary": "y", "noticedRound": 40}]},
          open(sm.STATE_PATH, "w", encoding="utf-8"))
loaded, _ = sm.State.load_or_init(NOW)
check("load_or_init migrates a file from before triage",
      nid(loaded.todos) == ["n9", "t9"] and loaded.triageSince == NOW, loaded.todos)
loaded.save()
saved = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("and the old notices key is not written back", "notices" not in saved, list(saved))
check("while the migration stamp is", saved.get("triageSince") == NOW, saved.get("triageSince"))
_clear(sm.STATE_PATH)
first_state, first = sm.State.load_or_init(NOW)
check("a first run starts already migrated, so its todos wait in 待分類",
      first is True and first_state.triageSince == NOW, first_state.triageSince)

# --- pushed mail with no todo waits in 待分類 like everything else ---
# It used to become a 重要事項 notice that the next round swept away unseen.
st41 = sm.State({"horizon": 100, "roundSeq": 41, "triageSince": 1,
                 "todos": [{"id": "old", "action": "from round 40", "priority": "normal"}]})
made = st41.pushed_as_todos(
    [{"id": "n1", "subject": "recruiter reply", "summary": "old text",
      "firstDeferredRound": 7},
     {"id": "n1", "summary": "reply by Friday"},
     {"id": "old", "summary": "pushed again"},
     {"id": "same", "summary": "statement is out"},
     {"id": "solo", "summary": "a person wrote to you"}], {"same"})
check("one todo per pushed message that has no row of its own",
      nid(made) == ["n1", "solo"], made)
m1 = [t for t in made if t["id"] == "n1"][0]
check("the later report wins the merge", m1["summary"] == "reply by Friday", m1)
check("an omitted field is not erased", m1["subject"] == "recruiter reply", m1)
check("the toast's summary is what it shows as the action",
      m1["action"] == "reply by Friday", m1)
check("another queue's clock does not follow it in", "firstDeferredRound" not in m1, m1)
check("and it carries no level, so it lands in 待分類", "priority" not in m1, m1)
check("the list itself is untouched until reconcile_todos runs",
      nid(st41.todos) == ["old"], st41.todos)

# --- end to end: a push this round waits in 待分類, a tick still wins ---
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.TRIAGE_PATH)
st50 = sm.State({"horizon": 100, "roundSeq": 50, "triageSince": 1,
                 "pendingNotify": [{"id": "e1", "subject": "statement",
                                    "summary": "confirm the charges"}],
                 "todos": [{"id": "tick1", "action": "finished", "priority": "normal"}]})
st50.save(); sm.Progress(200).save()
json.dump({"notifiedIds": ["e1"]}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
json.dump({"checkedIds": ["tick1"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
held = []
_was50 = sm.emit
sm.emit = held.append
sm.cmd_commit(None)
sm.emit = _was50
a50 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("commit puts the pushed message on the list", nid(a50["todos"]) == ["e1"],
      a50["todos"])
check("with the text the toast used, and no level",
      a50["todos"][0]["action"] == "confirm the charges"
      and "priority" not in a50["todos"][0], a50["todos"])
check("and clears it from pendingNotify", a50["pendingNotify"] == [], a50["pendingNotify"])
check("a ticked todo is archived, never put back", "tick1" in nid(_read_arch()),
      _read_arch())
check("the round asks for the list to open, because 待分類 is not empty",
      held[-1]["shouldOpenTodoList"] is True and held[-1]["untriaged"] == 1
      and held[-1]["filedFromPushThisRound"] == 1, held[-1])
_clear(sm.CHECKED_PATH, sm.ARCHIVE_PATH)

# --- a push and a todo for one mail in one round make one row ---
# Reported from the GUI when this was still two sections: a mail with both a
# push and a next step got a row in each.
sm.State({"horizon": 100, "roundSeq": 62, "triageSince": 1}).save()
sm.Progress(200).save()
json.dump({"important": [{"id": "PP", "from": "PayPal", "subject": "statement",
                          "summary": "August statement, reconcile it"}],
           "notifiedIds": ["PP"],
           "todos": [{"id": "PP", "from": "PayPal", "subject": "statement",
                      "action": "log in to PayPal and reconcile August",
                      "deadline": "", "uncertain": False}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
held.clear()
_was62 = sm.emit
sm.emit = held.append
sm.cmd_commit(None)
sm.emit = _was62
a62 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("a mail pushed and made a todo in one round appears once",
      nid(a62["todos"]) == ["PP"], a62["todos"])
check("the row keeps the action the LLM wrote, not the push summary",
      a62["todos"][0]["action"] == "log in to PayPal and reconcile August",
      a62["todos"][0])
check("nothing is filed from the push, and the todo still counts as new",
      (held[-1]["filedFromPushThisRound"], held[-1]["newTodosThisRound"]) == (0, 1),
      held[-1])
_clear(sm.ARCHIVE_PATH)

# --- a push and a tick landing in the same round ---
# A retry can genuinely succeed this round while the user ticks the same
# message between begin and commit. Without the filter the message would be
# archived and handed straight back to 待分類 in the same commit.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.TRIAGE_PATH)
st58 = sm.State({"horizon": 100, "roundSeq": 58, "triageSince": 1,
                 "pendingNotify": [{"id": "Z", "summary": "retry finally worked"}],
                 "todos": [{"id": "Z", "action": "the actual next step",
                            "priority": "normal", "createdRound": 57}]})
st58.save(); sm.Progress(200).save()
json.dump({"notifiedIds": ["Z"]}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
json.dump({"checkedIds": ["Z"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a58 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("a message ticked this round does not come back, even if the push succeeded",
      a58["todos"] == [], a58["todos"])
check("it is archived with the todo's own action, not the push summary",
      [(a["id"], a.get("action")) for a in _read_arch()]
      == [("Z", "the actual next step")], _read_arch())
check("and it leaves pendingNotify", a58["pendingNotify"] == [], a58["pendingNotify"])
_clear(sm.CHECKED_PATH, sm.ARCHIVE_PATH)

# --- the user's levels arrive in their own file and land on commit ---
_clear(sm.TRIAGE_PATH)
st70 = sm.State({"horizon": 100, "roundSeq": 70, "triageSince": 1,
                 "todos": [{"id": "a", "action": "x"},
                           {"id": "b", "action": "y", "priority": "normal"},
                           {"id": "c", "action": "z", "priority": "urgent"},
                           {"id": "d", "action": "w"},
                           {"subject": "no id"}]})
check("with no triage file nothing changes", st70.consume_triage() == (0, ""))
json.dump({"levels": {"a": "urgent", "b": "", "c": "urgent", "d": "someday",
                      "gone": "normal"}}, open(sm.TRIAGE_PATH, "w", encoding="utf-8"))
changed70, err70 = st70.consume_triage()
lv70 = {t.get("id"): t.get("priority") for t in st70.todos}
check("a level files the todo", lv70["a"] == "urgent", lv70)
check("an empty level sends it back to 待分類",
      "priority" not in [t for t in st70.todos if t.get("id") == "b"][0], st70.todos)
check("an unknown level is ignored rather than stored", lv70["d"] is None, lv70)
check("the count is only what actually changed", (changed70, err70) == (2, ""),
      (changed70, err70))
check("an id-less todo is never touched", st70.todos[-1] == {"subject": "no id"},
      st70.todos[-1])
open(sm.TRIAGE_PATH, "w").write("{ broken")
st71 = sm.State({"horizon": 100, "roundSeq": 71, "todos": [{"id": "a"}]})
check("an unreadable triage file reports instead of guessing",
      st71.consume_triage() == (0, "triage file unreadable"), st71.todos)
json.dump({"levels": ["not", "a", "map"]}, open(sm.TRIAGE_PATH, "w", encoding="utf-8"))
check("and so does one of the wrong shape",
      st71.consume_triage() == (0, "triage file unreadable"))
_clear(sm.TRIAGE_PATH)

# A report can never set or clear a level. Only the user's triage does.
st72 = sm.State({"horizon": 100, "roundSeq": 72, "triageSince": 1,
                 "todos": [{"id": "k", "action": "old", "priority": "important"}]})
st72.reconcile_todos([{"id": "k", "action": "new text", "priority": "normal"},
                      {"id": "n", "action": "fresh", "priority": "urgent"}], set())
lv72 = {t["id"]: t.get("priority") for t in st72.todos}
check("an upsert keeps the level the list already had",
      lv72["k"] == "important"
      and [t["action"] for t in st72.todos if t["id"] == "k"] == ["new text"], st72.todos)
check("and a new todo cannot arrive already filed", lv72["n"] is None, st72.todos)

# Through commit, where the level lands on the list and the round reports it.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.TRIAGE_PATH)
sm.State({"horizon": 100, "roundSeq": 73, "triageSince": 1,
          "todos": [{"id": "q", "action": "file me"}]}).save()
sm.Progress(200).save()
json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
json.dump({"levels": {"q": "important"}}, open(sm.TRIAGE_PATH, "w", encoding="utf-8"))
held.clear()
_was73 = sm.emit
sm.emit = held.append
sm.cmd_commit(None)
sm.emit = _was73
a73 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("commit writes the user's level into state.json",
      a73["todos"][0].get("priority") == "important", a73["todos"])
check("and reports it, with nothing left in 待分類 to open the list for",
      (held[-1]["triagedThisRound"], held[-1]["triageBlocked"], held[-1]["untriaged"],
       held[-1]["shouldOpenTodoList"]) == (1, "", 0, False), held[-1])
_clear(sm.TRIAGE_PATH)

# --- 追蹤中 is a stamp beside the level, never a level of its own ---
_clear(sm.FOLLOW_PATH, sm.TRIAGE_PATH)
st80 = sm.State({"horizon": 100, "roundSeq": 80, "triageSince": 1,
                 "todos": [{"id": "f", "action": "sent the form", "priority": "important"},
                           {"id": "back", "action": "got the reply", "priority": "urgent",
                            "followSince": 500},
                           {"id": "raw", "action": "never filed"},
                           {"id": "odd", "action": "string flag", "priority": "normal"},
                           {"subject": "no id", "priority": "normal"}]})
check("with no follow file nothing changes", st80.consume_follow(1000) == (0, ""))
json.dump({"follow": {"f": True, "back": False, "raw": True, "odd": "false",
                      "gone": True}}, open(sm.FOLLOW_PATH, "w", encoding="utf-8"))
changed80, err80 = st80.consume_follow(1000)
by80 = {t.get("id"): t for t in st80.todos}
check("a follow stamps the round's time and keeps the level",
      (by80["f"].get("followSince"), by80["f"]["priority"]) == (1000, "important"), by80["f"])
check("回到待辦 drops the stamp and the todo keeps the level it left with",
      "followSince" not in by80["back"] and by80["back"]["priority"] == "urgent", by80["back"])
check("a todo with no level is refused, so it cannot hide outside 待分類",
      "followSince" not in by80["raw"] and st80.untriaged_count() == 1, by80["raw"])
check("a string value is not read as a yes", "followSince" not in by80["odd"], by80["odd"])
check("only real changes count", (changed80, err80) == (2, ""), (changed80, err80))
check("an id-less todo is never touched", "followSince" not in st80.todos[-1], st80.todos[-1])
st80.consume_follow(9999)
check("following again does not restart the clock", by80["f"]["followSince"] == 1000,
      by80["f"])
open(sm.FOLLOW_PATH, "w").write("{ broken")
check("an unreadable follow file reports instead of guessing",
      st80.consume_follow(1000) == (0, "follow file unreadable"))
_clear(sm.FOLLOW_PATH)

st81 = sm.State({"horizon": 100, "roundSeq": 81, "triageSince": 1,
                 "todos": [{"id": "k", "action": "old", "priority": "normal",
                            "followSince": 700}]})
st81.reconcile_todos([{"id": "k", "action": "new", "followSince": 1},
                      {"id": "n", "action": "fresh", "followSince": 1}], set())
by81 = {t["id"]: t for t in st81.todos}
check("a report can neither move nor start a follow",
      by81["k"]["followSince"] == 700 and "followSince" not in by81["n"], st81.todos)
json.dump({"levels": {"k": ""}}, open(sm.TRIAGE_PATH, "w", encoding="utf-8"))
st81.consume_triage()
check("重新分類 clears the stamp too, so re-filing lands in 待辦清單",
      "followSince" not in by81["k"] and "priority" not in by81["k"], by81["k"])
_clear(sm.TRIAGE_PATH)

# Filed and followed in one round, then through commit and the restore path.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.TRIAGE_PATH, sm.FOLLOW_PATH)
sm.State({"horizon": 100, "roundSeq": 82, "triageSince": 1,
          "todos": [{"id": "q", "action": "file then follow"}]}).save()
sm.Progress(200).save()
json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
json.dump({"levels": {"q": "normal"}}, open(sm.TRIAGE_PATH, "w", encoding="utf-8"))
json.dump({"follow": {"q": True}}, open(sm.FOLLOW_PATH, "w", encoding="utf-8"))
held.clear()
_was82 = sm.emit
sm.emit = held.append
sm.cmd_commit(None)
sm.emit = _was82
a82 = json.load(open(sm.STATE_PATH, encoding="utf-8"))["todos"][0]
check("a level and a follow queued together both land, triage first",
      a82.get("priority") == "normal" and isinstance(a82.get("followSince"), int), a82)
check("commit reports the follow",
      (held[-1]["followedThisRound"], held[-1]["followBlocked"],
       held[-1]["shouldOpenTodoList"]) == (1, "", False), held[-1])
_clear(sm.TRIAGE_PATH, sm.FOLLOW_PATH)
_write_archive([dict(a82, archivedAt=2000)])
json.dump({"restoreIds": ["q"]}, open(sm.RESTORE_PATH, "w", encoding="utf-8"))
st83 = sm.State({"horizon": 100, "roundSeq": 83, "triageSince": 1})
st83.consume_restores()
check("a todo archived from 追蹤中 is restored to 待辦清單 at its level",
      [(t.get("priority"), "followSince" in t) for t in st83.todos] == [("normal", False)],
      st83.todos)
_clear(sm.ARCHIVE_PATH, sm.RESTORE_PATH)

# --- a restored todo comes back at its level, old untriaged mail as 普通 ---
_clear(sm.ARCHIVE_PATH, sm.RESTORE_PATH)
_write_archive([{"id": "lvl", "action": "was filed", "priority": "urgent",
                 "archivedAt": 500},
                {"id": "old", "action": "archived before triage", "archivedAt": 500},
                {"id": "new", "action": "archived from 待分類", "archivedAt": 2000}])
json.dump({"restoreIds": ["lvl", "old", "new"]},
          open(sm.RESTORE_PATH, "w", encoding="utf-8"))
st74 = sm.State({"horizon": 100, "roundSeq": 74, "triageSince": 1000})
st74.consume_restores()
lv74 = {t["id"]: t.get("priority") for t in st74.todos}
check("a filed todo comes back at its own level", lv74["lvl"] == "urgent", lv74)
check("one archived before triage existed comes back as 普通", lv74["old"] == "normal",
      lv74)
check("one archived from 待分類 since then goes back there", lv74["new"] is None, lv74)
_clear(sm.ARCHIVE_PATH, sm.RESTORE_PATH)

# --- itemsMissingId still covers every queue there is ---
st57 = sm.State({"horizon": 100, "roundSeq": 57,
                 "todos": [{"subject": "no id"}, {"id": "fine"}],
                 "pendingNotify": [{"summary": "no id either"}]})
check("orphan_count counts id-less entries across the queues", st57.orphan_count() == 2,
      st57.orphan_count())

# --- a ticked message is never pushed again ---
# begin hands out notifications before commit consumes the ticks, so a round
# used to push a toast for work the user had already marked done.
_clear(sm.ARCHIVE_PATH, sm.TRIAGE_PATH)
st49 = sm.State({"horizon": 100, "roundSeq": 49,
                 "pendingNotify": [{"id": "done1", "summary": "already finished"},
                                   {"id": "open1", "summary": "still open"}],
                 "todos": [{"id": "done1", "action": "was ticked"}]})
d49 = st49.split_debt({"done1"})
check("a ticked message is not pushed again",
      nid(d49["notifyNow"]) == ["open1"], d49["notifyNow"])
check("but it stays in the queue for commit to clear",
      nid(st49.pendingNotify) == ["done1", "open1"], st49.pendingNotify)
check("no argument still means no filtering",
      nid(st49.split_debt()["notifyNow"]) == ["done1", "open1"])

# --- step and commit must also report corruption instead of crashing ---
open(sm.STATE_PATH, "w").write("{ broken")
sm.Progress(200).save()
for label, fn, arg in (("step", sm.cmd_step, type("A", (), {"failed": True, "lo": 0, "hi": 0, "count": 0})()),
                       ("commit", sm.cmd_commit, None)):
    try:
        check(f"cmd_{label} aborts cleanly on corrupt state", fn(arg) == 2)
    except Exception as exc:
        check(f"cmd_{label} aborts cleanly on corrupt state", False, type(exc).__name__)

# --- a wrong shape in round.json must refuse the round, never be absorbed ---
# Dropping the bad field looked safer but isn't: cmd_step retires the interval
# either way, so the batch's only copy disappears with the watermark already
# past it. Keep-by-default doesn't help, since nothing here ever reached a queue.
def _shape_error(payload):
    json.dump(payload, open(sm.ROUND_PATH, "w", encoding="utf-8"))
    try:
        sm._read_findings(None)
        return False
    except sm.FindingsError:
        return True


check("an explicit null is a shape error", _shape_error({"important": None}))
check("a bare object where a list belongs is a shape error",
      _shape_error({"important": {"id": "m1"}}))
check("a string where a list belongs is a shape error",
      _shape_error({"todos": "nope"}))
check("one non-object entry poisons the whole list",
      _shape_error({"defer": [{"id": "d1"}, None]}))
check("an id list holding an object is a shape error",
      _shape_error({"notifiedIds": [{"id": "x"}]}))
check("an omitted field is still fine", not _shape_error({}))
check("well-formed lists are still fine",
      not _shape_error({"important": [{"id": "m1"}], "judgedIds": ["a", 1]}))

# Entry fields with a type-sensitive consumer. deadline crashes in-round, which
# is loud but recoverable. The two counting fields are the dangerous ones: they
# survive reconcile_* into state.json, and every later begin or commit then
# raises on int(), wedging the schedule until state.json is hand-edited.
check("a non-string deadline is refused",
      _shape_error({"todos": [{"id": "m1", "deadline": 123}]}))
check("a non-int firstDeferredRound is refused",
      _shape_error({"defer": [{"id": "d1", "firstDeferredRound": [1]}]}))
check("a non-int createdRound is refused",
      _shape_error({"todos": [{"id": "t1", "createdRound": [1]}]}))
check("a non-string subject is refused, it is a GUI sort key",
      _shape_error({"todos": [{"id": "t1", "subject": 123}]}))
check("True is not an acceptable round number",
      _shape_error({"todos": [{"id": "t1", "createdRound": True}]}))
check("correctly typed entry fields pass",
      not _shape_error({"todos": [{"id": "t1", "subject": "Pay", "deadline": "",
                                   "createdRound": 4, "uncertain": True}]}))
# bool() is true for every non-empty string, so "false" would display as
# 待確認 -- a wrong answer, not a crash.
check("a stringy uncertain is refused",
      _shape_error({"todos": [{"id": "t1", "uncertain": "false"}]}))

# The gate must not refuse what the old code handled correctly, or a
# reporter repeating the same safe mistake keeps losing a whole round to it.
check("an explicit null entry field is an omission, not a wrong type",
      not _shape_error({"todos": [{"id": "m1", "action": "Pay",
                                   "deadline": None}]}))
check("a numeric id is accepted, every consumer str()s it",
      not _shape_error({"todos": [{"id": 123, "action": "Pay"}]}))
check("_merge really does drop None, which is why the above is safe",
      sm._merge({}, {"id": "m1", "deadline": None}) == {"id": "m1"})
check("and _usable_id really does normalise a numeric id",
      sm._usable_id({"id": 123}) == "123")

# The counting fields must never reach state.json, so prove the refusal happens
# at ingestion rather than one round later.
for label, payload in (
        ("firstDeferredRound", {"defer": [{"id": "d1",
                                           "firstDeferredRound": [1]}]}),
        ("createdRound", {"todos": [{"id": "t1", "action": "pay",
                                     "createdRound": [1]}]})):
    st_w = sm.State({"horizon": 2000, "intervals": [[1000, 2000]]})
    st_w.save(); sm.Progress(2000).save()
    json.dump(payload, open(sm.ROUND_PATH, "w", encoding="utf-8"))
    rc_w = sm.cmd_step(type("A", (), {"failed": False, "lo": 1000, "hi": 2000,
                                      "count": 3})())
    saved = json.load(open(sm.STATE_PATH, encoding="utf-8"))
    check(f"a bad {label} never lands in state.json",
          rc_w == 2 and not saved["pendingJudge"] and not saved["todos"], saved)
    json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
    check(f"so the next begin still runs after a bad {label}",
          sm.cmd_begin(None) == 0)

# The assertion that matters. A shape error is only safe if the interval
# survives it, so the next round rescans the span this batch came from.
st10 = sm.State({"horizon": 2000, "intervals": [[1000, 2000]]})
st10.save(); sm.Progress(2000).save()
json.dump({"important": {"id": "m1", "summary": "recruiter wants a call"}},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
rc10 = sm.cmd_step(type("A", (), {"failed": False, "lo": 1000, "hi": 2000,
                                  "count": 3})())
a10 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("a malformed report makes step refuse, not proceed", rc10 == 2, rc10)
check("the interval is not retired on a malformed report",
      a10["intervals"] == [[1000, 2000]], a10["intervals"])
check("the watermark does not move past the unrecorded batch",
      a10["coveredThrough"] == 1000, a10["coveredThrough"])

# --- a frontier that stops moving is an outage, not a quiet mailbox ---
# The whole point of watching coverage rather than mail volume is that an empty
# interval retires exactly like a full one, so silence from the mailbox can
# never be mistaken for a task that stopped finishing rounds.
NOW = int(_time.time())
# emit was handed back to the real one further up, so capture it again here.
stall_out = []
_was_stall_emit = sm.emit
sm.emit = stall_out.append


def begin_stall(horizon, fresh=False):
    stall_out.clear()
    _clear(sm.STATE_PATH, sm.ROUND_PATH)
    if not fresh:
        sm.State({"horizon": horizon, "roundSeq": 40}).save()
    sm.cmd_begin(None)
    return stall_out[-1]


out_ok = begin_stall(NOW - 3600)
check("an hour behind says nothing at all", "coverageStalled" not in out_ok
      and "stalledHours" not in out_ok, out_ok)

out_bad = begin_stall(NOW - 30 * 3600)
check("thirty hours behind is a stall", out_bad["coverageStalled"] is True, out_bad)
check("and the message has the elapsed hours to show",
      out_bad["stalledHours"] == 30, out_bad["stalledHours"])

out_edge = begin_stall(NOW - 8 * 3600 - 30 * 60)
check("the longest normal gap between schedules is not a stall",
      "coverageStalled" not in out_edge, out_edge)

# A first run seeds the horizon exactly FIRST_RUN_LOOKBACK back, which is the
# threshold itself, so without its own exemption the very first round would
# report a stall on state it just created.
out_first = begin_stall(None, fresh=True)
check("a first run does not report a stall", out_first["firstRun"] is True
      and "coverageStalled" not in out_first, out_first)

# Finding nothing still retires the interval, so the frontier catches up.
stall_out.clear()
_clear(sm.STATE_PATH, sm.ROUND_PATH)
sm.State({"horizon": NOW - 2 * 3600, "roundSeq": 41}).save()
sm.cmd_begin(None)
iv = json.load(open(sm.STATE_PATH, encoding="utf-8"))["intervals"][0]
sm.cmd_step(type("A", (), {"failed": False, "lo": iv[0], "hi": iv[1], "count": 0})())
check("an interval that found no mail still retires",
      json.load(open(sm.STATE_PATH, encoding="utf-8"))["intervals"] == [],
      json.load(open(sm.STATE_PATH, encoding="utf-8"))["intervals"])
stall_out.clear()
sm.cmd_begin(None)
out_quiet = stall_out[-1]
check("so a silent mailbox never reads as a stall",
      "coverageStalled" not in out_quiet, out_quiet)
sm.emit = _was_stall_emit

# --- a catch-up begin backs off while another round is mid-flight ---
# Every task that missed its slot fires at once when the app reopens. Only the
# first begin may start a round, and the ones that lose must not touch anything
# the live round is about to read.
live_out = []
_was_live_emit = sm.emit
sm.emit = live_out.append
sm.LIVE_ROUND_SECONDS = LIVE_ROUND_REAL


def disk(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else None


_clear(sm.STATE_PATH, sm.ROUND_PATH, sm.PROGRESS_PATH)
sm.State({"horizon": NOW - 3600, "roundSeq": 50}).save()
live_out.clear()
check("the first catch-up begin starts a round", sm.cmd_begin(None) == 0
      and live_out[-1]["status"] == "PROCEED", live_out[-1])
json.dump({"roundToken": live_out[-1]["roundToken"], "todos": [{"id": "live"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
held = (disk(sm.STATE_PATH), disk(sm.PROGRESS_PATH), disk(sm.ROUND_PATH))
live_out.clear()
rc = sm.cmd_begin(None)
check("a second begin moments later backs off",
      rc == 0 and live_out[-1]["status"] == "ROUND_ALREADY_RUNNING", live_out[-1])
check("and leaves state, progress and findings exactly as the live round left them",
      (disk(sm.STATE_PATH), disk(sm.PROGRESS_PATH), disk(sm.ROUND_PATH)) == held)

old = _time.time() - LIVE_ROUND_REAL - 1
os.utime(sm.PROGRESS_PATH, (old, old))
live_out.clear()
sm.cmd_begin(None)
check("a round silent past the window is dead, so begin starts a new one",
      live_out[-1]["status"] == "PROCEED", live_out[-1])
check("and clears the dead round's findings as usual", not os.path.exists(sm.ROUND_PATH))

ahead = _time.time() + 3600
os.utime(sm.PROGRESS_PATH, (ahead, ahead))
live_out.clear()
sm.cmd_begin(None)
check("a progress file dated in the future does not hold rounds off",
      live_out[-1]["status"] == "PROCEED", live_out[-1])

json.dump({"roundToken": live_out[-1]["roundToken"]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
live_out.clear()
sm.cmd_begin(None)
check("a round started right after a commit is not blocked",
      live_out[-1]["status"] == "PROCEED", live_out[-1])

# --- a round turned away waits instead of leaving, and takes over a dead one ---
# Leaving at once let one live round that then died cost the whole catch-up,
# because every other round had already gone.
WAIT_REAL = sm.WAIT_MAX_SECONDS


def age_progress(seconds):
    t = _time.time() - seconds
    os.utime(sm.PROGRESS_PATH, (t, t))


def wait_with(on_sleep):
    """cmd_wait with a sleep that changes the world instead of passing time.

    Raises after a few polls, so a wait that never notices the change fails
    fast instead of spinning out the real WAIT_MAX_SECONDS.
    """
    calls = []

    def fake(seconds):
        calls.append(seconds)
        if len(calls) > 3:
            raise RuntimeError("wait never noticed the change")
        on_sleep()
    live_out.clear()
    try:
        sm.cmd_wait(None, sleep=fake)
    except RuntimeError as exc:
        return {"status": str(exc)}, calls
    return live_out[-1], calls


def begin_winner():
    _clear(sm.STATE_PATH, sm.ROUND_PATH, sm.PROGRESS_PATH)
    sm.State({"horizon": NOW - 3600, "roundSeq": 60}).save()
    live_out.clear()
    sm.cmd_begin(None)
    token = live_out[-1]["roundToken"]
    json.dump({"roundToken": token}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
    return token


def commit_winner():
    sm.cmd_commit(None)


begin_winner()
held = (disk(sm.STATE_PATH), disk(sm.PROGRESS_PATH), disk(sm.ROUND_PATH))
sm.WAIT_MAX_SECONDS = 0
live_out.clear()
check("wait reports a live round as still running",
      sm.cmd_wait(None) == 0 and live_out[-1]["status"] == "STILL_RUNNING"
      and "ageSeconds" in live_out[-1], live_out[-1])
check("and waiting writes nothing the live round reads",
      (disk(sm.STATE_PATH), disk(sm.PROGRESS_PATH), disk(sm.ROUND_PATH)) == held)

sm.WAIT_MAX_SECONDS = WAIT_REAL
out, calls = wait_with(commit_winner)
check("a commit during the wait ends it as covered", out["status"] == "COVERED", out)
check("after sleeping one poll interval", calls == [sm.WAIT_POLL_SECONDS], calls)

sm.WAIT_MAX_SECONDS = 0
live_out.clear()
sm.cmd_wait(None)
check("with the live round already committed there is nothing to wait for",
      live_out[-1]["status"] == "COVERED", live_out[-1])

# The same path covers a round that re-ran its own begin, taking over from
# itself instead of abandoning the round.
begin_winner()
seq_before = json.load(open(sm.STATE_PATH, encoding="utf-8"))["roundSeq"]
sm.WAIT_MAX_SECONDS = WAIT_REAL
out, _ = wait_with(lambda: age_progress(LIVE_ROUND_REAL + 1))
check("a live round that goes silent past the window is taken over",
      out["status"] == "TAKE_OVER", out)
live_out.clear()
sm.cmd_begin(None)
check("and the waiting round's begin then starts a round of its own",
      live_out[-1]["status"] == "PROCEED"
      and json.load(open(sm.STATE_PATH, encoding="utf-8"))["roundSeq"] == seq_before + 1,
      live_out[-1])

begin_winner()
ahead = _time.time() + 3600
os.utime(sm.PROGRESS_PATH, (ahead, ahead))
sm.WAIT_MAX_SECONDS = 0
live_out.clear()
sm.cmd_wait(None)
check("a future-dated progress file is taken over, matching begin's guard",
      live_out[-1]["status"] == "TAKE_OVER", live_out[-1])

sm.WAIT_MAX_SECONDS = WAIT_REAL
sm.LIVE_ROUND_SECONDS = 0
sm.emit = _was_live_emit

# --- the prompt gate skips only after a recent commit that left nothing owed ---
# It runs before the model starts, so a wrong SKIP silently loses a check while
# a wrong RUN only costs tokens. Every doubt has to come out as RUN.
_clear(sm.STATE_PATH, sm.ROUND_PATH, sm.PROGRESS_PATH, sm.CONFIG_PATH)
now = int(_time.time())
check("a first run with no state is not skipped",
      sm.gate_decision(now)["status"] == "RUN", sm.gate_decision(now))


def gate_with(**fields):
    sm.State({"horizon": now - 3600, "lastConfigStatus": "missing", **fields}).save()
    return sm.gate_decision(now)


check("a state that never recorded a commit is not skipped",
      gate_with()["status"] == "RUN", gate_with())
recent = gate_with(lastCommitAt=now - 60)
check("a commit a minute ago skips", recent["status"] == "SKIP"
      and recent["lastCommitAgeSeconds"] == 60, recent)
check("a commit exactly 45 minutes ago no longer skips",
      gate_with(lastCommitAt=now - 45 * 60)["status"] == "RUN")
check("a commit dated in the future does not skip",
      gate_with(lastCommitAt=now + 600)["status"] == "RUN")
check("an owed notification is never skipped",
      gate_with(lastCommitAt=now - 60, pendingNotify=[{"id": "p"}])["status"] == "RUN")
check("an unscanned interval is never skipped",
      gate_with(lastCommitAt=now - 60, intervals=[[now - 900, now]])["status"] == "RUN")
check("a config alert still owed is never skipped",
      gate_with(lastCommitAt=now - 60, lastConfigStatus="")["status"] == "RUN")

gate_with(lastCommitAt=now - 60)
before_gate = open(sm.STATE_PATH, encoding="utf-8").read()
gate_out = []
_was_gate_emit = sm.emit
sm.emit = gate_out.append
rc = sm.cmd_gate(None)
check("cmd_gate reports the decision and leaves state.json untouched",
      rc == 0 and gate_out[-1]["status"] == "SKIP"
      and open(sm.STATE_PATH, encoding="utf-8").read() == before_gate, gate_out)

_real_gate = sm.gate_decision
sm.gate_decision = lambda now: 1 / 0
gate_out.clear()
sm.cmd_gate(None)
sm.gate_decision = _real_gate
check("a gate that crashes answers RUN", gate_out[-1]["status"] == "RUN", gate_out)
sm.emit = _was_gate_emit

open(sm.STATE_PATH, "w").write("{ not json")
check("an unreadable state is not skipped", sm.gate_decision(now)["status"] == "RUN")

_clear(sm.STATE_PATH, sm.ROUND_PATH, sm.PROGRESS_PATH)
sm.State({"horizon": now - 3600}).save()
sm.Progress(now).save()
json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.emit = lambda payload: None
sm.cmd_commit(None)
sm.emit = _was_gate_emit
stamped = json.load(open(sm.STATE_PATH, encoding="utf-8")).get("lastCommitAt", 0)
check("a successful commit records when it happened",
      now <= stamped <= int(_time.time()), stamped)

# --- corrupt state must abort, never invent a watermark ---
open(sm.STATE_PATH, "w").write("{ not json")
try:
    sm.State.load_or_init(999)
    check("corrupt state raises", False)
except sm.StateError:
    check("corrupt state raises StateError", True)

shutil.rmtree(work, ignore_errors=True)
print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
