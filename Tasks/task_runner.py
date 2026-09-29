"""
Task Runner - Core scheduling logic for Kelpie.

Tasks are no longer defined in a single ``task_config.json`` with prompts in a
shared ``Templates`` folder. Instead, every task lives inside a user's profile
folder under the Numbat data root:

    <NUMBAT_DATA_ROOT>/
    └── <email>/                    # profile folder
        └── Tasks/
            └── <task_name>/        # folder name == task name
                ├── TaskConfig.json # persisted task config
                ├── TaskPrompt.txt  # prompt text fed to the model
                ├── Logs/           # per-task logs, one file per date
                └── InProgress/     # per-task in-progress session artifacts
                    └── <session_counter>/

    The data root comes from the global config (``kelpie_config.json``,
    key ``data_root``); default ``D:\\Data\\Numbat``, overridable per-run via
    the ``NUMBAT_DATA_ROOT`` environment variable.

On every run this module discovers all ``TaskConfig.json`` files nested under
the data root, retaining each task's folder location so that:
  - the prompt is read from that task's own ``TaskPrompt.txt`` at execution time;
  - logs for that task are written into that task's own ``Logs`` folder.

Last-execution timestamps are persisted in ``task_execution_state.json`` (next
to this module), keyed by ``<email>/<task_name>`` so tasks with the same name
in different profiles never collide.
"""

import json
import os
import sys
from datetime import datetime, timedelta

# Resolve paths relative to the project root (one level up from Tasks/)
TASKS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TASKS_DIR)

sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Logger"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Ollama"))

import kelpie_logger as logger
import config as kelpie_config
from Tasks.prompt_builders import build_prompt
from Tasks import action_plan
from ollama_client import load_config as load_ollama_config, get_client, resolve_model, send_message
from Messaging.queue_publisher import (
    publish_web_extract,
    publish_local_ai,
    PublishError,
)

SCRIPT_NAME = "task_runner.py"

# Root of the profile-based task data comes from the centralized global config
# (kelpie_config.json). It is still overridable per-run via the
# NUMBAT_DATA_ROOT environment variable (handled inside config.get_data_root).
DATA_ROOT = kelpie_config.get_data_root()

# Per-profile tasks live under "<email>/Tasks/<task_name>".
PROFILE_TASKS_SUBDIR = "Tasks"

# Fixed filenames inside each task folder (see TASKS.md §1).
TASK_CONFIG_FILENAME = "TaskConfig.json"
TASK_PROMPT_FILENAME = "TaskPrompt.txt"
TASK_LOGS_DIRNAME = "Logs"

# Per-profile config file (holds that profile's incrementing session counter).
# Lives directly inside the profile folder: "<DATA_ROOT>/<email>/Task_Config.json".
PROFILE_CONFIG_FILENAME = "Task_Config.json"

# In-progress session artifacts live under "<task_dir>/InProgress/<session_counter>".
# Each session folder holds the prompt fed to the next bot and a handoff status file.
INPROGRESS_DIRNAME = "InProgress"
SESSION_CONTEXT_FILENAME = "session_context.txt"
STATUS_FILENAME = "status.json"

# Execution state stays local to the Kelpie install (not per-profile).
EXECUTION_STATE_PATH = os.path.join(TASKS_DIR, "task_execution_state.json")


class DiscoveredTask:
    """A task found on disk, with everything needed to run and log it.

    Attributes:
        email: The owning profile (folder name directly under the data root).
        name: The task name (== its folder name and its config 'name').
        config: The parsed TaskConfig.json contents.
        task_dir: Absolute path to the task's folder.
        profile_dir: Absolute path to the owning profile folder
            (``<DATA_ROOT>/<email>``), where the per-profile Task_Config.json
            (session counter) lives.
        prompt_path: Absolute path to the task's TaskPrompt.txt.
        logs_dir: Absolute path to the task's Logs folder.
        state_key: Stable key for execution state ("<email>/<name>").
    """

    def __init__(self, email: str, name: str, config: dict, task_dir: str, profile_dir: str):
        self.email = email
        self.name = name
        self.config = config
        self.task_dir = task_dir
        self.profile_dir = profile_dir
        self.prompt_path = os.path.join(task_dir, TASK_PROMPT_FILENAME)
        self.logs_dir = os.path.join(task_dir, TASK_LOGS_DIRNAME)
        self.state_key = f"{email}/{name}"


