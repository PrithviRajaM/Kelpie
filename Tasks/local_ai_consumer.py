r"""
Local AI consumer - consumer for the ``Ollama/LocalAI`` queue.

The local AI model (Ollama) cannot reach the internet by itself. When a task's
action plan reaches an Ollama-owned step, a message is published to this queue::

    {
        "task_identifier": "<absolute path to the InProgress session folder>",
        "step_id": 2
    }

This consumer resumes the local AI model on that step:

1. It reads the run's context so the model understands what has happened so far:
   the original session context (``session_context.txt``) and the running record
   of outcomes (``action_detail.txt``), which lists any web content that Swagman
   extracted (URL -> downloaded filename) and where those files live.
2. It engages the local AI model (via :func:`Tasks.task_runner.run_ollama`).
3. It records the model's outcome under the step's heading in
   ``action_detail.txt`` so a future step can pick up where this one left off.
4. It posts the step's status to the ``fallback`` queue so the orchestrator can
   advance the plan. If the model needs more web content or other resources it
   can add a step to the plan (owned by the relevant owner) before reporting
   ``completed``; the orchestrator then dispatches that new step next.

The consumer mirrors the transport conventions of the other Kelpie consumers
(durable queue, manual ack, prefetch of 1, ack-on-handled, nack-without-requeue
on unexpected error).

Usage
-----
As a service (always on, auto-reconnecting)::

    py -3 Tasks/local_ai_consumer.py

Drain whatever is queued and exit::

    py -3 Tasks/local_ai_consumer.py --once
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

# Resolve paths relative to the Kelpie project root (one level up from Tasks/).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if os.path.join(PROJECT_ROOT, "Logger") not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "Logger"))
if os.path.join(PROJECT_ROOT, "Ollama") not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "Ollama"))

import kelpie_logger as logger
import config as kelpie_config
from Tasks import action_plan
from Tasks.task_runner import run_ollama, SESSION_CONTEXT_FILENAME
from Messaging.queue_publisher import (
    _settings_for,
    LOCAL_AI_CONFIG_KEY,
    publish_fallback,
    PublishError,
)

try:
    import pika
except ImportError as exc:  # pragma: no cover - surfaced at call time
    pika = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

SCRIPT_NAME = "local_ai_consumer.py"

# Name of the per-task Logs folder (mirrors task_runner.TASK_LOGS_DIRNAME). A
# task's log lives at "<task_dir>/Logs", and a session folder is
# "<task_dir>/InProgress/<session_counter>", so the Logs dir is two levels up.
TASK_LOGS_DIRNAME = "Logs"


def _logs_dir_for(session_dir: str) -> str:
    """Derive a task's ``Logs`` folder from its InProgress session-folder path.

    ``session_dir`` is the decoded
    ``<task_dir>/InProgress/<session_counter>`` path, so the task folder is two
    levels up and its ``Logs`` folder sits beside ``InProgress``.
    """
    normalized = os.path.normpath(session_dir)
    task_dir = os.path.dirname(os.path.dirname(normalized))
    return os.path.join(task_dir, TASK_LOGS_DIRNAME)


class ConsumeError(Exception):
    """Raised when the consumer cannot start or connect to the broker."""


@dataclass
class ConsumerSettings:
    """Connection + routing settings for consuming the local_AI queue.

    Defaults come from ``queue.config`` (the ``local_AI`` entry).
    """

    host: str = "localhost"
    port: int = 5672
    queue: str = "Ollama/LocalAI"
    username: str = "guest"
    password: str = "guest"
    virtual_host: str = "/"
    durable: bool = True
    prefetch: int = 1
    connection_attempts: int = 3
    retry_delay: float = 2.0

    @classmethod
    def from_config(cls) -> "ConsumerSettings":
        """Build settings from the ``local_AI`` entry in ``queue.config``."""
        qs = _settings_for(LOCAL_AI_CONFIG_KEY)
        return cls(
            host=qs.host,
            port=qs.port,
            queue=qs.queue,
            username=qs.username,
            password=qs.password,
            virtual_host=qs.virtual_host,
            durable=qs.durable,
            connection_attempts=qs.connection_attempts,
            retry_delay=qs.retry_delay,
        )


def _decode_payload(body: bytes) -> dict:
    """Decode a raw AMQP message body into a job mapping."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"message body is not valid UTF-8: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"message body is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"message body must be a JSON object, got {type(data).__name__}"
        )
    return data


