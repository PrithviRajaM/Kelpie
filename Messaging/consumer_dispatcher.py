r"""
Centralised consumer dispatcher for the Kelpie RabbitMQ consumers.

This is the consuming-side counterpart to ``Messaging/queue_publisher.py``.
Where the publisher posts a message to *one* queue, this dispatcher drives
*all* of the project's consumers from a single place: on each pass it drains
every consumer listed in ``queue.config`` (``consumers`` block), waits a fixed
delay, and repeats until an entire pass processes nothing - at which point the
queues are empty and the loop exits.

Why a dispatcher (and not the always-on ``consume_forever`` services)
---------------------------------------------------------------------
The individual consumers (``Swagman/queue_consumer.py``,
``Tasks/local_ai_consumer.py``, ``Tasks/fallback_consumer.py``) can each run as
a permanent service. This dispatcher instead uses their one-shot ``drain()``
entry points so the whole fleet can be woken on demand - the instant something
is published - run until the work is done, then stop. That keeps idle machines
quiet while still processing messages promptly.

Behaviour (the guarantees this module provides)
------------------------------------------------
* **Runs independently of its trigger.** :func:`start_if_idle` spawns the loop
  as a *detached* child process, so the publisher (or whoever triggered it)
  can exit immediately without stopping the dispatcher.
* **Single instance.** A small state file (``consumer_dispatcher_state.json``,
  next to this module, mirroring ``Tasks/task_execution_state.json``) records
  the running loop's PID and status. :func:`start_if_idle` is a no-op while a
  live instance is already running, so repeated publishes never stack up
  multiple loops.
* **Fixed delay per run.** Each pass is followed by :data:`RUN_DELAY_SECONDS`
  (10s) before the next pass.
* **Stops when there is nothing left.** The loop exits once a full pass drains
  ``0`` messages across *all* consumers.
* **Starts on publish.** ``queue_publisher.publish_message`` calls
  :func:`start_if_idle` after every successful publish, so any published
  message wakes the loop if it is not already running.

Usage
-----
Trigger the loop (used by the publisher; safe to call repeatedly)::

    from Messaging.consumer_dispatcher import start_if_idle
    start_if_idle()

Run the loop in the foreground (what the detached child actually executes)::

    py -3 Messaging/consumer_dispatcher.py --run

Run a single drain pass across all consumers and exit (diagnostics)::

    py -3 Messaging/consumer_dispatcher.py --once
"""

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Callable, Optional

# Resolve paths relative to the Kelpie project root (one level up from
# Messaging/) so the consumer modules import cleanly regardless of the working
# directory the dispatcher is launched from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
_LOGGER_DIR = os.path.join(PROJECT_ROOT, "Logger")
if _LOGGER_DIR not in sys.path:
    sys.path.insert(0, _LOGGER_DIR)

try:
    import kelpie_logger as logger
except Exception:  # pragma: no cover - logging must never break dispatch
    logger = None

SCRIPT_NAME = "consumer_dispatcher.py"

#: Seconds to wait between drain passes.
RUN_DELAY_SECONDS = 10.0

#: Path to the messaging config that lists the consumers to drive.
CONFIG_PATH = os.path.join(SCRIPT_DIR, "queue.config")

#: Path to the dispatcher's execution-state file (single-instance guard),
#: co-located with this module and mirroring Tasks/task_execution_state.json.
STATE_PATH = os.path.join(SCRIPT_DIR, "consumer_dispatcher_state.json")


# ---------------------------------------------------------------------------
# Logging shim
#
# The dispatcher may run before/after any task session, so it simply uses the
# global Kelpie logger when available and degrades to stderr otherwise. It must
# never raise from a logging call.
# ---------------------------------------------------------------------------
def _log_info(message: str) -> None:
    if logger is not None:
        try:
            logger.log_info(SCRIPT_NAME, message)
            return
        except Exception:
            pass
    print(f"[INFO] {SCRIPT_NAME}: {message}", file=sys.stderr)


