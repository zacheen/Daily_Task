"""End-to-end scenario tests for the state machine, run against a temp copy."""
import importlib.util, json, os, shutil, sys, tempfile

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statemachine.py")
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

# --- notified wins over failed for the same id ---
st3 = sm.State({"horizon": 100, "roundSeq": 1})
st3.save(); sm.Progress(200).save()
json.dump({"notifiedIds": ["m1"], "failedNotify": [{"id": "m1", "summary": "s"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a3 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("successfully notified id not left in pendingNotify", a3["pendingNotify"] == [], a3["pendingNotify"])
check("notified id recorded", "m1" in a3["notifiedIds"])

# --- queue debt survives when the LLM was never shown it ---
# The OOP review found this by execution: split_debt only exposes the first
# DEBT_BODY_BUDGET fresh items, so wholesale-replacing the queue at commit
# dropped every item beyond the slice with no error.
st4 = sm.State({"horizon": 100, "roundSeq": 7,
                "pendingJudge": [{"id": "old-A", "firstDeferredRound": 7},
                                 {"id": "old-B", "firstDeferredRound": 7},
                                 {"id": "old-C", "firstDeferredRound": 7}]})
st4.save(); sm.Progress(200).save()
shown = [x["id"] for x in st4.split_debt()["judgeNow"]]
check("budget slice hides the third fresh item", shown == ["old-A", "old-B"], shown)
# A perfectly obedient LLM can only re-defer what it was shown.
json.dump({"defer": [{"id": "old-A"}, {"id": "old-B"}]},
          open(sm.ROUND_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a4 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
ids4 = sorted(x["id"] for x in a4["pendingJudge"])
check("unshown judge debt survives commit", ids4 == ["old-A", "old-B", "old-C"], ids4)

# --- judgedIds is the only way an item leaves the judge queue ---
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
sm.CHECKED_PATH = os.path.join(work, "todos-checked.json")
sm.ARCHIVE_PATH = os.path.join(work, "todos-archive.json")

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
    """Ids in todos-archive.json. Other cases above already put rows here, so
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
# A broken config is a persistent state and the task runs four times a day, so
# alerting on the transition is what stops it becoming four identical pushes,
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

# --- the todo list only opens when there is something new AND it is not night ---
# The window lives here rather than in the round's own arithmetic, because the
# 06:00 task would otherwise pop a browser window while the user is asleep.
real_hours = sm.GUI_OPEN_HOURS
DAY = sm.GUI_OPEN_HOURS
check("the window start is inclusive", sm.within_gui_hours(9, DAY))
check("the window end is exclusive", not sm.within_gui_hours(23, DAY),
      "23 must still admit the 21:30 round, and exclude midnight")
check("the 06:00 round is outside the window", not sm.within_gui_hours(6, DAY))
check("the 11:10, 16:20 and 21:30 rounds are inside",
      all(sm.within_gui_hours(h, DAY) for h in (11, 16, 21)))
check("a window crossing midnight narrows instead of matching nothing",
      sm.within_gui_hours(23, (22, 6)) and sm.within_gui_hours(3, (22, 6))
      and not sm.within_gui_hours(12, (22, 6)))

held = []
was_emit = sm.emit
sm.emit = held.append


def commit_open(todos, hours):
    sm.GUI_OPEN_HOURS = hours
    sm.State({"horizon": 100, "roundSeq": 50, "todos": todos}).save()
    sm.Progress(200, config_seen="present").save()
    json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
    held.clear()
    sm.cmd_commit(None)
    return held[-1]


ALWAYS, NEVER = (0, 24), (0, 0)
fresh = [{"id": "n1", "subject": "new this round", "createdRound": 50}]
stale = [{"id": "o1", "subject": "from an older round", "createdRound": 49}]

out = commit_open(fresh, ALWAYS)
check("new todos inside the window open the list",
      out["shouldOpenTodoList"] is True and out["newTodosThisRound"] == 1, out)
out = commit_open(fresh, NEVER)
check("new todos outside the window do not open it",
      out["shouldOpenTodoList"] is False and out["newTodosThisRound"] == 1,
      "the count is still reported, it just is not the decision")
out = commit_open(stale, ALWAYS)
check("an old todo alone does not open it",
      out["shouldOpenTodoList"] is False and out["newTodosThisRound"] == 0, out)
out = commit_open([], ALWAYS)
check("no todos at all does not open it", out["shouldOpenTodoList"] is False)

sm.GUI_OPEN_HOURS = real_hours
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
# overwriting another's changes. All four tasks fire together when the app
# reopens after being closed past several slots.
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

def _read_arch():
    if not os.path.exists(sm.ARCHIVE_PATH):
        return []
    return json.load(open(sm.ARCHIVE_PATH, encoding="utf-8")).get("archived", [])


# --- restore from the archive, and the three-day purge ---
sm.RESTORE_PATH = os.path.join(work, "todos-restore.json")
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

# --- notices: what was pushed becomes reviewable, not fire-and-forget ---
# A notification used to be a push and nothing else, so the toast and the todo
# list could disagree and there was no way to say "that one is actually a
# todo". Notified mail now waits for the user, and taking no action resolves it
# into the archive.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.PROMOTE_PATH)
nid = lambda q: sorted(str(x.get("id")) for x in q)

st40 = sm.State({"horizon": 100, "roundSeq": 40})
stamped40, owned40 = st40.record_notices(
    [{"id": "n1", "subject": "recruiter reply", "summary": "old text",
      "firstDeferredRound": 7},
     {"id": "n1", "summary": "reply by Friday"},
     {"id": "n2", "subject": "album shared"}], NOW, set())
check("record_notices files one entry per message, not per report",
      nid(st40.notices) == ["n1", "n2"], st40.notices)
check("and counts distinct messages, since a re-push arrives twice",
      (stamped40, owned40) == (2, 0), (stamped40, owned40))
n1 = [x for x in st40.notices if x["id"] == "n1"][0]
check("a later report wins the merge", n1["summary"] == "reply by Friday", n1)
check("an omitted field is not erased", n1["subject"] == "recruiter reply", n1)
check("another queue's clock does not follow it in",
      "firstDeferredRound" not in n1, n1)
check("the review clock is this round", n1["noticedRound"] == 40, n1)

# Nothing is swept in the round that filed it, or the section would already be
# empty by the time the GUI opened.
swept40, err40 = st40.sweep_notices(NOW)
check("a notice filed this round survives its own commit", swept40 == 0, swept40)
check("so nothing was archived yet", _read_arch() == [], _read_arch())

st41 = sm.State({"horizon": 100, "roundSeq": 41, "notices": st40.notices})
swept41, err41 = st41.sweep_notices(NOW + 3600)
check("taking no action sweeps the notice next round", swept41 == 2, swept41)
check("the queue is empty afterwards", st41.notices == [], st41.notices)
check("both land in the archive", nid(_read_arch()) == ["n1", "n2"], _read_arch())
check("stamped, so the existing purge can age them out",
      all(a["archivedAt"] == NOW + 3600 for a in _read_arch()), _read_arch())
check("neither sweep reported a fault", (err40, err41) == ("", ""), (err40, err41))
check("and the 3-day purge is what finally disposes of them",
      sm.State({"horizon": 100, "roundSeq": 42}).purge_archive(
          NOW + 3600 + sm.ARCHIVE_TTL + 1) == 2 and _read_arch() == [], _read_arch())

# Promoting: the GUI asks in its own file, commit performs it.
st43 = sm.State({"horizon": 100, "roundSeq": 43})
st43.record_notices([{"id": "p1", "subject": "credit card statement",
                      "summary": "confirm the charges"},
                     {"id": "p2", "subject": "album shared"}], NOW, set())
json.dump({"promoteIds": ["p1"]}, open(sm.PROMOTE_PATH, "w", encoding="utf-8"))
st44 = sm.State({"horizon": 100, "roundSeq": 44, "notices": st43.notices})
moved44, perr44 = st44.consume_promotes()
check("a promoted notice becomes a todo", nid(st44.todos) == ["p1"], st44.todos)
check("under this round, so it shows up in the new section",
      st44.todos[0]["createdRound"] == 44, st44.todos[0])
check("the notice clock does not follow it into the todo",
      "noticedRound" not in st44.todos[0], st44.todos[0])
check("and it leaves the notice queue", nid(st44.notices) == ["p2"], st44.notices)
check("promoting reports no fault", (nid(moved44), perr44) == (["p1"], ""),
      (moved44, perr44))

swept44, _ = st44.sweep_notices(NOW)
check("promoting one notice does not rescue the others",
      nid(_read_arch()) == ["p2"] and swept44 == 1, _read_arch())
check("and the promoted todo is untouched by the sweep",
      nid(st44.todos) == ["p1"], st44.todos)

# An existing todo wins. A notice carries a one-line summary of the push, which
# must not overwrite the action the LLM wrote for the todo.
_clear(sm.ARCHIVE_PATH)
json.dump({"promoteIds": ["dup"]}, open(sm.PROMOTE_PATH, "w", encoding="utf-8"))
st45 = sm.State({"horizon": 100, "roundSeq": 45,
                 "todos": [{"id": "dup", "action": "the real next step",
                            "createdRound": 44}],
                 "notices": [{"id": "dup", "summary": "pushed",
                              "noticedRound": 44}]})
moved45, perr45 = st45.consume_promotes()
check("promoting a message that is already a todo adds no second row",
      nid(st45.todos) == ["dup"], st45.todos)
check("and does not overwrite its action",
      st45.todos[0]["action"] == "the real next step", st45.todos[0])
check("the notice is consumed either way", st45.notices == [], st45.notices)
# The count reaches the user in a push, so a skipped duplicate must not inflate
# it into a todo they will look for and not find.
check("and the skipped duplicate is not reported as promoted",
      (moved45, perr45) == ([], ""), (moved45, perr45))

# An unreadable request file must not be read as "nothing requested".
open(sm.PROMOTE_PATH, "w").write("{ broken")
st46 = sm.State({"horizon": 100, "roundSeq": 46,
                 "notices": [{"id": "keep", "noticedRound": 46}]})
moved46, perr46 = st46.consume_promotes()
check("an unreadable promote file reports instead of promoting",
      perr46 == "promote file unreadable", perr46)
check("and the notice is left alone", nid(st46.notices) == ["keep"], st46.notices)
_clear(sm.PROMOTE_PATH)

# record_notices must run before sweep_notices. The other order archives a
# re-notified message and then re-files it, leaving one message in two places.
st47 = sm.State({"horizon": 100, "roundSeq": 47,
                 "notices": [{"id": "again", "subject": "retry",
                              "noticedRound": 46}]})
st47.record_notices([{"id": "again", "subject": "retry"}], NOW, set())
swept47, _ = st47.sweep_notices(NOW)
check("a message notified again restarts its review clock", swept47 == 0, swept47)
check("so it is not sitting in the archive as well", _read_arch() == [], _read_arch())
check("and it is still reviewable", nid(st47.notices) == ["again"], st47.notices)

# An id-less notice can never be promoted, so it must not be stranded either.
st48 = sm.State({"horizon": 100, "roundSeq": 48,
                 "notices": [{"subject": "no id at all", "noticedRound": 47}]})
json.dump({"promoteIds": ["None"]}, open(sm.PROMOTE_PATH, "w", encoding="utf-8"))
st48.consume_promotes()
check("the string None cannot promote an id-less notice",
      st48.todos == [] and len(st48.notices) == 1, (st48.todos, st48.notices))
swept48, _ = st48.sweep_notices(NOW)
check("but the sweep still disposes of it",
      swept48 == 1 and st48.notices == [], st48.notices)
_clear(sm.PROMOTE_PATH, sm.ARCHIVE_PATH)

# A review clock that cannot be read sweeps rather than sticks.
st53 = sm.State({"horizon": 100, "roundSeq": 53,
                 "notices": [{"id": "nostamp", "subject": "hand written"},
                             {"id": "junk", "noticedRound": "soon"}]})
swept53, _ = st53.sweep_notices(NOW)
check("a notice with no usable review clock is swept, not stranded forever",
      swept53 == 2 and st53.notices == [], (swept53, st53.notices))

# A message already in the archive is not archived twice.
_clear(sm.ARCHIVE_PATH)
_write_archive([{"id": "both", "subject": "already there", "archivedAt": NOW}])
st52 = sm.State({"horizon": 100, "roundSeq": 52,
                 "notices": [{"id": "both", "noticedRound": 51}]})
swept52, _ = st52.sweep_notices(NOW)
check("the sweep does not duplicate an entry already archived",
      len(_read_arch()) == 1, _read_arch())
check("the notice is consumed regardless",
      st52.notices == [] and swept52 == 1, st52.notices)

# A refused archive write must leave the notices reviewable, not drop them.
_clear(sm.ARCHIVE_PATH)
_write_archive([], rev="mismatch")
st51 = sm.State({"horizon": 100, "roundSeq": 51,
                 "notices": [{"id": "s1", "noticedRound": 50}]})
_real_ra51 = sm._read_archive
sm._read_archive = lambda: ([], "", "stale-rev")
swept51, err51 = st51.sweep_notices(NOW)
sm._read_archive = _real_ra51
check("a refused archive write abandons the sweep", swept51 == 0, swept51)
check("and says so", err51 == "archive not writable", err51)
check("so the notice is still reviewable next round",
      nid(st51.notices) == ["s1"], st51.notices)

# --- a tick withdraws the notice too ---
# Found by the OOP review, by execution. A message can be both a todo and a
# notice, so the user can tick the todo and promote the notice before the same
# commit. consume_checked removes the todo, so consume_promotes then sees no
# live duplicate and re-adds it as a fresh unticked todo. reconcile_todos does
# drop it again, since `ticked` is its drop set, but only while promotion runs
# first, which nothing enforced.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.PROMOTE_PATH)
st54 = sm.State({"horizon": 100, "roundSeq": 54,
                 "todos": [{"id": "X", "action": "check the charges",
                            "createdRound": 53}],
                 "notices": [{"id": "X", "summary": "pushed", "noticedRound": 53}]})
json.dump({"checkedIds": ["X"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
json.dump({"promoteIds": ["X"]}, open(sm.PROMOTE_PATH, "w", encoding="utf-8"))
st54.consume_checked()
check("a tick withdraws the notice, not just the todo",
      st54.notices == [], st54.notices)
moved54, _ = st54.consume_promotes()
check("so a promote click left over from before the tick cannot resurrect it",
      st54.todos == [] and moved54 == [], (st54.todos, moved54))

# The same thing through the real commit, where the order is settle_notices'
# problem rather than cmd_commit's. The archive starts empty so the count below
# is about this message only; consume_checked above already put X in there.
_clear(sm.ARCHIVE_PATH)
st55 = sm.State({"horizon": 100, "roundSeq": 55,
                 "todos": [{"id": "Y", "action": "finished", "createdRound": 54}],
                 "notices": [{"id": "Y", "summary": "pushed", "noticedRound": 54}]})
st55.save(); sm.Progress(200).save()
json.dump({}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
json.dump({"checkedIds": ["Y"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
json.dump({"promoteIds": ["Y"]}, open(sm.PROMOTE_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a55 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("commit leaves nothing on the list for a ticked-and-promoted message",
      a55["todos"] == [] and a55["notices"] == [], (a55["todos"], a55["notices"]))
check("and it is archived exactly once",
      [a["id"] for a in _read_arch()] == ["Y"], _read_arch())
_clear(sm.CHECKED_PATH, sm.PROMOTE_PATH, sm.ARCHIVE_PATH)

# --- a push and a tick landing in the same round ---
# Second finding of the OOP review, same family as the one above. A retry can
# genuinely succeed this round while the user ticks the same message between
# begin and commit. `pushed` is built from the reported notifiedIds, before
# `notified` absorbs the ticks, so without a filter the message is archived and
# handed a fresh notice in the same commit -- and promoting that notice later
# resurrects finished work, with nothing left to drop it.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.PROMOTE_PATH)
st58 = sm.State({"horizon": 100, "roundSeq": 58,
                 "pendingNotify": [{"id": "Z", "summary": "retry finally worked"}],
                 "todos": [{"id": "Z", "action": "the actual next step",
                            "createdRound": 57}]})
st58.save(); sm.Progress(200).save()
json.dump({"notifiedIds": ["Z"]}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
json.dump({"checkedIds": ["Z"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a58 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("a message ticked this round gets no notice, even if the push succeeded",
      a58["notices"] == [], a58["notices"])
check("it is archived with the todo's own action, not the push summary",
      [(a["id"], a.get("action")) for a in _read_arch()]
      == [("Z", "the actual next step")], _read_arch())
check("and it leaves pendingNotify", a58["pendingNotify"] == [], a58["pendingNotify"])
_clear(sm.CHECKED_PATH, sm.ARCHIVE_PATH)

# --- 重要事項 only holds pushed mail the todo list will never show ---
# Reported from the GUI: a mail with both a push and a next step got a row
# under 重要事項 and another under 這次新增, and its 加到待辦 button could not do
# anything, because consume_promotes skips a live duplicate. The section is for
# the opposite case, mail judged important with nothing to complete.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.PROMOTE_PATH)
st59 = sm.State({"horizon": 100, "roundSeq": 59,
                 "todos": [{"id": "old", "action": "from round 58",
                            "createdRound": 58}]})
filed59, owned59 = st59.record_notices(
    [{"id": "old", "summary": "pushed again"},
     {"id": "same", "summary": "statement is out"},
     {"id": "solo", "summary": "a person wrote to you"}], NOW, {"same"})
check("a push reported as a todo this round files no notice",
      nid(st59.notices) == ["solo"], st59.notices)
check("and the counts separate what was filed from what a todo owns",
      (filed59, owned59) == (1, 2), (filed59, owned59))

# A message can become a todo rounds after it was notified, so the rule has to
# reach notices already on the queue, not just this round's pushes.
st60 = sm.State({"horizon": 100, "roundSeq": 60,
                 "notices": [{"id": "late", "summary": "pushed last round",
                              "noticedRound": 59}]})
filed60, owned60 = st60.record_notices([], NOW, {"late"})
check("a notice already filed leaves when its message turns into a todo",
      st60.notices == [] and (filed60, owned60) == (0, 1),
      (st60.notices, filed60, owned60))
swept60, _serr60 = st60.sweep_notices(NOW)
check("dropped rather than swept, so 已封存 gets no 復原 button for live work",
      (swept60, _read_arch()) == (0, []), _read_arch())

st61 = sm.State({"horizon": 100, "roundSeq": 61,
                 "todos": [{"id": "t1", "action": "unrelated"}],
                 "notices": [{"subject": "no id at all", "noticedRound": 61}]})
filed61, owned61 = st61.record_notices([], NOW, set())
check("an id-less notice matches no todo, so the rule cannot reach it",
      len(st61.notices) == 1 and (filed61, owned61) == (0, 0),
      (st61.notices, filed61, owned61))

# The same thing through the real commit, which is where the reported ids and
# the pushed ids finally meet.
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.PROMOTE_PATH)
sm.State({"horizon": 100, "roundSeq": 62}).save()
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
check("a mail pushed and made a todo in one round appears on the list only",
      nid(a62["todos"]) == ["PP"] and a62["notices"] == [],
      (a62["todos"], a62["notices"]))
check("the row keeps the action the LLM wrote, not the push summary",
      a62["todos"][0]["action"] == "log in to PayPal and reconcile August",
      a62["todos"][0])
check("commit reports the suppression rather than hiding it",
      (held[-1]["newNoticesThisRound"], held[-1]["noticesSkippedAsTodo"])
      == (0, 1), held[-1])
check("the new todo is still counted, so the round still has something to open",
      held[-1]["newTodosThisRound"] == 1, held[-1])
_clear(sm.ARCHIVE_PATH)

# --- settle_notices owns the order, so callers cannot get it wrong ---
st56 = sm.State({"horizon": 100, "roundSeq": 56,
                 "notices": [{"id": "carry", "subject": "from last round",
                              "noticedRound": 55},
                             {"id": "repush", "subject": "notified again",
                              "noticedRound": 55}]})
json.dump({"promoteIds": ["carry"]}, open(sm.PROMOTE_PATH, "w", encoding="utf-8"))
out56 = st56.settle_notices([{"id": "repush", "subject": "notified again"},
                             {"id": "brandnew", "subject": "first push"}], NOW, set())
check("settle_notices promotes before sweeping, so the request is not discarded",
      nid(out56["promoted"]) == ["carry"] and nid(st56.todos) == ["carry"],
      (out56["promoted"], st56.todos))
check("it files before sweeping, so a re-push is not archived and re-filed",
      nid(st56.notices) == ["brandnew", "repush"], st56.notices)
check("and nothing was swept, because nothing was left alone",
      out56["swept"] == 0 and _read_arch() == [], (out56["swept"], _read_arch()))
check("the counts it reports match what it did",
      (out56["newNotices"], out56["skippedAsTodo"],
       out56["promoteBlocked"], out56["sweepBlocked"]) == (2, 0, "", ""), out56)
_clear(sm.PROMOTE_PATH, sm.ARCHIVE_PATH)

# --- an id-less notice now shows up in itemsMissingId ---
st57 = sm.State({"horizon": 100, "roundSeq": 57,
                 "notices": [{"subject": "no id"}, {"id": "fine"}]})
check("orphan_count covers the notice queue as well", st57.orphan_count() == 1,
      st57.orphan_count())

# --- the ordering bug the notice section came from ---
# begin handed out a notification before commit consumed the ticks, so a round
# pushed a toast for work the user had already marked done.
_clear(sm.ARCHIVE_PATH, sm.PROMOTE_PATH)
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

# --- end to end: a push this round is a notice, a tick still wins ---
_clear(sm.ARCHIVE_PATH, sm.CHECKED_PATH, sm.RESTORE_PATH, sm.PROMOTE_PATH)
st50 = sm.State({"horizon": 100, "roundSeq": 50,
                 "pendingNotify": [{"id": "e1", "subject": "statement",
                                    "summary": "confirm the charges"}],
                 "todos": [{"id": "tick1", "action": "finished"}]})
st50.save(); sm.Progress(200).save()
json.dump({"notifiedIds": ["e1"]}, open(sm.ROUND_PATH, "w", encoding="utf-8"))
json.dump({"checkedIds": ["tick1"]}, open(sm.CHECKED_PATH, "w", encoding="utf-8"))
sm.cmd_commit(None)
a50 = json.load(open(sm.STATE_PATH, encoding="utf-8"))
check("commit files the pushed message as a notice",
      nid(a50["notices"]) == ["e1"], a50["notices"])
check("with the text the toast used",
      a50["notices"][0]["summary"] == "confirm the charges", a50["notices"])
check("and clears it from pendingNotify",
      a50["pendingNotify"] == [], a50["pendingNotify"])
# A tick is finished work. Filing it as a notice would put it back in front of
# the user, which is the mismatch this whole section exists to end.
check("a ticked todo is archived, never filed as a notice",
      "tick1" not in nid(a50["notices"]), a50["notices"])
check("it went to the archive instead", "tick1" in nid(_read_arch()), _read_arch())
_clear(sm.CHECKED_PATH)

# --- step and commit must also report corruption instead of crashing ---
open(sm.STATE_PATH, "w").write("{ broken")
sm.Progress(200).save()
for label, fn, arg in (("step", sm.cmd_step, type("A", (), {"failed": True, "lo": 0, "hi": 0, "count": 0})()),
                       ("commit", sm.cmd_commit, None)):
    try:
        check(f"cmd_{label} aborts cleanly on corrupt state", fn(arg) == 2)
    except Exception as exc:
        check(f"cmd_{label} aborts cleanly on corrupt state", False, type(exc).__name__)

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
