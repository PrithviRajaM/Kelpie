"""
MCP Client - "web_through_mcp" interaction flow for Kelpie.

Unlike the "web_first" flow (Ollama/ollama_client.py) where Kelpie fetches web
content, converts it to markdown, and feeds a single grounded prompt to the
model, this flow hands the model an agentic loop via `mcphost`.

`mcphost` loads the Ollama model together with a set of MCP servers (defined in
an external config file). The model itself decides how many times to reach out
to the web / tools through those MCP servers before producing a final answer.

The model is loaded with a command equivalent to:

    mcphost -m ollama:qwen3.5:9b --config D:\\AISpace\\mcp-servers.config.json

This module drives `mcphost` non-interactively: it passes the built prompt as a
single-shot prompt, lets mcphost run the tool-calling loop to completion, and
returns the model's final textual response.

Configuration comes from the "web_through_mcp" block in ollama_config.json:

    "web_through_mcp": {
        "mcphost_command": "mcphost",
        "model": "ollama:qwen3.5:9b",
        "mcp_config_path": "D:\\AISpace\\mcp-servers.config.json",
        "timeout_seconds": 600
    }
"""

import os
import subprocess
import sys

# Ensure the script's directory is on the path so kelpie_logger can be imported
# regardless of the working directory the caller runs from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import kelpie_logger as logger

SCRIPT_NAME = "mcp_client.py"

# Sensible defaults; overridden by the "web_through_mcp" config block.
DEFAULT_MCPHOST_COMMAND = "mcphost"
DEFAULT_MODEL = "ollama:qwen3.5:9b"
DEFAULT_MCP_CONFIG_PATH = r"D:\AISpace\mcp-servers.config.json"
DEFAULT_TIMEOUT_SECONDS = 600


def _get_mcp_config(config: dict) -> dict:
    """Extract the web_through_mcp settings from the Ollama config dict."""
    return config.get("web_through_mcp", {}) or {}


def build_mcphost_command(prompt: str, config: dict) -> list[str]:
    """Build the mcphost argument list for a single-shot (non-interactive) run.

    Equivalent to:
        mcphost -m ollama:qwen3.5:9b \
                --config D:\\AISpace\\mcp-servers.config.json \
                -p "<prompt>"

    The `-p/--prompt` flag runs mcphost in non-interactive mode: it executes the
    model's tool-calling loop against the configured MCP servers and exits once
    the model produces its final answer. This lets the model decide how many
    times to interact with the web.

    Args:
        prompt: The fully built prompt to send to the model.
        config: The Ollama configuration dict (contains web_through_mcp block).

    Returns:
        A list of command tokens suitable for subprocess.run.
    """
    mcp_cfg = _get_mcp_config(config)

    mcphost_command = mcp_cfg.get("mcphost_command", DEFAULT_MCPHOST_COMMAND)
    model = mcp_cfg.get("model", DEFAULT_MODEL)
    mcp_config_path = mcp_cfg.get("mcp_config_path", DEFAULT_MCP_CONFIG_PATH)

    return [
        mcphost_command,
        "-m", model,
        "--config", mcp_config_path,
        "-p", prompt,
    ]


def send_message_through_mcp(message: str, config: dict) -> str | None:
    """Run the model through mcphost so it can interact with the web via MCP.

    Spawns `mcphost` as a subprocess in non-interactive single-prompt mode. The
    model, loaded together with the configured MCP servers, autonomously decides
    how many times to call web/tool capabilities before returning a final
    answer. The subprocess stdout is captured and returned as the response.

    Args:
        message: The prompt to send to the model.
        config: The Ollama configuration dict (contains the web_through_mcp block).

    Returns:
        The model's final response text, or None on failure.
    """
    mcp_cfg = _get_mcp_config(config)
    timeout_seconds = mcp_cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    mcp_config_path = mcp_cfg.get("mcp_config_path", DEFAULT_MCP_CONFIG_PATH)

    # Warn early if the MCP servers config is missing; mcphost will fail otherwise.
    if not os.path.exists(mcp_config_path):
        logger.log_warning(
            SCRIPT_NAME,
            f"MCP config path not found: {mcp_config_path}. mcphost may fail to start.",
        )

    command = build_mcphost_command(message, config)

    # Log the command without dumping the (potentially large) prompt body.
    printable_cmd = " ".join(command[:-1]) + ' -p "<prompt omitted>"'
    logger.log_info(
        SCRIPT_NAME,
        f"Starting web_through_mcp run: {printable_cmd} (timeout={timeout_seconds}s).",
    )

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        logger.log_error(
            SCRIPT_NAME,
            f"mcphost executable not found ('{command[0]}'). Ensure mcphost is installed and on PATH.",
        )
        return None
    except subprocess.TimeoutExpired:
        logger.log_error(
            SCRIPT_NAME,
            f"mcphost run timed out after {timeout_seconds}s.",
        )
        return None
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Failed to run mcphost: {e}")
        return None

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:500]
        logger.log_error(
            SCRIPT_NAME,
            f"mcphost exited with code {result.returncode}. stderr: {stderr_snippet}",
        )
        return None

    reply = (result.stdout or "").strip()

    if not reply:
        logger.log_warning(SCRIPT_NAME, "mcphost produced no stdout output.")
        return None

    logger.log_info(
        SCRIPT_NAME,
        f"web_through_mcp run completed. Response length: {len(reply)} chars.",
    )
    return reply