def _log_error(message: str) -> None:
    if logger is not None:
        try:
            logger.log_error(SCRIPT_NAME, message)
            return
        except Exception:
            pass
    print(f"[ERROR] {SCRIPT_NAME}: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    """Read ``queue.config`` and return it as a dict (tolerant of failure)."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _consumer_entries() -> list:
    """Return the configured consumer entries as a list of dicts.

    Each entry is ``{"name", "module", "drain", "queue_key"}``. Entries missing
    a module or drain function name are skipped (with a logged warning) so a
    partial/misconfigured file cannot crash the loop.
    """
    config = _load_config()
    consumers = config.get("consumers")
    if not isinstance(consumers, dict):
        return []

    entries = []
    for name, conf in consumers.items():
        if not isinstance(conf, dict):
            continue
        module = conf.get("module")
        drain = conf.get("drain", "drain")
        if not isinstance(module, str) or not module.strip():
            _log_error(f"Consumer '{name}' has no 'module'; skipping.")
            continue
        entries.append(
            {
                "name": name,
                "module": module.strip(),
                "drain": drain if isinstance(drain, str) and drain.strip() else "drain",
                "queue_key": conf.get("queue_key", name),
            }
        )
    return entries


def _resolve_drain(entry: dict) -> Optional[Callable[[], int]]:
    """Import a consumer module and return its drain callable, or None.

    The drain callable is invoked with no arguments; every Kelpie consumer's
    ``drain()`` builds its own settings from ``queue.config`` when called
    without arguments, so no wiring of connection settings is needed here.
    """
    try:
        module = importlib.import_module(entry["module"])
    except Exception as exc:  # noqa: BLE001 - report and skip a bad module
        _log_error(f"Could not import consumer module '{entry['module']}': {exc}")
        return None

    func = getattr(module, entry["drain"], None)
    if not callable(func):
        _log_error(
            f"Consumer module '{entry['module']}' has no callable "
            f"'{entry['drain']}'; skipping."
        )
        return None
    return func


# ---------------------------------------------------------------------------
# Execution state (single-instance guard)
# ---------------------------------------------------------------------------
def _read_state() -> dict:
    """Return the dispatcher state file as a dict (empty if absent/broken)."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    """Persist the dispatcher state file (best effort)."""
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
    except OSError as exc:
        _log_error(f"Could not write dispatcher state {STATE_PATH}: {exc}")


def _pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` is currently running.

    Uses the OS process table so a stale state file (e.g. after a crash) does
    not permanently block future runs.
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # tasklist is available on all supported Windows versions.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            # If we cannot check, assume alive to avoid spawning a duplicate.
            return True
        return str(pid) in (out.stdout or "")
    # POSIX: signal 0 probes existence without affecting the process.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_running() -> bool:
    """Return True if a live dispatcher loop is already running.

    A state file that names a PID which is no longer alive is treated as not
    running (and is not trusted), so a crashed run self-heals on the next
    trigger.
    """
    state = _read_state()
    if state.get("status") != "running":
        return False
    pid = state.get("pid")
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        
        return False
    if pid == os.getpid():
        return True
    return _pid_alive(pid)


def _mark_running() -> None:
    _write_state(
        {
            "status": "running",
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(),
        }
    )


def _mark_stopped(passes: int, total_handled: int) -> None:
    _write_state(
        {
            "status": "stopped",
            "pid": os.getpid(),
            "stopped_at": datetime.now().isoformat(),
            "last_run_passes": passes,
            "last_run_messages": total_handled,
        }
    )


# ---------------------------------------------------------------------------
# Draining
# ---------------------------------------------------------------------------
def drain_all_once() -> int:
    """Drain every configured consumer exactly once; return total handled.

    Each consumer's ``drain()`` is called in turn. A failure in one consumer is
    logged and treated as ``0`` handled for that consumer, so one broken
    consumer never aborts the whole pass.
    """
    entries = _consumer_entries()
    if not entries:
        _log_error("No consumers configured in queue.config ('consumers' block).")
        return 0

    total = 0
    for entry in entries:
        func = _resolve_drain(entry)
        if func is None:
            continue
        try:
            handled = func() or 0
        except Exception as exc:  # noqa: BLE001 - one bad consumer must not stop others
            _log_error(f"Consumer '{entry['name']}' drain failed: {exc}")
            continue
        try:
            handled = int(handled)
        except (ValueError, TypeError):
            handled = 0
        if handled:
            _log_info(f"Consumer '{entry['name']}' handled {handled} message(s).")
        total += handled
    return total


def run_loop(delay: float = RUN_DELAY_SECONDS) -> None:
    """Run drain passes until a full pass handles nothing, then stop.

    On each iteration every configured consumer is drained once. If the pass
    handled at least one message there may be follow-on work (a drained message
    can publish to another queue - e.g. a Swagman result posts to fallback,
    which posts the next step to local_AI), so the loop waits ``delay`` seconds
    and runs another pass. The loop exits the first time a whole pass handles
    zero messages, meaning every queue is empty.

    The single-instance guard is maintained here: the state file is marked
    ``running`` for the life of the loop and ``stopped`` on exit (even on
    error), so :func:`start_if_idle` can tell whether a live loop exists.
    """
    _mark_running()
    passes = 0
    total_handled = 0
    _log_info("Consumer dispatcher started; draining all consumers.")
    try:
        while True:
            passes += 1
            handled = drain_all_once()
            total_handled += handled
            if handled == 0:
                _log_info(
                    f"No messages across any consumer; stopping after "
                    f"{passes} pass(es) ({total_handled} message(s) total)."
                )
                return
            time.sleep(delay)
    finally:
        _mark_stopped(passes, total_handled)


# ---------------------------------------------------------------------------
# Trigger (start the loop detached, only if idle)
# ---------------------------------------------------------------------------
def start_if_idle() -> bool:
    """Start the dispatcher loop as a detached process, unless one is running.

    This is the entry point the publisher calls after a successful publish. It
    is safe to call from any process and as often as desired:

    * If a live dispatcher loop is already running, it returns ``False`` without
      doing anything (so concurrent publishes never stack up loops).
    * Otherwise it spawns ``python consumer_dispatcher.py --run`` as a *detached*
      child - fully independent of the caller, which may exit immediately - and
      returns ``True``.

    Any failure to spawn is logged and swallowed (returns ``False``) so a
    dispatch problem can never break the publish that triggered it.
    """
    try:
        if is_running():
            return False

        creationflags = 0
        close_fds = True
        kwargs = {}
        if os.name == "nt":
            # DETACHED_PROCESS: no console; CREATE_NEW_PROCESS_GROUP: not tied
            # to the parent's Ctrl+C. Together they let the child outlive the
            # publisher's process/console.
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            # Start a new session so the child is not killed with the parent.
            kwargs["start_new_session"] = True

        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--run"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=close_fds,
            creationflags=creationflags,
            **kwargs,
        )
        _log_info("Consumer dispatcher spawned (detached).")
        return True
    except Exception as exc:  # noqa: BLE001 - triggering must never break the caller
        _log_error(f"Failed to start consumer dispatcher: {exc}")
        return False


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Centralised dispatcher that drains all Kelpie consumers."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--run",
        action="store_true",
        help="Run the drain loop in the foreground until all queues are empty "
        "(this is what the detached child executes).",
    )
    group.add_argument(
        "--once",
        action="store_true",
        help="Run a single drain pass across all consumers and exit.",
    )
    group.add_argument(
        "--start",
        action="store_true",
        help="Start the loop as a detached process if one is not already "
        "running, then exit immediately.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=RUN_DELAY_SECONDS,
        help=f"Seconds between drain passes (default {RUN_DELAY_SECONDS:.0f}).",
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    if args.once:
        drain_all_once()
    elif args.start:
        started = start_if_idle()
        print("started" if started else "already running")
    else:
        # Default (and --run) run the loop in the foreground.
        run_loop(delay=args.delay)
    return 0


if __name__ == "__main__":
    sys.exit(main())
