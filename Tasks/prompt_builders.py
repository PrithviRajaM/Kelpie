"""
Prompt Builders - Extensible prompt construction for Kelpie tasks.

Uses a registry pattern so new task-specific prompt builders can be added
without modifying the core logic. Each builder is a function that receives
the raw template content and returns a ready-to-send AI prompt string.

To add a custom builder for a new task:
    1. Define a function: def build_<task_name>(template_content: str) -> str
    2. Register it: @register_builder("<task_name>")

If no custom builder is registered for a task, the default builder is used
which wraps the template content with a simple instruction preamble.
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Logger"))

import kelpie_logger as logger

SCRIPT_NAME = "prompt_builders.py"

# Registry: maps task_name -> builder function
_BUILDERS: dict[str, callable] = {}


def register_builder(task_name: str):
    """Decorator to register a custom prompt builder for a specific task.

    Usage:
        @register_builder("My_Custom_Task")
        def build_my_custom_task(template_content: str) -> str:
            return f"Custom preamble: {template_content}"
    """
    def decorator(func):
        _BUILDERS[task_name] = func
        logger.log_debug(SCRIPT_NAME, f"Registered custom prompt builder for task '{task_name}'.")
        return func
    return decorator


def default_builder(template_content: str) -> str:
    """Default prompt builder - wraps template content with a clear instruction.

    Used when no task-specific builder is registered.
    """
    prompt = (
        "You are a helpful AI assistant. Complete the following task accurately.\n\n"
        f"Task:\n{template_content}\n\n"
        "Provide a clear, structured response."
    )
    return prompt


def build_prompt(task_name: str, template_content: str) -> str:
    """Build the final prompt for a task.

    Looks up a registered custom builder for the task_name. Falls back to
    the default builder if none is registered.

    Args:
        task_name: The name of the task (matches task_config.json 'name' field).
        template_content: Raw content loaded from the template file.

    Returns:
        The constructed prompt string ready to send to the AI model.
    """
    builder = _BUILDERS.get(task_name, None)

    if builder:
        logger.log_info(SCRIPT_NAME, f"Using custom prompt builder for task '{task_name}'.")
        return builder(template_content)

    logger.log_info(SCRIPT_NAME, f"Using default prompt builder for task '{task_name}'.")
    return default_builder(template_content)


# ---------------------------------------------------------------------------
# Custom builders for specific tasks go below.
# Each one is auto-registered via the @register_builder decorator.
# ---------------------------------------------------------------------------

@register_builder("List_Coffee_Coles")
def build_list_coffee_coles(template_content: str) -> str:
    """Custom builder for the Coles coffee listing task.

    Adds specificity around output format and constraints.
    """
    prompt = (
        "You are a product research assistant.\n\n"
        f"Instruction: {template_content}\n\n"
        "Return the results as a numbered list with product name and price if available.\n"
        "If you cannot access live data, provide your best knowledge of popular coffee "
        "products typically available at Coles Australia."
    )
    return prompt
