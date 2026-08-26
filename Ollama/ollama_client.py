"""
Ollama Client Script - Interact with a local Ollama installation.

Provides simple CLI actions to:
  - list the models installed in Ollama
  - list the models currently loaded (running) in memory
  - start (preload) a model into memory
  - send a chat message to a model and receive/log the response
  - run an interactive chat session with a model

Model selection is driven by ollama_config.json (same directory as this
script). Each action accepts an optional --model argument; when omitted the
"default_model" configured in ollama_config.json is used.

Requires:
  - Ollama installed and running locally (https://ollama.com/download)
  - The official `ollama` Python package (pip install ollama)
"""

import argparse
import json
import os
import sys

# Ensure the script's directory is on the path so kelpie_logger can be imported
# regardless of the working directory the script is run from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import kelpie_logger as logger

try:
    import ollama
except ImportError:
    print(
        "The 'ollama' package is required. Install it with: pip install ollama"
    )
    sys.exit(1)

try:
    import requests
except ImportError:
    print(
        "The 'requests' package is required. Install it with: pip install requests"
    )
    sys.exit(1)

import re


SCRIPT_NAME = "ollama_client.py"
CONFIG_PATH = os.path.join(SCRIPT_DIR, "ollama_config.json")


def load_config() -> dict:
    """Load the Ollama configuration file (ollama_config.json).

    Returns:
        A dict with keys: ollama_host, keep_alive, models, default_model.
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.log_error(SCRIPT_NAME, f"Config file not found: {CONFIG_PATH}")
        raise
    except json.JSONDecodeError as e:
        logger.log_error(SCRIPT_NAME, f"Config file is not valid JSON: {e}")
        raise

    if not config.get("default_model"):
        logger.log_warning(SCRIPT_NAME, "No 'default_model' set in ollama_config.json.")

    return config


def get_client(config: dict) -> "ollama.Client":
    """Create an Ollama client pointed at the configured host."""
    host = config.get("ollama_host", "http://localhost:11434")
    return ollama.Client(host=host)


def resolve_model(requested_model, config: dict) -> str:
    """Resolve the model name to use: explicit request wins, otherwise the
    configured default_model is used.

    Raises:
        ValueError: if no model can be resolved.
    """
    if requested_model:
        return requested_model

    default_model = config.get("default_model")
    if not default_model:
        raise ValueError(
            "No model specified and no 'default_model' configured in ollama_config.json."
        )

    configured_models = config.get("models", [])
    if configured_models and default_model not in configured_models:
        logger.log_warning(
            SCRIPT_NAME,
            f"default_model '{default_model}' is not listed in the configured 'models' array.",
        )

    return default_model


def list_models(client: "ollama.Client") -> None:
    """List all models currently installed in the local Ollama installation."""
    try:
        response = client.list()
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Failed to list installed models: {e}")
        print(f"Error listing models: {e}")
        return

    models = response.models
    logger.log_info(SCRIPT_NAME, f"Listed {len(models)} installed model(s).")

    if not models:
        print("No models are installed in Ollama.")
        return

    print(f"Installed models ({len(models)}):")
    for m in models:
        size_gb = (m.size or 0) / (1024 ** 3)
        param_size = m.details.parameter_size if m.details else "?"
        print(f"  - {m.model}  [{param_size}, {size_gb:.2f} GB]")


def list_running_models(client: "ollama.Client") -> None:
    """List models currently loaded into memory by the Ollama server."""
    try:
        response = client.ps()
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Failed to list running models: {e}")
        print(f"Error listing running models: {e}")
        return

    models = response.models
    logger.log_info(SCRIPT_NAME, f"{len(models)} model(s) currently loaded in memory.")

    if not models:
        print("No models are currently loaded in memory.")
        return

    print(f"Running models ({len(models)}):")
    for m in models:
        vram_gb = (m.size_vram or 0) / (1024 ** 3)
        print(f"  - {m.model}  [VRAM: {vram_gb:.2f} GB, expires at: {m.expires_at}]")


def start_model(client: "ollama.Client", model: str, keep_alive: str) -> bool:
    """Preload (start) a model into memory without generating text.

    Ollama loads a model into memory when it receives a generate/chat
    request. Sending an empty prompt to /api/generate loads the model
    without producing a completion.
    """
    logger.log_info(SCRIPT_NAME, f"Starting model '{model}' (keep_alive={keep_alive}).")
    print(f"Starting model '{model}' ...")

    try:
        response = client.generate(model=model, prompt="", keep_alive=keep_alive)
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Failed to start model '{model}': {e}")
        print(f"Error starting model '{model}': {e}")
        return False

    logger.log_info(
        SCRIPT_NAME,
        f"Model '{model}' started successfully (done_reason={response.done_reason}).",
    )
    print(f"Model '{model}' is now loaded and ready.")
    return True


def send_message(client: "ollama.Client", model: str, message: str, keep_alive: str):
    """Send a single chat message to a model and return/log the response."""
    logger.log_info(SCRIPT_NAME, f"Sending message to '{model}': {message}")

    try:
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": message}],
            keep_alive=keep_alive,
        )
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Chat request to '{model}' failed: {e}")
        print(f"Error: {e}")
        return None

    reply = response.message.content
    logger.log_info(SCRIPT_NAME, f"Received response from '{model}': {reply}")
    return reply


def extract_urls(text: str) -> list[str]:
    """Extract URLs from a text string using regex."""
    url_pattern = r'https?://[^\s,\"\'>)}\]]+'
    return re.findall(url_pattern, text)


def fetch_web_content(url: str, model: str, config: dict) -> str | None:
    """Fetch content from a URL using the Ollama backend API (Page Assist).

    Uses the /api/generate endpoint to ask the model (via Page Assist's web
    access capability) to retrieve and summarize the content of a given URL.

    Args:
        url: The web URL to fetch content from.
        model: The Ollama model name to use for the web-enabled request.
        config: The Ollama configuration dict.

    Returns:
        The text content retrieved from the URL, or None on failure.
    """
    host = config.get("ollama_host", "http://localhost:11434")
    web_model = config.get("web_access", {}).get("model", model)
    endpoint = f"{host}/api/generate"

    fetch_prompt = (
        f"Access the following URL and extract the main content from the webpage. "
        f"Return ONLY the relevant product/content information found on the page, "
        f"without any commentary about your abilities or limitations.\n\n"
        f"URL: {url}"
    )

    logger.log_info(SCRIPT_NAME, f"Fetching web content from '{url}' using model '{web_model}'...")

    try:
        response = requests.post(
            endpoint,
            json={
                "model": web_model,
                "prompt": fetch_prompt,
                "stream": False,
            },
            timeout=120,
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.log_error(SCRIPT_NAME, f"Timeout while fetching web content from '{url}'.")
        return None
    except requests.exceptions.RequestException as e:
        logger.log_error(SCRIPT_NAME, f"Failed to fetch web content from '{url}': {e}")
        return None

    try:
        result = response.json()
        content = result.get("response", "")
        if content:
            logger.log_info(SCRIPT_NAME, f"Successfully fetched web content from '{url}' ({len(content)} chars).")
        else:
            logger.log_warning(SCRIPT_NAME, f"Empty response when fetching web content from '{url}'.")
        return content
    except (ValueError, KeyError) as e:
        logger.log_error(SCRIPT_NAME, f"Failed to parse web content response: {e}")
        return None


def send_message_with_web(client: "ollama.Client", model: str, message: str, keep_alive: str, config: dict):
    """Send a chat message with web access support.

    If the message contains URLs, this function first fetches the web content
    from those URLs using the Ollama /api/generate endpoint (Page Assist backend),
    then incorporates the fetched content into the prompt before sending it to
    the model for a final response.

    Args:
        client: The Ollama client instance.
        model: The model name to use for the final chat response.
        message: The user prompt (may contain URLs).
        keep_alive: Keep-alive duration string.
        config: The Ollama configuration dict.

    Returns:
        The model's response string, or None on failure.
    """
    web_access_enabled = config.get("web_access", {}).get("enabled", False)
    urls = extract_urls(message)

    if not web_access_enabled or not urls:
        # Fall back to standard send_message if web access is disabled or no URLs found
        return send_message(client, model, message, keep_alive)

    logger.log_info(SCRIPT_NAME, f"Web access enabled. Found {len(urls)} URL(s) in prompt: {urls}")

    # Fetch content from each URL
    web_contents = []
    for url in urls:
        content = fetch_web_content(url, model, config)
        if content:
            web_contents.append(f"--- Content from {url} ---\n{content}\n--- End of content ---")

    if not web_contents:
        logger.log_error(SCRIPT_NAME, "Could not fetch any web content. Aborting task to avoid generating assumed data.")
        return None

    # Build an enriched prompt with the fetched web data
    combined_web_content = "\n\n".join(web_contents)
    enriched_message = (
        f"{message}\n\n"
        f"Below is the actual content retrieved from the website(s). "
        f"Use this data to answer the request accurately:\n\n"
        f"{combined_web_content}"
    )

    logger.log_info(SCRIPT_NAME, f"Sending web-enriched message to '{model}' ({len(enriched_message)} chars).")

    try:
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": enriched_message}],
            keep_alive=keep_alive,
        )
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Chat request to '{model}' failed: {e}")
        print(f"Error: {e}")
        return None

    reply = response.message.content
    logger.log_info(SCRIPT_NAME, f"Received web-enriched response from '{model}': {reply}")
    return reply


def interactive_chat(client: "ollama.Client", model: str, keep_alive: str) -> None:
    """Run an interactive, multi-turn chat session with a model in the console."""
    print(f"Interactive chat with '{model}'. Type 'exit' or 'quit' to stop.")
    logger.log_info(SCRIPT_NAME, f"Interactive chat session started with '{model}'.")

    history = []
    try:
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            history.append({"role": "user", "content": user_input})
            logger.log_info(SCRIPT_NAME, f"Sending message to '{model}': {user_input}")

            try:
                response = client.chat(model=model, messages=history, keep_alive=keep_alive)
            except Exception as e:
                logger.log_error(SCRIPT_NAME, f"Chat request to '{model}' failed: {e}")
                print(f"Error: {e}")
                history.pop()
                continue

            reply = response.message.content
            history.append({"role": "assistant", "content": reply})
            logger.log_info(SCRIPT_NAME, f"Received response from '{model}': {reply}")
            print(f"{model}: {reply}")
    finally:
        logger.log_info(SCRIPT_NAME, f"Interactive chat session ended with '{model}'.")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Interact with a local Ollama installation (list, start, chat)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List models installed in Ollama.")
    subparsers.add_parser("running", help="List models currently loaded in memory.")
    subparsers.add_parser("config", help="Show the current Ollama configuration.")

    start_parser = subparsers.add_parser("start", help="Load (start) a model into memory.")
    start_parser.add_argument("--model", help="Model name to start. Defaults to the config's default_model.")

    chat_parser = subparsers.add_parser("chat", help="Send message(s) to a model and receive a response.")
    chat_parser.add_argument("--model", help="Model name to chat with. Defaults to the config's default_model.")
    chat_parser.add_argument(
        "--message",
        help="A single message to send. If omitted, an interactive chat session starts.",
    )

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    logger.log_info(SCRIPT_NAME, f"Command '{args.command}' invoked.")

    config = load_config()
    keep_alive = config.get("keep_alive", "5m")

    if args.command == "config":
        print(json.dumps(config, indent=4))
        return

    client = get_client(config)

    if args.command == "list":
        list_models(client)
        return

    if args.command == "running":
        list_running_models(client)
        return

    if args.command == "start":
        model = resolve_model(args.model, config)
        start_model(client, model, keep_alive)
        return

    if args.command == "chat":
        model = resolve_model(args.model, config)
        if args.message:
            reply = send_message(client, model, args.message, keep_alive)
            if reply is not None:
                print(f"{model}: {reply}")
        else:
            interactive_chat(client, model, keep_alive)
        return


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Unhandled error: {e}")
        print(f"Fatal error: {e}")
        sys.exit(1)
