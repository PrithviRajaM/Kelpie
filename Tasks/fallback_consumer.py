r"""
Fallback orchestrator - consumer for the ``Kelpie/Fallback`` queue.

Every action owner (Swagman, Ollama, ...) posts a step-status message to this
queue when it finishes its step. This consumer is the orchestrator that turns
those individual completions into forward progress on a task's action plan:

1. It receives a step-status message::

       {
           "task_identifier": "<absolute path to the InProgress session folder>",
           "step_id": 1,
           "status": "completed"        # or "failed"
       }

2. It updates that step's status in ``action_plan.json`` (via
   :mod:`Tasks.action_plan`).
3. If the reported step failed, it stops (the run stalls on the failure).
4. Otherwise it looks up the next ``not started`` step, sets it to
   ``in progress`` and publishes it to the respective owner's queue
   (Swagman/WebExtract for a Swagman step, Ollama/LocalAI for an Ollama step).
5. If there is no next step, the plan is complete and the run is done.

The consumer mirrors the transport conventions of
``Swagman/queue_consumer.py`` (durable queue, manual ack, prefetch of 1,
ack-on-handled, nack-without-requeue on unexpected error).

Message contract for step status
---------------------------------
Aliases are accepted so producers are not tightly coupled to one spelling:
``task``/``session``/``session_dir`` for ``task_identifier`` and ``step``/
``id`` for ``step_id``.

Usage
-----
As a service (always on, auto-reconnecting)::

    py -3 Tasks/fallback_consumer.py

Drain whatever is queued and exit::

    py -3 Tasks/fallback_consumer.py --once
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

import kelpie_logger as logger
import config as kelpie_config
from Tasks import action_plan
from Messaging.queue_publisher import (
    _settings_for,
    FALLBACK_CONFIG_KEY,
    publish_web_extract,
    publish_local_ai,
    PublishError,
)

try:
    import pika
except ImportError as exc:  # pragma: no cover - surfaced at call time
    pika = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

SCRIPT_NAME = "fallback_consumer.py"

# Fixed filename of a task's persisted config (mirrors task_runner.py).
TASK_CONFIG_FILENAME = "TaskConfig.json"

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
    """Connection + routing settings for consuming the fallback queue.

    Defaults come from ``queue.config`` (the ``fallback`` entry) so the queue
    name and connection stay in one place; individual fields can still be
    overridden on the command line.
    """

    host: str = "localhost"
    port: int = 5672
    queue: str = "Kelpie/Fallback"
    username: str = "guest"
    password: str = "guest"
    virtual_host: str = "/"
    durable: bool = True
    prefetch: int = 1
    connection_attempts: int = 3
    retry_delay: float = 2.0

    @classmethod
    def from_config(cls) -> "ConsumerSettings":
        """Build settings from the ``fallback`` entry in ``queue.config``."""
        qs = _settings_for(FALLBACK_CONFIG_KEY)
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
    """Decode a raw AMQP message body into a status mapping."""
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


def _extract_fields(msg: dict) -> tuple[str, int, str]:
    """Pull ``(task_identifier, step_id, status)`` from a status message.

    Accepts a few friendly aliases. Raises ValueError if a required field is
    missing or malformed.
    """
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

    status = msg.get("status")
    if isinstance(status, str):
        status = status.strip().lower()

    missing = []
    if task_identifier is None:
        missing.append("task_identifier")
    if step_raw is None:
        missing.append("step_id")
    if not status:
        missing.append("status")
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")

    try:
        step_id = int(step_raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"step_id must be an integer, got {step_raw!r}") from exc

    # Resolve the compact "{profile}|{task}|{counter}" token to the absolute
    # InProgress session folder so the plan bookkeeping and TaskConfig.json
    # lookup (which walk the on-disk path) work unchanged. Legacy absolute
    # paths pass through untouched.
    task_identifier = kelpie_config.decode_task_identifier(task_identifier)

    return task_identifier, step_id, status


def _load_task_config(session_dir: str) -> dict:
    """Load the owning task's TaskConfig.json from a session folder.

    A session folder is ``<task_dir>/InProgress/<session_counter>``, so the task
    config lives two levels up. Returns an empty dict if it cannot be read (the
    orchestrator can still advance Ollama steps without the config).
    """
    task_dir = os.path.dirname(os.path.dirname(os.path.normpath(session_dir)))
    config_path = os.path.join(task_dir, TASK_CONFIG_FILENAME)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.log_warning(
            SCRIPT_NAME,
            f"Could not read task config at {config_path}; proceeding without it.",
        )
        return {}


def _config_web_urls(config: dict) -> list:
    """Return the configured URLs (``web_urls`` list or legacy ``web_url``)."""
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


def _start_step(session_dir: str, step: dict) -> bool:
    """Mark a step in progress and publish it to its owner's queue.

    Returns True if the job was published, False otherwise (in which case the
    step is marked failed).
    """
    owner = step.get("action_owner")
    step_id = step.get("step_id")

    action_plan.update_step_status(session_dir, step_id, action_plan.STATUS_IN_PROGRESS)

    try:
        if owner == action_plan.OWNER_SWAGMAN:
            config = _load_task_config(session_dir)
            urls = _config_web_urls(config)
            task_identifier = kelpie_config.task_identifier_from_session_dir(
                session_dir
            )
            for url in urls:
                publish_web_extract(
                    {
                        "task_identifier": task_identifier,
                        "web_url": url,
                        "destination_folder_name": task_identifier,
                        "step_id": step_id,
                    }
                )
            logger.log_info(
                SCRIPT_NAME,
                f"Advanced to step {step_id} (Swagman): published "
                f"{len(urls)} web-extract job(s).",
            )
            return True

        if owner == action_plan.OWNER_OLLAMA:
            publish_local_ai(
                {
                    "task_identifier": kelpie_config.task_identifier_from_session_dir(
                        session_dir
                    ),
                    "step_id": step_id,
                }
            )
            logger.log_info(
                SCRIPT_NAME,
                f"Advanced to step {step_id} (Ollama): published local-AI job.",
            )
            return True

        logger.log_error(
            SCRIPT_NAME,
            f"Unknown action owner '{owner}' for step {step_id}; marking failed.",
        )
        action_plan.update_step_status(session_dir, step_id, action_plan.STATUS_FAILED)
        return False
    except PublishError as exc:
        logger.log_error(
            SCRIPT_NAME,
            f"Failed to publish step {step_id} ({owner}) job: {exc}; marking failed.",
        )
        action_plan.update_step_status(session_dir, step_id, action_plan.STATUS_FAILED)
        return False


def _advance_plan(session_dir: str, step_id: int, status: str) -> None:
    """Update the reported step, then start the next one if the run continues."""
    # Normalize a couple of common status spellings to the canonical set.
    normalized = {
        "complete": action_plan.STATUS_COMPLETED,
        "completed": action_plan.STATUS_COMPLETED,
        "done": action_plan.STATUS_COMPLETED,
        "success": action_plan.STATUS_COMPLETED,
        "succeeded": action_plan.STATUS_COMPLETED,
        "fail": action_plan.STATUS_FAILED,
        "failed": action_plan.STATUS_FAILED,
        "error": action_plan.STATUS_FAILED,
    }.get(status, status)

    action_plan.update_step_status(session_dir, step_id, normalized)

    if normalized == action_plan.STATUS_FAILED:
        logger.log_warning(
            SCRIPT_NAME,
            f"Step {step_id} reported failed for '{session_dir}'. "
            f"Halting plan advancement.",
        )
        return

    if action_plan.is_plan_complete(session_dir):
        logger.log_info(
            SCRIPT_NAME,
            f"Action plan for '{session_dir}' is complete. Task run done.",
        )
        return

    next_step = action_plan.next_actionable_step(session_dir)
    if next_step is None:
        logger.log_info(
            SCRIPT_NAME,
            f"No further actionable step for '{session_dir}' (nothing to advance).",
        )
        return

    _start_step(session_dir, next_step)


def _handle_message(channel, method, properties, body) -> None:
    """pika callback: process one step-status delivery and ack/nack it."""
    delivery_tag = method.delivery_tag
    try:
        msg = _decode_payload(body)
        task_identifier, step_id, status = _extract_fields(msg)
    except ValueError as exc:
        logger.log_error(
            SCRIPT_NAME,
            f"Discarding malformed fallback message (delivery_tag={delivery_tag}): {exc}",
        )
        channel.basic_ack(delivery_tag=delivery_tag)
        return

    # The task may have been moved/removed from InProgress since this message
    # was published (e.g. cancelled or already completed). If its session folder
    # is gone, there is nothing to advance: record it against the task, ack the
    # message so it leaves the queue, and stop here.
    if not os.path.isdir(task_identifier):
        logs_dir = _logs_dir_for(task_identifier)
        logger.log_task_warning(
            logs_dir,
            SCRIPT_NAME,
            f"Task session folder no longer exists: '{task_identifier}'. "
            f"Discarding fallback status '{status}' for step {step_id} "
            f"without advancing the plan.",
        )
        channel.basic_ack(delivery_tag=delivery_tag)
        return

    logger.log_info(
        SCRIPT_NAME,
        f"Fallback received status '{status}' for step {step_id} "
        f"of '{task_identifier}'.",
    )

    try:
        _advance_plan(task_identifier, step_id, status)
    except Exception as exc:  # noqa: BLE001 - keep the orchestrator alive
        logger.log_error(
            SCRIPT_NAME,
            f"Error advancing plan for '{task_identifier}' step {step_id}: {exc}. "
            f"Nacking (no requeue).",
        )
        channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
        return

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
    """Start consuming fallback messages (blocking)."""
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
    """Run the orchestrator as an always-on, auto-reconnecting service."""
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
    """Process every queued fallback message, then return (no blocking wait)."""
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
        description="Orchestrate task action plans from the Kelpie/Fallback queue."
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
    # Start from the config-driven defaults, then apply CLI overrides.
    base = ConsumerSettings.from_config()
    settings = ConsumerSettings(
        host=args.host if args.host != base.host else base.host,
        port=args.port,
        queue=args.queue if args.queue != "Kelpie/Fallback" else base.queue,
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