def _extract_fields(msg: dict) -> tuple[str, int]:
    """Pull ``(task_identifier, step_id)`` from a local-AI job message."""
    task_identifier = None
    for key in ("task_identifier", "task", "session", "session_dir"):
        value = msg.get(key)
        if isinstance(value, str) and value.strip():
            task_identifier = value.strip()
            break

    step_raw = None
    for key in ("step_id", "step", "id"):
        if key in msg and msg[key] is not None:
            step_raw = msg[key]
            break

    missing = []
    if task_identifier is None:
        missing.append("task_identifier")
    if step_raw is None:
        missing.append("step_id")
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")

    try:
        step_id = int(step_raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"step_id must be an integer, got {step_raw!r}") from exc

    # Resolve the compact "{profile}|{task}|{counter}" token to the absolute
    # InProgress session folder so reading session_context.txt / action_detail
    # works unchanged. Legacy absolute paths pass through untouched.
    task_identifier = kelpie_config.decode_task_identifier(task_identifier)

    return task_identifier, step_id


def _read_session_context(session_dir: str) -> str:
    """Return the run's ``session_context.txt`` content, or empty string."""
    path = os.path.join(session_dir, SESSION_CONTEXT_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _build_resume_prompt(session_dir: str) -> str:
    """Assemble the prompt that resumes the model, explaining prior progress.

    Combines the original session context with the accumulated
    ``action_detail.txt`` so the model understands what has been done so far
    (e.g. which web pages were extracted and where their files live) before it
    continues.
    """
    context = _read_session_context(session_dir).strip()
    detail = (action_plan.read_detail(session_dir) or "").strip()

    parts = []
    if context:
        parts.append("=== Original task context ===\n" + context)
    if detail:
        parts.append(
            "=== Progress so far (outcomes recorded by each step) ===\n" + detail
        )
    parts.append(
        "=== Your task ===\n"
        "Using the context and the progress above, continue the task. The "
        "extracted web content (if any) is stored in the 'web_extract' folder "
        "inside this task's session folder; refer to the filenames listed under "
        "the web-extract step above. You cannot access the internet directly."
    )
    return "\n\n".join(parts)


def _process_local_ai_step(session_dir: str, step_id: int) -> str:
    """Run the local AI model for a step and record its outcome.

    Returns the canonical status to report to the fallback queue
    (``completed`` or ``failed``).
    """
    prompt = _build_resume_prompt(session_dir)

    logger.log_info(
        SCRIPT_NAME,
        f"Engaging local AI model for step {step_id} of '{session_dir}'.",
    )

    reply = run_ollama(prompt)

    if reply:
        outcome = (
            "The local AI model was engaged and produced the following response:\n"
            f"{reply}"
        )
        action_plan.append_detail(session_dir, step_id, action_plan.OWNER_OLLAMA, outcome)
        logger.log_info(
            SCRIPT_NAME,
            f"Local AI step {step_id} completed ({len(reply)} chars) for '{session_dir}'.",
        )
        return action_plan.STATUS_COMPLETED

    outcome = "The local AI model returned no response; the step failed."
    action_plan.append_detail(session_dir, step_id, action_plan.OWNER_OLLAMA, outcome)
    logger.log_error(
        SCRIPT_NAME,
        f"Local AI step {step_id} failed (no response) for '{session_dir}'.",
    )
    return action_plan.STATUS_FAILED


def _handle_message(channel, method, properties, body) -> None:
    """pika callback: process one local-AI delivery and ack/nack it."""
    delivery_tag = method.delivery_tag
    try:
        msg = _decode_payload(body)
        task_identifier, step_id = _extract_fields(msg)
    except ValueError as exc:
        logger.log_error(
            SCRIPT_NAME,
            f"Discarding malformed local-AI message (delivery_tag={delivery_tag}): {exc}",
        )
        channel.basic_ack(delivery_tag=delivery_tag)
        return

    # The task may have been moved/removed from InProgress since this message
    # was published (e.g. cancelled or already completed). If its session folder
    # is gone, there is nothing to process: record it against the task, ack the
    # message so it leaves the queue, and stop here.
    if not os.path.isdir(task_identifier):
        logs_dir = _logs_dir_for(task_identifier)
        logger.log_task_warning(
            logs_dir,
            SCRIPT_NAME,
            f"Task session folder no longer exists: '{task_identifier}'. "
            f"Discarding local-AI job for step {step_id} without processing.",
        )
        channel.basic_ack(delivery_tag=delivery_tag)
        return

    logger.log_info(
        SCRIPT_NAME,
        f"Processing local-AI job for step {step_id} of '{task_identifier}'.",
    )

    try:
        status = _process_local_ai_step(task_identifier, step_id)
    except Exception as exc:  # noqa: BLE001 - keep the consumer alive
        logger.log_error(
            SCRIPT_NAME,
            f"Local-AI job for '{task_identifier}' step {step_id} raised: {exc}. "
            f"Nacking (no requeue).",
        )
        channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
        return

    # Report the outcome to the fallback orchestrator so the plan can advance.
    try:
        publish_fallback(
            {
                "task_identifier": kelpie_config.task_identifier_from_session_dir(
                    task_identifier
                ),
                "step_id": step_id,
                "status": status,
            }
        )
    except PublishError as exc:
        logger.log_error(
            SCRIPT_NAME,
            f"Failed to post fallback status for '{task_identifier}' step {step_id}: {exc}.",
        )
        # The step outcome is recorded; drop the message rather than loop.

    channel.basic_ack(delivery_tag=delivery_tag)


def _connection_parameters(settings: ConsumerSettings):
    credentials = pika.PlainCredentials(settings.username, settings.password)
    return pika.ConnectionParameters(
        host=settings.host,
        port=settings.port,
        virtual_host=settings.virtual_host,
        credentials=credentials,
        connection_attempts=settings.connection_attempts,
        retry_delay=settings.retry_delay,
    )


def consume(settings: Optional[ConsumerSettings] = None) -> None:
    """Start consuming local-AI messages (blocking)."""
    if pika is None:
        raise ConsumeError(
            f"The 'pika' package is required to consume messages: {_IMPORT_ERROR}"
        )

    settings = settings or ConsumerSettings.from_config()

    connection = None
    try:
        connection = pika.BlockingConnection(_connection_parameters(settings))
        channel = connection.channel()
        channel.queue_declare(queue=settings.queue, durable=settings.durable)
        channel.basic_qos(prefetch_count=settings.prefetch)
        channel.basic_consume(
            queue=settings.queue,
            on_message_callback=_handle_message,
            auto_ack=False,
        )
        logger.log_info(
            SCRIPT_NAME,
            f"Consuming from queue '{settings.queue}' on "
            f"{settings.host}:{settings.port} (vhost '{settings.virtual_host}'). "
            f"Press Ctrl+C to stop.",
        )
        try:
            channel.start_consuming()
        except KeyboardInterrupt:
            logger.log_info(SCRIPT_NAME, "Interrupted; stopping consumer.")
            channel.stop_consuming()
    except ConsumeError:
        raise
    except Exception as exc:
        raise ConsumeError(
            f"Failed to consume from queue '{settings.queue}' on "
            f"{settings.host}:{settings.port}: {exc}"
        ) from exc
    finally:
        if connection is not None and connection.is_open:
            try:
                connection.close()
            except Exception:
                pass


def consume_forever(
    settings: Optional[ConsumerSettings] = None,
    *,
    reconnect_delay: float = 5.0,
) -> None:
    """Run the local-AI consumer as an always-on, auto-reconnecting service."""
    settings = settings or ConsumerSettings.from_config()
    while True:
        try:
            consume(settings)
            logger.log_info(SCRIPT_NAME, "Consumer stopped cleanly; exiting service loop.")
            return
        except KeyboardInterrupt:
            logger.log_info(SCRIPT_NAME, "Interrupted; shutting down consumer service.")
            return
        except ConsumeError as exc:
            logger.log_error(
                SCRIPT_NAME,
                f"Consumer connection lost/failed: {exc}. "
                f"Reconnecting in {reconnect_delay:.0f}s.",
            )
            try:
                time.sleep(reconnect_delay)
            except KeyboardInterrupt:
                logger.log_info(SCRIPT_NAME, "Interrupted during reconnect wait; exiting.")
                return


def drain(settings: Optional[ConsumerSettings] = None) -> int:
    """Process every queued local-AI message, then return (no blocking wait)."""
    if pika is None:
        raise ConsumeError(
            f"The 'pika' package is required to consume messages: {_IMPORT_ERROR}"
        )

    settings = settings or ConsumerSettings.from_config()

    handled = 0
    connection = None
    try:
        connection = pika.BlockingConnection(_connection_parameters(settings))
        channel = connection.channel()
        channel.queue_declare(queue=settings.queue, durable=settings.durable)
        while True:
            method, properties, body = channel.basic_get(
                queue=settings.queue, auto_ack=False
            )
            if method is None:
                break
            _handle_message(channel, method, properties, body)
            handled += 1
        if handled:
            logger.log_info(
                SCRIPT_NAME,
                f"Drained {handled} message(s) from queue '{settings.queue}'.",
            )
    except ConsumeError:
        raise
    except Exception as exc:
        raise ConsumeError(
            f"Failed to drain queue '{settings.queue}' on "
            f"{settings.host}:{settings.port}: {exc}"
        ) from exc
    finally:
        if connection is not None and connection.is_open:
            try:
                connection.close()
            except Exception:
                pass
    return handled


def _build_arg_parser() -> argparse.ArgumentParser:
    defaults = ConsumerSettings()
    parser = argparse.ArgumentParser(
        description="Resume the local AI model on Ollama/LocalAI queue jobs."
    )
    parser.add_argument("--host", default=defaults.host, help="RabbitMQ host.")
    parser.add_argument("--port", type=int, default=defaults.port, help="AMQP port.")
    parser.add_argument("--queue", default=defaults.queue, help="Queue name.")
    parser.add_argument("--username", default=defaults.username, help="AMQP username.")
    parser.add_argument("--password", default=defaults.password, help="AMQP password.")
    parser.add_argument("--virtual-host", default=defaults.virtual_host, help="AMQP virtual host.")
    parser.add_argument("--prefetch", type=int, default=defaults.prefetch, help="QoS prefetch.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain all currently-queued messages and exit.",
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    base = ConsumerSettings.from_config()
    settings = ConsumerSettings(
        host=args.host if args.host != base.host else base.host,
        port=args.port,
        queue=args.queue if args.queue != "Ollama/LocalAI" else base.queue,
        username=args.username,
        password=args.password,
        virtual_host=args.virtual_host,
        durable=base.durable,
        prefetch=args.prefetch,
        connection_attempts=base.connection_attempts,
        retry_delay=base.retry_delay,
    )
    try:
        if args.once:
            drain(settings)
        else:
            consume_forever(settings)
    except ConsumeError as exc:
        logger.log_error(SCRIPT_NAME, str(exc))
        print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
