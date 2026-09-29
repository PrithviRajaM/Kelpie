r"""
Action Plan - Sole owner of a task run's action plan and action detail files.

A task run's orchestration is driven by two artifacts stored inside the current
InProgress session folder (``<task_dir>/InProgress/<session_counter>``):

``action_plan.json``
    An ordered, step-by-step plan for the run. Every step uses one universal
    format::

        {
            "step_id": 1,
            "action_owner": "Swagman",   # who acts (Swagman, Ollama, ...)
            "action_description": "Extract web content for the listed URLs.",
            "status": "not started"      # not started | in progress | completed | failed
        }

    The file wraps the ordered steps plus a little run metadata::

        {
            "task_identifier": "<InProgress session folder>",
            "created_at": "2026-09-23T10:00:00",
            "updated_at": "2026-09-23T10:05:00",
            "steps": [ { ...step... }, ... ]
        }

``action_detail.txt``
    A human-readable running record. It opens with the task prompt, and each
    action owner appends the outcome of its step under a ``Step ID`` heading so
    that a later step (especially a local-AI step) can understand what has
    happened so far and where produced resources live.

Design rules (per the task specification)
------------------------------------------
* The plan is **created first**; status updates and structural edits happen as
  steps progress.
* Structural edits (add / modify a step) may only touch steps whose status is
  ``not started``. A step that is in progress, completed, or failed is
  immutable except for its status, which advances through
  :func:`update_step_status`.
* This module is the single place that reads/writes these two files, so all
  concurrent producers (the task runner, the fallback orchestrator, the
  Swagman consumer and the local-AI consumer) share one contract.

This module deliberately depends only on the standard library and the Kelpie
logger, so any consumer/publisher process can import it cheaply.
"""

import json
import os
import sys
from datetime import datetime

# Resolve paths relative to the project root (one level up from Tasks/) so the
# logger imports cleanly regardless of the caller's working directory.
_TASKS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TASKS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if os.path.join(_PROJECT_ROOT, "Logger") not in sys.path:
    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "Logger"))

import kelpie_logger as logger

SCRIPT_NAME = "action_plan.py"

# Artifact filenames, stored inside the InProgress session folder.
ACTION_PLAN_FILENAME = "action_plan.json"
ACTION_DETAIL_FILENAME = "action_detail.txt"

# Canonical action owners.
OWNER_SWAGMAN = "Swagman"
OWNER_OLLAMA = "Ollama"

# Canonical step statuses.
STATUS_NOT_STARTED = "not started"
STATUS_IN_PROGRESS = "in progress"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

_VALID_STATUSES = {
    STATUS_NOT_STARTED,
    STATUS_IN_PROGRESS,
    STATUS_COMPLETED,
    STATUS_FAILED,
}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def plan_path(session_dir: str) -> str:
    """Return the absolute path to ``action_plan.json`` in a session folder."""
    return os.path.join(session_dir, ACTION_PLAN_FILENAME)


def detail_path(session_dir: str) -> str:
    """Return the absolute path to ``action_detail.txt`` in a session folder."""
    return os.path.join(session_dir, ACTION_DETAIL_FILENAME)


# ---------------------------------------------------------------------------
# Step construction
# ---------------------------------------------------------------------------