def discover_tasks() -> list:
    """Discover every task under the data root, retaining its location.

    Walks ``<DATA_ROOT>/<email>/Tasks/<task_name>`` and treats any folder that
    contains a ``TaskConfig.json`` as a task. The config's parsed contents and
    the folder path are both retained so execution can later read the task's
    own prompt and write to the task's own Logs folder.

    Returns:
        A list of ``DiscoveredTask`` objects (may be empty).
    """
    if not os.path.isdir(DATA_ROOT):
        logger.log_error(SCRIPT_NAME, f"Data root does not exist: {DATA_ROOT}")
        return []

    discovered = []

    for email in sorted(os.listdir(DATA_ROOT)):
        profile_dir = os.path.join(DATA_ROOT, email)
        if not os.path.isdir(profile_dir):
            continue

        tasks_dir = os.path.join(profile_dir, PROFILE_TASKS_SUBDIR)
        if not os.path.isdir(tasks_dir):
            # A profile with no Tasks folder simply has no tasks yet.
            continue

        for task_name in sorted(os.listdir(tasks_dir)):
            task_dir = os.path.join(tasks_dir, task_name)
            if not os.path.isdir(task_dir):
                continue

            config_path = os.path.join(task_dir, TASK_CONFIG_FILENAME)
            if not os.path.isfile(config_path):
                # Only folders holding a TaskConfig.json are tasks.
                continue

            config = _load_task_config(config_path)
            if config is None:
                continue

            discovered.append(DiscoveredTask(email, task_name, config, task_dir, profile_dir))

    logger.log_info(
        SCRIPT_NAME,
        f"Discovered {len(discovered)} task(s) under {DATA_ROOT}.",
    )
    return discovered


