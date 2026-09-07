"""
Task Runner - Core scheduling logic for Kelpie.

Reads task_config.json, checks each task's last execution against its
frequency_in_minutes, and runs qualified tasks by loading their template,
building a prompt, and calling the Ollama client.

Last execution timestamps are persisted in task_execution_state.json.
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
from Tasks.prompt_builders import build_prompt
from ollama_client import load_config as load_ollama_config, get_client, resolve_model, send_message, send_message_with_web
from mcp_client import send_message_through_mcp

# Supported web access interaction modes (task_config.json "web_access_mode").
WEB_MODE_FIRST = "web_first"
WEB_MODE_THROUGH_MCP = "web_through_mcp"
DEFAULT_WEB_MODE = WEB_MODE_FIRST

SCRIPT_NAME = "task_runner.py"
TASK_CONFIG_PATH = os.path.join(TASKS_DIR, "task_config.json")
EXECUTION_STATE_PATH = os.path.join(TASKS_DIR, "task_execution_state.json")
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "Templates")


def load_task_config() -> list:
    """Load task_config.json and return a list of task definitions.

    Supports both a single task object and an array of tasks.
    """
    try:
        with open(TASK_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.log_error(SCRIPT_NAME, f"Task config not found: {TASK_CONFIG_PATH}")
        return []
    except json.JSONDecodeError as e:
        logger.log_error(SCRIPT_NAME, f"Task config is invalid JSON: {e}")
        return []

    # Normalize: accept a single object or a list
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data

    logger.log_error(SCRIPT_NAME, "task_config.json has unexpected structure.")
    return []


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


def is_task_due(task_name: str, frequency_minutes: int, state: dict) -> bool:
    """Determine whether a task is due for execution based on its frequency.

    Returns True if the task has never run or if enough time has elapsed.
    """
    task_state = state.get("tasks", {}).get(task_name)

    if not task_state or not task_state.get("last_execution"):
        logger.log_info(SCRIPT_NAME, f"Task '{task_name}' has never been executed. It is due.")
        return True

    last_run_str = task_state["last_execution"]
    try:
        last_run = datetime.fromisoformat(last_run_str)
    except ValueError:
        logger.log_warning(SCRIPT_NAME, f"Invalid last_execution timestamp for '{task_name}'. Treating as due.")
        return True

    next_due = last_run + timedelta(minutes=frequency_minutes)
    now = datetime.now()

    if now >= next_due:
        elapsed = (now - last_run).total_seconds() / 60
        logger.log_info(SCRIPT_NAME, f"Task '{task_name}' is due. Last ran {elapsed:.1f} min ago (frequency: {frequency_minutes} min).")
        return True

    remaining = (next_due - now).total_seconds() / 60
    logger.log_info(SCRIPT_NAME, f"Task '{task_name}' is not due yet. Next run in {remaining:.1f} min.")
    return False


def load_template(task_name: str) -> str | None:
    """Load the template file matching the task name from the Templates folder.

    Looks for Templates/<task_name>.txt
    """
    template_path = os.path.join(TEMPLATES_DIR, f"{task_name}.txt")

    if not os.path.exists(template_path):
        logger.log_error(SCRIPT_NAME, f"Template not found for task '{task_name}': {template_path}")
        return None

    try:
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except IOError as e:
        logger.log_error(SCRIPT_NAME, f"Failed to read template for '{task_name}': {e}")
        return None


def execute_task(task_name: str, template_content: str, web_access_mode: str = DEFAULT_WEB_MODE) -> str | None:
    """Execute a task: build prompt, call Ollama, return the response.

    The web_access_mode selects how the model interacts with the web:
      - "web_first" (default): Kelpie fetches web content, converts it to
        markdown, and feeds a single grounded prompt to the model
        (send_message_with_web).
      - "web_through_mcp": Kelpie hands the model an agentic loop via mcphost,
        letting the model decide how many times to interact with web content
        through MCP servers (send_message_through_mcp).
    """
    # Build prompt using the extensible prompt builder
    prompt = build_prompt(task_name, template_content)
    logger.log_info(SCRIPT_NAME, f"Prompt built for task '{task_name}': {prompt[:100]}...")

    # Load Ollama config (shared by both web access modes)
    try:
        ollama_config = load_ollama_config()
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Failed to load Ollama config for task '{task_name}': {e}")
        return None

    mode = (web_access_mode or DEFAULT_WEB_MODE).strip().lower()

    # --- web_through_mcp: agentic, model-driven web interaction via mcphost ---
    if mode == WEB_MODE_THROUGH_MCP:
        logger.log_info(
            SCRIPT_NAME,
            f"Task '{task_name}' using web_access_mode='{WEB_MODE_THROUGH_MCP}' (mcphost agentic loop).",
        )
        response = send_message_through_mcp(prompt, ollama_config)

        if response:
            logger.log_info(SCRIPT_NAME, f"Task '{task_name}' completed. Response length: {len(response)} chars.")
            logger.log_info(SCRIPT_NAME, f"Task '{task_name}' response: {response[:500]}")
        else:
            logger.log_error(SCRIPT_NAME, f"Task '{task_name}' received no response from mcphost.")
        return response

    # --- web_first (default): Kelpie-fetched web content fed to the model ---
    if mode != WEB_MODE_FIRST:
        logger.log_warning(
            SCRIPT_NAME,
            f"Task '{task_name}' has unknown web_access_mode='{web_access_mode}'. Falling back to '{WEB_MODE_FIRST}'.",
        )

    try:
        client = get_client(ollama_config)
        model = resolve_model(None, ollama_config)
        keep_alive = ollama_config.get("keep_alive", "5m")
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Failed to initialize Ollama client for task '{task_name}': {e}")
        return None

    logger.log_info(SCRIPT_NAME, f"Task '{task_name}' using web_access_mode='{WEB_MODE_FIRST}'. Calling Ollama model '{model}'...")
    # web_first mode always requests web content; whether web fetching happens is
    # driven by the task's web_access_mode, not by task_config.json's "enabled"
    # flag (which only controls whether the task runs at all).
    response = send_message_with_web(client, model, prompt, keep_alive, ollama_config, web_access_enabled=True)

    if response:
        logger.log_info(SCRIPT_NAME, f"Task '{task_name}' completed. Response length: {len(response)} chars.")
        logger.log_info(SCRIPT_NAME, f"Task '{task_name}' response: {response[:500]}")
    else:
        logger.log_error(SCRIPT_NAME, f"Task '{task_name}' received no response from Ollama.")

    return response


def run_all_tasks() -> None:
    """Main entry point: evaluate and run all configured tasks."""
    logger.log_info(SCRIPT_NAME, "Task runner started.")

    tasks = load_task_config()
    if not tasks:
        logger.log_warning(SCRIPT_NAME, "No tasks found in task_config.json.")
        return

    state = load_execution_state()

    for task in tasks:
        task_name = task.get("name")
        if not task_name:
            logger.log_warning(SCRIPT_NAME, "Skipping task with no 'name' field.")
            continue

        # Check if task is enabled
        enabled = task.get("enabled", False)
        if not enabled:
            logger.log_info(SCRIPT_NAME, f"Task '{task_name}' is disabled. Skipping.")
            continue

        # Parse frequency
        try:
            frequency_minutes = int(task.get("frequency_in_minutes", 0))
        except (ValueError, TypeError):
            logger.log_error(SCRIPT_NAME, f"Task '{task_name}' has invalid frequency_in_minutes. Skipping.")
            continue

        if frequency_minutes <= 0:
            logger.log_error(SCRIPT_NAME, f"Task '{task_name}' has frequency <= 0. Skipping.")
            continue

        # Check if task is due
        if not is_task_due(task_name, frequency_minutes, state):
            continue

        # Task is qualified to run
        logger.log_info(SCRIPT_NAME, f"Task '{task_name}' has started to run.")

        # Load template
        template_content = load_template(task_name)
        if not template_content:
            continue

        # Determine web access interaction mode (defaults to web_first)
        web_access_mode = task.get("web_access_mode") or DEFAULT_WEB_MODE

        # Execute the task
        response = execute_task(task_name, template_content, web_access_mode)

        # Save execution timestamp regardless of success (to avoid retry storms)
        if "tasks" not in state:
            state["tasks"] = {}

        state["tasks"][task_name] = {
            "last_execution": datetime.now().isoformat(),
            "last_status": "success" if response else "failed"
        }
        save_execution_state(state)

    logger.log_info(SCRIPT_NAME, "Task runner completed.")


if __name__ == "__main__":
    run_all_tasks()