def make_step(step_id: int, action_owner: str, action_description: str,
              status: str = STATUS_NOT_STARTED) -> dict:
    """Build a single step dict in the universal step format.

    Args:
        step_id: Ordinal id of the step (1-based, unique within the plan).
        action_owner: Who acts on the step (e.g. ``"Swagman"`` or ``"Ollama"``).
        action_description: What the step should accomplish.
        status: Initial status; defaults to ``"not started"``.

    Returns:
        A step dict with keys ``step_id``, ``action_owner``,
        ``action_description`` and ``status``.
    """
    if status not in _VALID_STATUSES:
        status = STATUS_NOT_STARTED
    return {
        "step_id": int(step_id),
        "action_owner": action_owner,
        "action_description": action_description,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Create / read / write the plan
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_plan(session_dir: str, steps: list, task_identifier: str | None = None) -> dict | None:
    """Create ``action_plan.json`` for a run from an ordered list of steps.

    The plan is created first (before any step runs). If a plan already exists
    in ``session_dir`` it is left untouched and returned as-is, so re-invoking a
    run never clobbers an in-flight plan.

    Args:
        session_dir: The InProgress session folder for this run.
        steps: Ordered list of step dicts (use :func:`make_step`).
        task_identifier: Optional identifier stored in the plan metadata;
            defaults to ``session_dir``.

    Returns:
        The persisted plan dict, or None if it could not be written.
    """
    existing = read_plan(session_dir)
    if existing is not None:
        logger.log_info(
            SCRIPT_NAME,
            f"Action plan already exists at {plan_path(session_dir)}; keeping it.",
        )
        return existing

    plan = {
        "task_identifier": task_identifier or session_dir,
        "created_at": _now(),
        "updated_at": _now(),
        "steps": list(steps),
    }

    if _write_plan(session_dir, plan):
        logger.log_info(
            SCRIPT_NAME,
            f"Action plan created with {len(plan['steps'])} step(s) at {plan_path(session_dir)}.",
        )
        return plan
    return None


def read_plan(session_dir: str) -> dict | None:
    """Read and parse ``action_plan.json``. Returns None if absent/invalid."""
    path = plan_path(session_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.log_error(SCRIPT_NAME, f"Could not read action plan {path}: {e}")
        return None

    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        logger.log_error(SCRIPT_NAME, f"Action plan has unexpected structure: {path}")
        return None
    return data


def _write_plan(session_dir: str, plan: dict) -> bool:
    """Serialize ``plan`` to ``action_plan.json``. Returns success."""
    path = plan_path(session_dir)
    plan["updated_at"] = _now()
    try:
        os.makedirs(session_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=4)
        return True
    except OSError as e:
        logger.log_error(SCRIPT_NAME, f"Failed to write action plan {path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Step queries
# ---------------------------------------------------------------------------

def get_step(session_dir: str, step_id: int) -> dict | None:
    """Return the step with ``step_id`` from the plan, or None if not found."""
    plan = read_plan(session_dir)
    if plan is None:
        return None
    for step in plan["steps"]:
        if int(step.get("step_id", -1)) == int(step_id):
            return step
    return None


def next_actionable_step(session_dir: str) -> dict | None:
    """Return the first ``not started`` step in order, or None if none remain.

    If any step is currently ``in progress`` or ``failed``, None is returned so
    the orchestrator does not start a new step while one is active or the run
    has stalled on a failure.
    """
    plan = read_plan(session_dir)
    if plan is None:
        return None

    for step in plan["steps"]:
        status = step.get("status")
        if status == STATUS_IN_PROGRESS:
            # A step is already running; do not advance.
            return None
        if status == STATUS_FAILED:
            # The run has stalled on a failure; do not advance.
            return None

    for step in plan["steps"]:
        if step.get("status") == STATUS_NOT_STARTED:
            return step
    return None


def is_plan_complete(session_dir: str) -> bool:
    """Return True if every step in the plan is ``completed``."""
    plan = read_plan(session_dir)
    if plan is None:
        return False
    return bool(plan["steps"]) and all(
        step.get("status") == STATUS_COMPLETED for step in plan["steps"]
    )


# ---------------------------------------------------------------------------
# Step mutations
# ---------------------------------------------------------------------------

def update_step_status(session_dir: str, step_id: int, status: str) -> bool:
    """Advance a step's ``status``. Any existing step's status may change.

    This is the one mutation allowed on non-``not started`` steps: their status
    can always progress (e.g. ``not started`` -> ``in progress`` -> ``completed``).

    Args:
        session_dir: The InProgress session folder for this run.
        step_id: The step to update.
        status: The new status (must be one of the canonical statuses).

    Returns:
        True on success, False if the plan/step is missing or status invalid.
    """
    if status not in _VALID_STATUSES:
        logger.log_error(SCRIPT_NAME, f"Refusing to set invalid status '{status}'.")
        return False

    plan = read_plan(session_dir)
    if plan is None:
        logger.log_error(SCRIPT_NAME, f"No action plan to update in {session_dir}.")
        return False

    for step in plan["steps"]:
        if int(step.get("step_id", -1)) == int(step_id):
            step["status"] = status
            if _write_plan(session_dir, plan):
                logger.log_info(
                    SCRIPT_NAME,
                    f"Step {step_id} status set to '{status}' in {plan_path(session_dir)}.",
                )
                return True
            return False

    logger.log_error(SCRIPT_NAME, f"Step {step_id} not found in plan {plan_path(session_dir)}.")
    return False


def add_step(session_dir: str, action_owner: str, action_description: str,
             position: int | None = None) -> dict | None:
    """Add a new ``not started`` step to the plan.

    New steps are only ever inserted among (or after) other ``not started``
    steps: insertion is clamped so it can never land before a step that has
    already started/finished, preserving the rule that only ``not started``
    steps are mutable.

    Args:
        session_dir: The InProgress session folder for this run.
        action_owner: Who will act on the new step.
        action_description: What the new step should accomplish.
        position: Optional 0-based index among the steps to insert at. When
            omitted the step is appended to the end. The index is clamped to be
            no earlier than the first ``not started`` step.

    Returns:
        The newly created step dict, or None on failure.
    """
    plan = read_plan(session_dir)
    if plan is None:
        logger.log_error(SCRIPT_NAME, f"No action plan to add a step to in {session_dir}.")
        return None

    steps = plan["steps"]
    max_id = max((int(s.get("step_id", 0)) for s in steps), default=0)
    new_step = make_step(max_id + 1, action_owner, action_description)

    # The earliest index a new (not-started) step may occupy is right after the
    # last step that is not "not started".
    first_mutable = 0
    for idx, step in enumerate(steps):
        if step.get("status") != STATUS_NOT_STARTED:
            first_mutable = idx + 1

    if position is None:
        insert_at = len(steps)
    else:
        insert_at = max(first_mutable, min(int(position), len(steps)))

    steps.insert(insert_at, new_step)

    if _write_plan(session_dir, plan):
        logger.log_info(
            SCRIPT_NAME,
            f"Added step {new_step['step_id']} ({action_owner}) at index {insert_at} "
            f"in {plan_path(session_dir)}.",
        )
        return new_step
    return None


def modify_step(session_dir: str, step_id: int, *,
                action_owner: str | None = None,
                action_description: str | None = None) -> bool:
    """Modify the owner/description of a step, only if it is ``not started``.

    A step that is in progress, completed or failed is immutable here; use
    :func:`update_step_status` to change its status instead.

    Args:
        session_dir: The InProgress session folder for this run.
        step_id: The step to modify.
        action_owner: New owner (optional).
        action_description: New description (optional).

    Returns:
        True on success, False if the step is missing or not ``not started``.
    """
    plan = read_plan(session_dir)
    if plan is None:
        return False

    for step in plan["steps"]:
        if int(step.get("step_id", -1)) == int(step_id):
            if step.get("status") != STATUS_NOT_STARTED:
                logger.log_warning(
                    SCRIPT_NAME,
                    f"Refusing to modify step {step_id}: status is "
                    f"'{step.get('status')}' (only 'not started' steps are editable).",
                )
                return False
            if action_owner is not None:
                step["action_owner"] = action_owner
            if action_description is not None:
                step["action_description"] = action_description
            return _write_plan(session_dir, plan)

    logger.log_error(SCRIPT_NAME, f"Step {step_id} not found in plan {plan_path(session_dir)}.")
    return False


# ---------------------------------------------------------------------------
# action_detail.txt
# ---------------------------------------------------------------------------

def create_detail(session_dir: str, task_prompt: str) -> bool:
    """Create ``action_detail.txt`` seeded with the task prompt.

    Created once, alongside the plan. If it already exists it is left untouched
    so outcomes accumulated by earlier steps are never lost.

    Args:
        session_dir: The InProgress session folder for this run.
        task_prompt: The task's prompt, written at the top of the file.

    Returns:
        True on success (including the "already exists" case), False on error.
    """
    path = detail_path(session_dir)
    if os.path.isfile(path):
        return True

    header = (
        "==================== ACTION DETAIL ====================\n"
        f"Created: {_now()}\n"
        "-------------------- Task Prompt --------------------\n"
        f"{task_prompt}\n"
        "=====================================================\n"
    )
    try:
        os.makedirs(session_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(header)
        logger.log_info(SCRIPT_NAME, f"Action detail created at {path}.")
        return True
    except OSError as e:
        logger.log_error(SCRIPT_NAME, f"Failed to create action detail {path}: {e}")
        return False


def append_detail(session_dir: str, step_id: int, owner: str, outcome: str) -> bool:
    """Append an owner's outcome for a step under a ``Step ID`` heading.

    Each entry is written under a heading like ``--- Step 1 (Swagman) ---`` so a
    later step (e.g. a local-AI step) can read what happened before and where
    produced resources live.

    Args:
        session_dir: The InProgress session folder for this run.
        step_id: The step whose outcome is being recorded.
        owner: The action owner reporting the outcome.
        outcome: A human-readable description of the outcome (may be multi-line).

    Returns:
        True on success, False on error.
    """
    path = detail_path(session_dir)
    entry = (
        f"\n--- Step {step_id} ({owner}) --- {_now()} ---\n"
        f"{outcome}\n"
    )
    try:
        os.makedirs(session_dir, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
        return True
    except OSError as e:
        logger.log_error(SCRIPT_NAME, f"Failed to append to action detail {path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Web-extract progress tracking
#
# A single Swagman step can involve several URLs, each delivered as its own
# web-extract message. To know when the whole step is finished, the step records
# the URLs it expects (``web_urls``) and accumulates the ones that have been
# captured (``completed_urls``). ``record_web_extract`` updates that bookkeeping
# and reports whether the step is now complete.
# ---------------------------------------------------------------------------

def record_web_extract(session_dir: str, step_id: int, web_url: str,
                       result_path: str | None) -> bool:
    """Record one URL's web-extract outcome against a Swagman step.

    Marks ``web_url`` as done for ``step_id`` (regardless of success/failure,
    since either way that URL will not be retried by this run) and returns True
    when every expected URL for the step has now been accounted for.

    Args:
        session_dir: The InProgress session folder for this run.
        step_id: The Swagman step this URL belongs to.
        web_url: The URL that was processed.
        result_path: The captured file path on success, or None on failure.

    Returns:
        True if the step has now processed all of its expected URLs (or the
        expected set is unknown/empty, in which case a single URL completes it).
    """
    plan = read_plan(session_dir)
    if plan is None:
        return True

    for step in plan["steps"]:
        if int(step.get("step_id", -1)) != int(step_id):
            continue

        completed = step.get("completed_urls")
        if not isinstance(completed, list):
            completed = []
        if web_url not in completed:
            completed.append(web_url)
        step["completed_urls"] = completed

        expected = step.get("web_urls")
        expected_set = set(expected) if isinstance(expected, list) and expected else set()

        _write_plan(session_dir, plan)

        if expected_set:
            return expected_set.issubset(set(completed))
        # No expected set recorded: treat this URL as completing the step.
        return True

    # Step not found: nothing to track, consider it complete.
    return True


def read_detail(session_dir: str) -> str | None:
    """Return the full contents of ``action_detail.txt``, or None if absent."""
    path = detail_path(session_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        logger.log_error(SCRIPT_NAME, f"Failed to read action detail {path}: {e}")
        return None