def _load_task_config(config_path: str) -> dict | None:
    """Load and parse a single TaskConfig.json. Returns None on failure."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.log_error(SCRIPT_NAME, f"Task config not found: {config_path}")
        return None
    except json.JSONDecodeError as e:
        logger.log_error(SCRIPT_NAME, f"Task config is invalid JSON ({config_path}): {e}")
        return None

    if not isinstance(data, dict):
        logger.log_error(SCRIPT_NAME, f"Task config has unexpected structure: {config_path}")
        return None

    return data


def load_execution_state() -> dict:
    """Load the execution state file that tracks last run times."""
    if not os.path.exists(EXECUTION_STATE_PATH):
        return {"tasks": {}}

    try:
        with open(EXECUTION_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.log_warning(SCRIPT_NAME, f"Could not read execution state, resetting: {e}")
        return {"tasks": {}}


def save_execution_state(state: dict) -> None:
    """Persist the execution state back to disk."""
    try:
        with open(EXECUTION_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
    except IOError as e:
        logger.log_error(SCRIPT_NAME, f"Failed to save execution state: {e}")


def next_session_counter(profile_dir: str) -> int:
    """Derive, persist, and activate a new session counter for a profile.

    The counter is stored per profile in ``<profile_dir>/Task_Config.json``
    under the key ``session_counter``. Behavior:

      - If the file does not exist (or is unreadable/invalid), it is created
        with ``session_counter`` set to 1, and 1 is used for this run.
      - Otherwise the stored value is incremented by one, written back to the
        file immediately, and the incremented value is used for this run.

    The resulting value is set on the logger so that every subsequent log line
    (global and per-task) carries this profile's session counter.

    Args:
        profile_dir: Absolute path to the owning profile folder.

    Returns:
        The session counter value to use for this run.
    """
    config_path = os.path.join(profile_dir, PROFILE_CONFIG_FILENAME)

    existing = None
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                existing = data
        except (OSError, json.JSONDecodeError) as e:
            logger.log_warning(
                SCRIPT_NAME,
                f"Could not read {PROFILE_CONFIG_FILENAME} in {profile_dir}, recreating: {e}",
            )

    if existing is None:
        # File missing or invalid: start this profile's counter at 1.
        config = {"session_counter": 1}
    else:
        config = existing
        try:
            current = int(config.get("session_counter", 0))
        except (ValueError, TypeError):
            current = 0
        config["session_counter"] = current + 1

    new_counter = config["session_counter"]

    # Persist the incremented value immediately, before the task proceeds.
    try:
        os.makedirs(profile_dir, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
    except OSError as e:
        logger.log_error(
            SCRIPT_NAME,
            f"Failed to persist {PROFILE_CONFIG_FILENAME} in {profile_dir}: {e}",
        )

    # From here on, all logging for this task run carries this counter.
    logger.set_session_counter(new_counter)

    return new_counter


def prepare_session_context(task: "DiscoveredTask", session_counter: int, prompt_content: str) -> str | None:
    """Stage the in-progress session artifacts for a task run.

    Creates ``<task_dir>/InProgress/<session_counter>/`` and writes:
      - ``session_context.txt``: the raw prompt content handed to the next bot.
      - ``status.json``: a handoff status file (only created if not already
        present) describing the current and next bot and the next action.

    Args:
        task: The task being run.
        session_counter: The counter for this run (used as the folder name).
        prompt_content: The prompt text to persist as the session context.

    Returns:
        The absolute path to the created session folder, or None on failure.
    """
    session_dir = os.path.join(task.task_dir, INPROGRESS_DIRNAME, str(session_counter))

    try:
        os.makedirs(session_dir, exist_ok=True)
    except OSError as e:
        msg = f"Failed to create session folder for task '{task.name}': {e}"
        logger.log_error(SCRIPT_NAME, msg)
        return None

    # Persist the prompt content as the session context for the next bot.
    context_path = os.path.join(session_dir, SESSION_CONTEXT_FILENAME)
    try:
        with open(context_path, "w", encoding="utf-8") as f:
            f.write(prompt_content)
    except OSError as e:
        msg = f"Failed to write {SESSION_CONTEXT_FILENAME} for task '{task.name}': {e}"
        logger.log_error(SCRIPT_NAME, msg)
        return None

    # Create the handoff status file only if it does not already exist.
    status_path = os.path.join(session_dir, STATUS_FILENAME)
    if not os.path.isfile(status_path):
        status = {
            "Current_bot": "Kelpie - Task Scheduler",
            "Next_bot": "Magpie - AI Agent",
            "Next_Action": "Analyse and start the Task",
        }
        try:
            with open(status_path, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=4)
        except OSError as e:
            msg = f"Failed to write {STATUS_FILENAME} for task '{task.name}': {e}"
            logger.log_error(SCRIPT_NAME, msg)
            return None

    logger.log_info(SCRIPT_NAME, f"Session context staged for task '{task.name}' at {session_dir}.")

    return session_dir


def is_task_due(task: "DiscoveredTask", frequency_minutes: int, state: dict) -> bool:
    """Determine whether a task is due for execution based on its frequency.

    Returns True if the task has never run or if enough time has elapsed.
    Progress is logged both globally and into the task's own Logs folder.
    """
    task_state = state.get("tasks", {}).get(task.state_key)

    if not task_state or not task_state.get("last_execution"):
        msg = f"Task '{task.name}' has never been executed. It is due."
        logger.log_info(SCRIPT_NAME, msg)
        logger.log_task_info(task.logs_dir, SCRIPT_NAME, msg)
        return True

    last_run_str = task_state["last_execution"]
    try:
        last_run = datetime.fromisoformat(last_run_str)
    except ValueError:
        msg = f"Invalid last_execution timestamp for '{task.name}'. Treating as due."
        logger.log_warning(SCRIPT_NAME, msg)
        logger.log_task_warning(task.logs_dir, SCRIPT_NAME, msg)
        return True

    next_due = last_run + timedelta(minutes=frequency_minutes)
    now = datetime.now()

    if now >= next_due:
        elapsed = (now - last_run).total_seconds() / 60
        msg = f"Task '{task.name}' is due. Last ran {elapsed:.1f} min ago (frequency: {frequency_minutes} min)."
        #logger.log_info(SCRIPT_NAME, msg)
        #logger.log_task_info(task.logs_dir, SCRIPT_NAME, msg)
        return True

    remaining = (next_due - now).total_seconds() / 60
    logger.log_info(SCRIPT_NAME, f"Task '{task.name}' is not due yet. Next run in {remaining:.1f} min.")
    return False


def load_prompt(task: "DiscoveredTask") -> str | None:
    """Load the task's prompt from its own TaskPrompt.txt.

    The prompt is stored verbatim; an empty prompt is allowed and returned as
    an empty string. Only a missing/unreadable file yields None.
    """
    if not os.path.isfile(task.prompt_path):
        msg = f"Prompt not found for task '{task.name}': {task.prompt_path}"
        logger.log_error(SCRIPT_NAME, msg)
        return None

    try:
        with open(task.prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except IOError as e:
        msg = f"Failed to read prompt for '{task.name}': {e}"
        logger.log_error(SCRIPT_NAME, msg)
        return None


def run_ollama(prompt: str, logs_dir: str | None = None) -> str | None:
    """Send a prompt to the configured local Ollama model and return the reply.

    This is the low-level "engage the local AI model" helper. It is used by the
    local-AI step of a task's action plan (via the local_AI queue consumer) and
    is safe to call from any process that has imported this module. All progress
    is mirrored into ``logs_dir`` when one is supplied.

    Args:
        prompt: The fully-built prompt to send to the model.
        logs_dir: Optional task Logs folder to mirror progress into.

    Returns:
        The model's reply text, or None if the model could not be reached or
        returned nothing.
    """
    # When called during an active task session, plain log_* calls are already
    # routed to the task's own log; ``logs_dir`` is retained for backward
    # compatibility and to force task logging even if no session is active.
    if logs_dir and logger.active_task_logs_dir() is None:
        logger.begin_task_session(logs_dir)
        _opened_session = True
    else:
        _opened_session = False

    try:
        try:
            ollama_config = load_ollama_config()
        except Exception as e:
            logger.log_error(SCRIPT_NAME, f"Failed to load Ollama config: {e}")
            return None

        try:
            client = get_client(ollama_config)
            model = resolve_model(None, ollama_config)
            keep_alive = ollama_config.get("keep_alive", "5m")
        except Exception as e:
            logger.log_error(SCRIPT_NAME, f"Failed to initialize Ollama client: {e}")
            return None

        logger.log_info(SCRIPT_NAME, f"Calling Ollama model '{model}'...")
        response = send_message(client, model, prompt, keep_alive)

        if response:
            logger.log_info(SCRIPT_NAME, f"Ollama completed. Response length: {len(response)} chars.")
        else:
            logger.log_error(SCRIPT_NAME, "Received no response from Ollama.")

        return response
    finally:
        if _opened_session:
            logger.end_task_session()


def _web_extract_enabled(config: dict) -> bool:
    """Return True when the task config enables web extraction.

    Supports the newer ``web_extract`` flag and falls back to the original
    ``web_access`` flag documented in TASKS.md, so existing configs keep working.
    """
    value = config.get("web_extract")
    if value is None:
        value = config.get("web_access", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _web_urls(config: dict) -> list:
    """Return the list of URLs to extract from the task config.

    Accepts ``web_urls`` (a list, or a single string) and tolerates a legacy
    single ``web_url`` key. Blank/non-string entries are dropped.
    """
    raw = config.get("web_urls")
    if raw is None:
        raw = config.get("web_url")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [u.strip() for u in raw if isinstance(u, str) and u.strip()]


def _build_action_plan_steps(config: dict) -> list:
    """Build the ordered action-plan steps for a task run.

    When web extraction is enabled and URLs are configured, the first step is
    owned by Swagman (extract the web content) and the second by Ollama (engage
    the local AI model on the extracted content). Otherwise the plan is a single
    Ollama step.
    """
    steps = []
    step_id = 1

    if _web_extract_enabled(config):
        urls = _web_urls(config)
        if urls:
            swagman_step = action_plan.make_step(
                step_id,
                action_plan.OWNER_SWAGMAN,
                f"Extract web content for {len(urls)} configured URL(s): "
                + ", ".join(urls),
            )
            # Record the URLs this step expects so its completion can be tracked
            # as each per-URL web-extract job reports back.
            swagman_step["web_urls"] = urls
            swagman_step["completed_urls"] = []
            steps.append(swagman_step)
            step_id += 1

    steps.append(
        action_plan.make_step(
            step_id,
            action_plan.OWNER_OLLAMA,
            "Engage the local AI model to analyse the task prompt and any "
            "extracted resources, then complete the task or request more "
            "resources.",
        )
    )
    return steps


def _start_step(session_dir: str, step: dict, config: dict, logs_dir: str) -> bool:
    """Mark a step in progress and publish it to its action owner's queue.

    The step's status is set to ``in progress`` first (so a crash after the
    publish cannot leave a step looking un-started), then the appropriate
    owner queue receives the job. Returns True if the job was published.
    """
    owner = step.get("action_owner")
    step_id = step.get("step_id")

    action_plan.update_step_status(session_dir, step_id, action_plan.STATUS_IN_PROGRESS)

    # While a task session is active, plain log_* calls already route to the
    # task log; the publisher takes an explicit callback, so point it at the
    # active task log (falling back to logs_dir) to keep publish events on the
    # task's own log too.
    log = lambda m: logger.log_info(SCRIPT_NAME, m)

    try:
        if owner == action_plan.OWNER_SWAGMAN:
            urls = _web_urls(config)
            for url in urls:
                publish_web_extract(
                    {
                        "task_identifier": session_dir,
                        "web_url": url,
                        "destination_folder_name": session_dir,
                        "step_id": step_id,
                    },
                    log=log,
                )
            logger.log_info(
                SCRIPT_NAME,
                f"Published {len(urls)} web-extract job(s) for step {step_id}.",
            )
            return True

        if owner == action_plan.OWNER_OLLAMA:
            publish_local_ai(
                {"task_identifier": session_dir, "step_id": step_id},
                log=log,
            )
            logger.log_info(SCRIPT_NAME, f"Published local-AI job for step {step_id}.")
            return True

        msg = f"Unknown action owner '{owner}' for step {step_id}; cannot start."
        logger.log_error(SCRIPT_NAME, msg)
        action_plan.update_step_status(session_dir, step_id, action_plan.STATUS_FAILED)
        return False
    except PublishError as e:
        msg = f"Failed to publish step {step_id} ({owner}) job: {e}"
        logger.log_error(SCRIPT_NAME, msg)
        action_plan.update_step_status(session_dir, step_id, action_plan.STATUS_FAILED)
        return False


def execute_task(task, config_name: str, state: dict) -> str:
    """Run a single, already-qualified task and persist its execution state.

    This is the core execution step, split out so it can be invoked directly
    (e.g. on demand) without going through the full discover/evaluate loop in
    ``run_all_tasks``. Callers are responsible for ensuring the task is enabled
    and due before calling this.

    Args:
        task: The discovered task to execute.
        config_name: The resolved display name for the task (config 'name' or
            folder name), used for logging and state.
        state: The mutable execution-state dict; updated and saved in place.

    Returns:
        True if execution succeeded, False otherwise. Returns False if the
        prompt could not be loaded or the session context could not be staged.
    """
    # Guard: if this task already has an in-progress session (any folder inside
    # its "InProgress" directory), a prior run has not completed its handoff.
    # Terminate this execution to avoid running the same task concurrently.
    inprogress_dir = os.path.join(task.task_dir, INPROGRESS_DIRNAME)
    if os.path.isdir(inprogress_dir):
        has_session = any(
            os.path.isdir(os.path.join(inprogress_dir, entry))
            for entry in os.listdir(inprogress_dir)
        )
        if has_session:
            msg = (
                f"Task '{config_name}' ({task.email}) execution terminated: "
                f"the task is already in progress (an active session exists under {inprogress_dir})."
            )
            logger.log_warning(SCRIPT_NAME, msg)
            logger.log_task_warning(task.logs_dir, SCRIPT_NAME, msg)
            return "The task execution has been terminated because an earlier instance of the same task is still in progress."

    # Task is qualified to run. Derive a fresh session counter for this
    # task's profile and persist it immediately; every log line below
    # will carry this counter.
    session_counter = next_session_counter(task.profile_dir)

    # Activate the task session so that, from here until the run completes,
    # every log line - including nested calls into Ollama, Swagman and
    # Messaging - is written to this task's own log instead of the global log.
    logger.begin_task_session(task.logs_dir)
    try:
        return _run_qualified_task(task, config_name, state, session_counter)
    finally:
        # Always revert to global logging once the run has been handed off.
        logger.end_task_session()


def _run_qualified_task(task, config_name: str, state: dict, session_counter: int) -> str:
    """Body of ``execute_task`` that runs while the task session is active.

    Split out so ``execute_task`` can guarantee (via try/finally) that the
    task session is ended no matter how this returns. Every ``logger.log_*``
    call here is routed to the task's own log by the active session, so the
    previous explicit ``log_task_*`` mirror calls are no longer needed.
    """
    logger.log_info(SCRIPT_NAME, f"Task '{config_name}' ({task.email}) has started to run (session {session_counter}).")

    # Load the task's own prompt
    prompt_content = load_prompt(task)
    if prompt_content is None:
        return f"Task prompt is not found"

    # Stage the in-progress session artifacts (session_context.txt +
    # status.json) under "<task_dir>/InProgress/<session_counter>"
    # before handing off to execution.
    session_dir = prepare_session_context(task, session_counter, prompt_content)
    if session_dir is None:
        return "Error while creating session directory"

    # ------------------------------------------------------------------
    # Build the action plan and start driving it.
    #
    # The plan is created first and stored in the session folder as
    # action_plan.json. When web_extract is enabled with configured URLs, the
    # first step is Swagman (extract web content) and the second is Ollama
    # (engage the local AI model); otherwise the plan is a single Ollama step.
    #
    # A companion action_detail.txt is created alongside the plan, seeded with
    # the task prompt, so every action owner can append the outcome of its step
    # for later steps (especially the local-AI step) to read.
    # ------------------------------------------------------------------
    logger.log_info(SCRIPT_NAME, f"Task ::: {task.task_dir}")

    steps = _build_action_plan_steps(task.config)
    plan = action_plan.create_plan(session_dir, steps, task_identifier=session_dir)
    if plan is None:
        return "Error while creating action plan"

    prompt = build_prompt(task.name, prompt_content)
    action_plan.create_detail(session_dir, prompt)

    logger.log_info(
        SCRIPT_NAME,
        f"Task '{config_name}' action plan created with {len(steps)} step(s) "
        f"(session {session_counter}).",
    )

    # Kick off the first actionable step by publishing it to its owner's queue.
    first_step = action_plan.next_actionable_step(session_dir)
    if first_step is None:
        response = "Action plan has no actionable step to start"
        logger.log_error(SCRIPT_NAME, f"Task '{config_name}': {response}.")
    elif _start_step(session_dir, first_step, task.config, task.logs_dir):
        response = (
            f"Action plan started at step {first_step['step_id']} "
            f"({first_step['action_owner']})"
        )
        logger.log_info(
            SCRIPT_NAME,
            f"Task '{config_name}' {response} (session {session_counter}).",
        )
    else:
        response = "Error while starting the first action-plan step"
        logger.log_error(SCRIPT_NAME, f"Task '{config_name}': {response}.")

    # Save execution timestamp regardless of success (to avoid retry storms)
    if "tasks" not in state:
        state["tasks"] = {}

    state["tasks"][task.state_key] = {
        "email": task.email,
        "name": config_name,
        "last_execution": datetime.now().isoformat(),
        "last_status": "success" if response else "failed",
    }
    save_execution_state(state)

    return response


def run_all_tasks() -> None:
    """Main entry point: discover, evaluate, and run all tasks on disk."""
    logger.log_info(SCRIPT_NAME, "Task runner started.")

    tasks = discover_tasks()
    if not tasks:
        logger.log_warning(SCRIPT_NAME, f"No tasks found under {DATA_ROOT}.")
        return

    state = load_execution_state()

    for task in tasks:
        # The config's own 'name' is preferred; fall back to the folder name.
        config_name = task.config.get("name") or task.name

        # Check if task is enabled
        enabled = task.config.get("enabled", False)
        if not enabled:
            msg = f"Task '{config_name}' is disabled. Skipping."
            logger.log_info(SCRIPT_NAME, msg)
            logger.log_task_info(task.logs_dir, SCRIPT_NAME, msg)
            continue

        # Parse frequency
        try:
            frequency_minutes = int(task.config.get("frequency_in_minutes", 0))
        except (ValueError, TypeError):
            msg = f"Task '{config_name}' has invalid frequency_in_minutes. Skipping."
            logger.log_error(SCRIPT_NAME, msg)
            logger.log_task_error(task.logs_dir, SCRIPT_NAME, msg)
            continue

        if frequency_minutes <= 0:
            msg = f"Task '{config_name}' has frequency <= 0. Skipping."
            logger.log_error(SCRIPT_NAME, msg)
            logger.log_task_error(task.logs_dir, SCRIPT_NAME, msg)
            continue

        # Check if task is due
        if not is_task_due(task, frequency_minutes, state):
            continue

        execute_task(task, config_name, state)

    logger.log_info(SCRIPT_NAME, "Task runner completed.")


if __name__ == "__main__":
    run_all_tasks()
