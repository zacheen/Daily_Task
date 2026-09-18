"""Does the todo GUI really exit when no page is open?

test_task_list_gui.py covers `exit_reason` as a pure function, proving the policy
but not that the watchdog thread ever acts on it. That gap matters more here:
an unattended scheduled run starts this server, so a watchdog that never fires
leaves a resident process nobody closes, defeating the whole reason the task
is allowed to launch it.

Runs the real module on a spare port with the timeouts shrunk, then drives it
over HTTP. No browser is opened, because the runner calls app.run itself
instead of going through the module's __main__ block.

Case 3 posts /api/bye itself, so it proves the endpoint and the watchdog, NOT
that a browser ever calls it. Do not read its timing as the real close latency.
The page side was verified by hand instead, on 2026-09-09 against a real
browser: the ping fired every 15s, and pagehide's beacon reached /api/bye,
after which the server exited within a second. Note that a renderer killed
outright (an automated tab close, a crash) runs no unload handler and so sends
no beacon; that is precisely the case CLIENT_TIMEOUT backstops, measured to
fire exactly 150s after the last touch.

    conda run -n ML python test_task_list_gui_exit.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

# Email_Check/, one level up from test/. The viewer sits under task_list/.
CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(CODE, "task_list", "task_list_gui.py")
WORK = tempfile.mkdtemp(prefix="guiexit.")
# sys.executable, so this uses whichever interpreter ran the test rather than
# hunting for the conda env by path.
PY = sys.executable

RUNNER = os.path.join(WORK, "runner.py")
with open(RUNNER, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(
        "import importlib.util, sys, threading, time\n"
        "spec = importlib.util.spec_from_file_location('tg', %r)\n"
        "tg = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(tg)\n"
        "tg.STARTUP_GRACE = float(sys.argv[2])\n"
        "tg.CLIENT_TIMEOUT = float(sys.argv[3])\n"
        "tg.WATCHDOG_TICK = 0.4\n"
        "tg._started = time.time()\n"
        "threading.Thread(target=tg._watchdog, daemon=True).start()\n"
        "tg.app.run(host='127.0.0.1', port=int(sys.argv[1]),\n"
        "           debug=False, use_reloader=False)\n" % SRC)

fails = []


def check(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


def spawn(port, grace, timeout):
    return subprocess.Popen([PY, RUNNER, str(port), str(grace), str(timeout)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def serve(port, grace, timeout):
    """A running server, warmed up without registering a client.

    The warm-up deliberately sends no cid: _touch("") is a no-op, so it cannot
    leave a phantom page alive and mask the very exit being tested.
    """
    proc = spawn(port, grace, timeout)
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/todos", timeout=1)
            return proc
        except Exception:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"server on {port} never came up")


def post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 method="POST", data=b"")
    urllib.request.urlopen(req, timeout=2).read()


def died_within(proc, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if proc.poll() is not None:
            return True
        time.sleep(0.2)
    return False


def reap(proc):
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


try:
    # 1. The unattended failure that used to justify banning scheduled launches
    #    outright: webbrowser.open silently does nothing and nobody ever calls.
    #    Driven without a warm-up, since the grace runs from process start.
    p = spawn(8771, 3, 60)
    check("a browser that never connects exits after the startup grace",
          died_within(p, 15))
    reap(p)

    # 2. No beacon arrives: crash, sleep, or a tab Chrome discarded (discard
    #    runs no page callbacks, so the ping timeout is the only backstop).
    p = serve(8772, 60, 3)
    post(8772, "/api/ping?cid=a")
    check("a live page is not killed inside the timeout window",
          not died_within(p, 2))
    check("a page gone quiet exits", died_within(p, 15))
    reap(p)

    # 3. The normal case.
    p = serve(8773, 60, 60)
    post(8773, "/api/ping?cid=a")
    started = time.time()
    post(8773, "/api/bye?cid=a")
    check("closing the page exits promptly", died_within(p, 8),
          "%.1fs" % (time.time() - started))
    reap(p)

    # 4. The case a single last-seen timestamp gets wrong: one tab closing must
    #    not take the other one's server with it.
    p = serve(8774, 60, 60)
    post(8774, "/api/ping?cid=a")
    post(8774, "/api/ping?cid=b")
    post(8774, "/api/bye?cid=a")
    check("a surviving second tab is not killed", not died_within(p, 4))
    post(8774, "/api/bye?cid=b")
    check("closing the second tab exits", died_within(p, 8))
    reap(p)
finally:
    shutil.rmtree(WORK, ignore_errors=True)

print()
print("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails))
sys.exit(1 if fails else 0)
