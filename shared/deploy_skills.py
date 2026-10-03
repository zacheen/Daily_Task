"""Deploy scheduled-task SKILL.md files from a repo to where the app reads them.

The app reads ~/.claude/scheduled-tasks/<task-id>/SKILL.md afresh at every run,
and that folder has no version control. Each repo keeps its stubs as
Scheduled_Tasks/<task-id>.md, and this tool copies every one of them over with
the report-language line from skill_deploy.toml appended. That config is a
gitignored copy of the tracked skill_deploy.example.toml beside it.

A deployed file that differs from what this tool last wrote was edited elsewhere
(the app's editor or update_scheduled_task), so it is never overwritten silently.
The tool prints the diff and leaves that task alone unless it is named in
--replace-edited.

    conda run --no-capture-output -n ML python <path to this file> <repo>
    ... --check                         compare only, exit 1 if anything differs
    ... --replace-edited TASK_ID [...]  overwrite those tasks even if edited elsewhere
"""

import argparse
import difflib
import hashlib
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path

DEFAULTS_PATH = Path(__file__).resolve().parent / "skill_deploy.toml"

# The app never passes frontmatter to the model, so the line goes in the body.
# Rule files are English to save tokens, so without it all user-facing output
# would be English. Verbatim strings stay exempt because allow rules and
# self-checks match them exactly.
LANGUAGE_LINE = (
    "Write everything this run produces for the user to read in {language}. That covers your "
    "final reply, report files, notification messages and any text that appears in a list or "
    "page the user opens. Commands, file paths, code, and any heading, label or string a rule "
    "file says to copy exactly stay as written."
)


class DeployError(Exception):
    pass


def tasks_root():
    return Path.home() / ".claude" / "scheduled-tasks"


def state_path():
    # Machine-local on purpose: it records what was last written to this
    # machine's copies, so a shared copy would be wrong on another machine.
    return Path.home() / ".claude" / "skill_deploy_state.json"


def read_config(path):
    if not path.is_file():
        example = path.with_name(f"{path.stem}.example{path.suffix}")
        if example.is_file():
            raise DeployError(f"{path} does not exist. Copy {example.name} beside it to {path.name} and edit the copy")
        raise DeployError(f"{path} does not exist")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_tasks(repo, defaults_path=None):
    """Return (task_id, source_path, language) for each Scheduled_Tasks/<task_id>.md in repo."""
    # Resolved at call time rather than as a default argument, so tests can
    # point DEFAULTS_PATH at a sandbox.
    if defaults_path is None:
        defaults_path = DEFAULTS_PATH
    language = read_config(defaults_path).get("report_language")
    if not language:
        raise DeployError(f"{defaults_path} sets no report_language")
    # Resolved first because Path(".").name is empty, which would send a run
    # from inside Scheduled_Tasks looking for Scheduled_Tasks/Scheduled_Tasks.
    repo = repo.resolve()
    folder = repo if repo.name == "Scheduled_Tasks" else repo / "Scheduled_Tasks"
    if not folder.is_dir():
        raise DeployError(f"{folder} does not exist")
    sources = sorted(folder.glob("*.md"))
    if not sources:
        raise DeployError(f"{folder} holds no .md stubs")
    return [(source.stem, source, language) for source in sources]


def frontmatter_name(text):
    lines = text.split("\n")
    if lines[0] != "---":
        return None
    for line in lines[1:]:
        if line == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip()
    return None


def render(task_id, source, language):
    if not source.is_file():
        raise DeployError(f"{task_id}: source {source} does not exist")
    text = source.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    # The id is the file stem, so a frontmatter name that differs means one was
    # renamed without the other, or the stub was copied from another task.
    name = frontmatter_name(text)
    if name != task_id:
        raise DeployError(f"{task_id}: {source} has frontmatter name {name!r}")
    body = text.rstrip("\n") + "\n\n" + LANGUAGE_LINE.format(language=language) + "\n"
    return body.encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_state(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DeployError(f"{path} is not valid JSON ({error}), fix or delete it") from error


def write_atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".deploy.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def print_diff(task_id, current, rendered):
    diff = difflib.unified_diff(
        current.decode("utf-8", errors="replace").splitlines(),
        rendered.decode("utf-8").splitlines(),
        fromfile=f"deployed/{task_id}/SKILL.md",
        tofile=f"repo/{task_id}",
        lineterm="",
    )
    for line in diff:
        print("    " + line)


def deploy(tasks, root, state_file, check=False, replace_edited=frozenset()):
    """Deploy or compare every task. Return True when all of them end up matching the repo."""
    unknown = set(replace_edited) - {task_id for task_id, _, _ in tasks}
    if unknown:
        raise DeployError(f"--replace-edited names tasks with no stub in this repo: {', '.join(sorted(unknown))}")
    state = read_state(state_file)
    # Render every task before writing any, so a broken source deploys nothing
    # rather than half the tasks.
    rendered_tasks = [(task_id, render(task_id, source, language)) for task_id, source, language in tasks]
    try:
        return _deploy_rendered(rendered_tasks, root, state, check, replace_edited)
    finally:
        # A file written without its hash recorded would read as an outside
        # edit next run, so save the state even when a write fails.
        if not check:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(state_file, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _deploy_rendered(rendered_tasks, root, state, check, replace_edited):
    all_match = True
    for task_id, rendered in rendered_tasks:
        dest = root / task_id / "SKILL.md"
        if not dest.parent.is_dir():
            # Registering a task belongs to the app, so a missing folder is
            # reported, not created. A bare SKILL.md would never run.
            print(f"{task_id}: NO TASK, {dest.parent} does not exist, create the task in the app first")
            all_match = False
            continue
        current = dest.read_bytes() if dest.exists() else None
        if current == rendered:
            print(f"{task_id}: up to date")
            if not check:
                state[task_id] = sha256(rendered)
            continue
        edited = current is not None and state.get(task_id) != sha256(current)
        if edited and task_id not in replace_edited:
            reason = "EDITED OUTSIDE THIS TOOL" if task_id in state else "NEVER DEPLOYED BY THIS TOOL"
            print(f"{task_id}: {reason}, left alone. Diff from the deployed copy to the repo version")
            print_diff(task_id, current, rendered)
            all_match = False
            continue
        if check:
            print(f"{task_id}: NOT DEPLOYED, the repo version changed since the last deploy")
            all_match = False
            continue
        write_atomic(dest, rendered)
        state[task_id] = sha256(rendered)
        print(f"{task_id}: deployed" + (" over an outside edit" if edited else ""))
    return all_match


def main(argv=None):
    # The diff carries Chinese, which the cp950 console encoding cannot print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Deploy scheduled-task SKILL.md files from a repo.")
    parser.add_argument("repo", type=Path, help="a repo root, or its Scheduled_Tasks folder")
    parser.add_argument("--check", action="store_true", help="compare only, write nothing")
    parser.add_argument("--replace-edited", nargs="+", default=[], metavar="TASK_ID")
    args = parser.parse_args(argv)
    if args.check and args.replace_edited:
        parser.error("--check writes nothing, so --replace-edited has no meaning with it")
    try:
        tasks = load_tasks(args.repo)
        all_match = deploy(tasks, tasks_root(), state_path(), args.check, frozenset(args.replace_edited))
    # Exit 1 means a task differs or was left alone, and an uncaught exception
    # would exit 1 too, so everything that stops the run (a mistyped path, a
    # file the app holds open) is caught here and returned as 2.
    except (DeployError, OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0 if all_match else 1


if __name__ == "__main__":
    sys.exit(main())
