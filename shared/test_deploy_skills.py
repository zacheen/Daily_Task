"""Tests for deploy_skills.py. Run the file directly, no pytest needed.

Every case works in a throwaway directory standing in for both the repo and
~/.claude/scheduled-tasks, so nothing real is read or written. That includes
shared/skill_deploy.toml, which is gitignored and absent from a fresh clone.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_skills as ds  # noqa: E402

FAILURES = []


def check(condition, message):
    if not condition:
        FAILURES.append(message)


def stub(task_id, body="Read INSTRUCTIONS.md and follow it."):
    return f"---\nname: {task_id}\ndescription: test task\n---\n\n{body}\n"


class Sandbox:
    def __init__(self, tmp):
        self.repo = tmp / "repo"
        self.stubs = self.repo / "Scheduled_Tasks"
        self.root = tmp / "scheduled-tasks"
        self.state = tmp / "state.json"
        self.defaults = tmp / "skill_deploy.toml"
        self.stubs.mkdir(parents=True)
        self.root.mkdir()
        self.defaults.write_text('report_language = "English"\n', encoding="utf-8")

    def add(self, task_id, text=None, make_folder=True):
        (self.stubs / f"{task_id}.md").write_bytes((text or stub(task_id)).encode("utf-8"))
        if make_folder:
            (self.root / task_id).mkdir(exist_ok=True)

    def run(self, **kwargs):
        tasks = ds.load_tasks(self.repo, self.defaults)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = ds.deploy(tasks, self.root, self.state, **kwargs)
        return result, out.getvalue()

    def deployed(self, task_id):
        return (self.root / task_id / "SKILL.md").read_bytes()


def test_first_deploy(sb):
    sb.add("t1")
    ok, out = sb.run()
    text = sb.deployed("t1").decode("utf-8")
    check(ok, "first deploy into an empty task folder should succeed")
    check(text.startswith(stub("t1").rstrip("\n")), "deployed file should start with the source verbatim")
    check(text.rstrip("\n").endswith(ds.LANGUAGE_LINE.format(language="English")), "language line should end the file")
    check("deployed" in out, "output should say the task was deployed")


def test_up_to_date_writes_nothing(sb):
    sb.add("t1")
    sb.run()
    before = (sb.root / "t1" / "SKILL.md").stat().st_mtime_ns
    ok, out = sb.run()
    check(ok and "up to date" in out, "second run with no change should report up to date")
    check((sb.root / "t1" / "SKILL.md").stat().st_mtime_ns == before, "an up-to-date task must not be rewritten")


def test_repo_change_redeploys(sb):
    sb.add("t1")
    sb.run()
    (sb.stubs / "t1.md").write_text(stub("t1", "New body."), encoding="utf-8")
    ok, _ = sb.run()
    check(ok and b"New body." in sb.deployed("t1"), "a repo change over an untouched deployed copy should redeploy")


def test_outside_edit_is_refused(sb):
    sb.add("t1")
    sb.run()
    edited = sb.deployed("t1") + b"edited in the app\n"
    (sb.root / "t1" / "SKILL.md").write_bytes(edited)
    (sb.stubs / "t1.md").write_text(stub("t1", "New body."), encoding="utf-8")
    ok, out = sb.run()
    check(not ok, "an outside edit should make the run fail")
    check(sb.deployed("t1") == edited, "an outside edit must survive the run")
    check("EDITED OUTSIDE" in out and "+New body." in out, "the refusal should print the diff")


def test_replace_edited_overrides(sb):
    sb.add("t1")
    sb.run()
    (sb.root / "t1" / "SKILL.md").write_bytes(b"edited in the app\n")
    ok, out = sb.run(replace_edited=frozenset({"t1"}))
    check(ok and b"Read INSTRUCTIONS.md" in sb.deployed("t1"), "--replace-edited should overwrite the outside edit")
    check("over an outside edit" in out, "the output should say it overwrote an outside edit")
    ok, _ = sb.run()
    check(ok, "after a forced deploy the next plain run should be clean")


def test_existing_file_without_state_is_refused(sb):
    sb.add("t1")
    (sb.root / "t1" / "SKILL.md").write_bytes(b"written by the app before this tool existed\n")
    ok, out = sb.run()
    check(not ok and sb.deployed("t1").startswith(b"written by the app"), "a file the tool never wrote must not be overwritten")
    check("NEVER DEPLOYED" in out, "a file with no recorded deploy should be labelled as never deployed, not as edited")


def test_matching_file_without_state_is_adopted(sb):
    sb.add("t1")
    rendered = ds.render("t1", sb.stubs / "t1.md", "English")
    (sb.root / "t1" / "SKILL.md").write_bytes(rendered)
    ok, out = sb.run()
    check(ok and "up to date" in out, "a deployed copy that already matches should count as up to date")
    (sb.stubs / "t1.md").write_text(stub("t1", "New body."), encoding="utf-8")
    ok, _ = sb.run()
    check(ok and b"New body." in sb.deployed("t1"), "a matching copy should be adopted so the next repo change deploys")


def test_check_never_writes(sb):
    sb.add("t1")
    ok, out = sb.run(check=True)
    check(not ok and "NOT DEPLOYED" in out, "--check should report a pending deploy")
    check(not (sb.root / "t1" / "SKILL.md").exists(), "--check must not write the task file")
    check(not sb.state.exists(), "--check must not write the state file")


def test_missing_task_folder_is_not_created(sb):
    sb.add("t1", make_folder=False)
    ok, out = sb.run()
    check(not ok and "NO TASK" in out, "a task the app does not know should be reported")
    check(not (sb.root / "t1").exists(), "the tool must not create a task folder")


def test_name_mismatch_is_an_error(sb):
    sb.add("t1", text=stub("other-task"))
    try:
        sb.run()
        check(False, "a frontmatter name that differs from the task id should raise")
    except ds.DeployError:
        pass


def test_crlf_and_bom_source(sb):
    sb.add("t1")
    (sb.stubs / "t1.md").write_bytes(b"\xef\xbb\xbf" + stub("t1").replace("\n", "\r\n").encode("utf-8"))
    ok, _ = sb.run()
    check(ok and b"\r\n" not in sb.deployed("t1") and not sb.deployed("t1").startswith(b"\xef\xbb\xbf"),
          "a CRLF or BOM source should deploy as plain LF UTF-8")


def test_unknown_replace_target_is_an_error(sb):
    sb.add("t1")
    try:
        sb.run(replace_edited=frozenset({"typo"}))
        check(False, "--replace-edited naming an unknown task should raise")
    except ds.DeployError:
        pass


def test_broken_source_deploys_nothing(sb):
    sb.add("t1")
    sb.add("t2", text=stub("wrong-name"))
    try:
        sb.run()
        check(False, "a broken second source should raise")
    except ds.DeployError:
        pass
    check(not (sb.root / "t1" / "SKILL.md").exists(), "a broken source must stop the run before any task is written")


def test_midrun_failure_still_records_earlier_deploys(sb):
    sb.add("t1")
    sb.add("t2")
    (sb.root / "t2" / "SKILL.md").mkdir()  # reading or replacing a directory fails mid-run
    try:
        sb.run()
        check(False, "an unreadable deployed copy should raise")
    except OSError:
        pass
    state = json.loads(sb.state.read_text(encoding="utf-8")) if sb.state.exists() else {}
    check(state.get("t1") == ds.sha256(sb.deployed("t1")), "a task written before the failure must keep its hash in the state")


def test_load_errors(sb):
    for label, setup in {
        "a repo with no Scheduled_Tasks folder": lambda: sb.stubs.rmdir(),
        "a Scheduled_Tasks folder with no .md stubs": lambda: None,
    }.items():
        setup()
        try:
            ds.load_tasks(sb.repo, sb.defaults)
            check(False, f"{label} should raise")
        except ds.DeployError:
            pass
        sb.stubs.mkdir(exist_ok=True)
    sb.add("t1")
    sb.defaults.write_text("", encoding="utf-8")
    try:
        ds.load_tasks(sb.repo, sb.defaults)
        check(False, "a defaults file without report_language should raise")
    except ds.DeployError:
        pass


def test_only_md_stubs_are_tasks(sb):
    sb.add("t1")
    (sb.stubs / "notes.txt").write_text("not a stub", encoding="utf-8")
    tasks = ds.load_tasks(sb.repo, sb.defaults)
    check([task_id for task_id, _, _ in tasks] == ["t1"], "only .md files should become tasks, named by their stem")
    check(ds.load_tasks(sb.stubs, sb.defaults) == tasks, "the Scheduled_Tasks folder itself should work as the argument")
    cwd = Path.cwd()
    try:
        os.chdir(sb.stubs)
        check([task_id for task_id, _, _ in ds.load_tasks(Path("."), sb.defaults)] == ["t1"],
              "'.' run from inside Scheduled_Tasks should find the stubs there")
    finally:
        os.chdir(cwd)


def test_corrupt_state_is_an_error(sb):
    sb.add("t1")
    sb.state.write_text("{not json", encoding="utf-8")
    try:
        sb.run()
        check(False, "a corrupt state file should raise DeployError")
    except ds.DeployError:
        pass


def test_main_exit_codes(sb):
    real = ds.tasks_root, ds.state_path, ds.DEFAULTS_PATH
    ds.tasks_root, ds.state_path, ds.DEFAULTS_PATH = (lambda: sb.root), (lambda: sb.state), sb.defaults
    try:
        sb.add("t1")
        repo = str(sb.repo)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            check(ds.main([repo, "--check"]) == 1, "--check with a pending deploy should exit 1")
            check(ds.main([repo]) == 0, "a clean deploy should exit 0")
            check(ds.main([str(sb.repo / "missing")]) == 2, "a repo path that does not exist should exit 2")
            try:
                ds.main([repo, "--check", "--replace-edited", "t1"])
                check(False, "--check with --replace-edited should be rejected")
            except SystemExit as error:
                check(error.code == 2, "--check with --replace-edited should exit 2")
            sb.defaults.write_text("[[task]\n", encoding="utf-8")
            check(ds.main([repo]) == 2, "a skill_deploy.toml with a TOML syntax error should exit 2")
            sb.defaults.write_text('report_language = "English"\n', encoding="utf-8")
            sb.add("t2", text=stub("wrong-name"))
            check(ds.main([repo]) == 2, "a DeployError should exit 2")
    finally:
        ds.tasks_root, ds.state_path, ds.DEFAULTS_PATH = real


def test_missing_defaults_points_at_its_example(sb):
    sb.add("t1")
    example = sb.defaults.with_name("skill_deploy.example.toml")
    sb.defaults.rename(example)
    try:
        ds.load_tasks(sb.repo, sb.defaults)
        check(False, "a missing skill_deploy.toml should raise")
    except ds.DeployError as error:
        check("skill_deploy.example.toml" in str(error), "the error should name the example to copy")
    example.unlink()
    try:
        ds.load_tasks(sb.repo, sb.defaults)
        check(False, "a missing defaults file should raise")
    except ds.DeployError as error:
        check("example" not in str(error), "with no example beside it, the error should not invent one")


def test_state_records_written_hash(sb):
    sb.add("t1")
    sb.run()
    state = json.loads(sb.state.read_text(encoding="utf-8"))
    check(state.get("t1") == ds.sha256(sb.deployed("t1")), "state should hold the hash of the bytes written")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test(Sandbox(Path(tmp)))
            except Exception as error:  # a crash is a failure of that case, not of the run
                FAILURES.append(f"{test.__name__} raised {error!r}")
    for failure in FAILURES:
        print("FAIL", failure)
    print(f"{len(tests)} cases, {len(FAILURES)} failures")
    sys.exit(1 if FAILURES else 0)
