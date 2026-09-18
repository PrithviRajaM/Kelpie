"""
Prompt Builders - Prompt construction for Kelpie tasks.

The prompt sent to the model is the task's template content exactly as
authored. No preamble, instructions, or wrapping text are added.
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Logger"))

import kelpie_logger as logger

SCRIPT_NAME = "prompt_builders.py"


def build_prompt(task_name: str, template_content: str) -> str:
    """Return the task's template content unchanged as the prompt.

    The template content is passed to the model verbatim; nothing is added
    or altered.

    Args:
        task_name: The name of the task (matches task_config.json 'name' field).
        template_content: Raw content loaded from the template file.

    Returns:
        The template content, unmodified.
    """
    logger.log_info(SCRIPT_NAME, f"Building prompt for task '{task_name}'.")

    return template_content
