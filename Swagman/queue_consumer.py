r"""
RabbitMQ consumer for the ``Swagman/WebExtract`` queue.

This module listens on the shared RabbitMQ service for web-extract jobs and
hands each one to :func:`Swagman.swagman.extract_web_page`. It is the consuming
counterpart to ``Messaging/queue_publisher.py`` (which some other bot uses to
enqueue jobs) and follows the same conventions:

* the connection settings live in a small ``ConsumerSettings`` dataclass that
  can be built from a plain mapping (``from_mapping``);
* the queue is declared durable so it survives a broker restart;
* only the standard library and ``pika`` are required for the transport layer.

Queue details (from ``Reference/RabbitMQ... Swagman_WebExtract ...html``)
------------------------------------------------------------------------
* virtual host: ``/``
* queue name:   ``Swagman/WebExtract``

Message contract
----------------
Each message body is expected to be JSON describing one web-extract job::

    {
        "task_identifier": "Extract_Coles_Menu",
        "web_url": "https://www.coles.com.au/browse",
        "destination_folder_name": "coles_item_categories"
    }

Those three keys map directly onto the arguments of
``Swagman.swagman.extract_web_page``. Aliases are accepted for convenience
(``task``/``name`` for ``task_identifier``, ``url`` for ``web_url``,
``destination``/``folder`` for ``destination_folder_name``).

Acknowledgement policy
----------------------
* Messages are consumed with manual acknowledgement and a prefetch of 1 so a
  single job is processed at a time.
* A job that completes (or fails cleanly, e.g. bad payload) is **acked** so it
  is removed from the queue - a malformed message is never redelivered in a
  loop.
* A job that raises an unexpected/transport error is **nacked** with
  ``requeue=False`` (so it is dropped or dead-lettered rather than poisoning
  the queue). Adjust ``requeue`` below if a dead-letter exchange is configured.

Usage
-----
As a library::

    from Swagman.queue_consumer import ConsumerSettings, consume

    consume(ConsumerSettings(queue="Swagman/WebExtract"))

From the command line::

    py -3 Swagman/queue_consumer.py --host localhost --queue "Swagman/WebExtract"
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

# Resolve paths relative to the Kelpie project root (two levels up from
# Kelpie/Swagman/) so the Kelpie logger and the local swagman module import
# cleanly regardless of the working directory the consumer is launched from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
_LOGGER_DIR = os.path.join(PROJECT_ROOT, "Logger")
if _LOGGER_DIR not in sys.path:
    sys.path.insert(0, _LOGGER_DIR)

import kelpie_logger as logger
from swagman import extract_web_page

try:
    import pika
except ImportError as exc:  # pragma: no cover - surfaced at call time
    pika = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

SCRIPT_NAME = "queue_consumer.py"


class ConsumeError(Exception):
    """Raised when the consumer cannot start or connect to the broker."""


@dataclass
class ConsumerSettings:
    """Connection + routing settings for consuming ``Swagman/WebExtract``.

    Attributes:
        host: RabbitMQ host (the service runs locally by default).
        port: AMQP port (RabbitMQ default is 5672).
        queue: Name of the queue to consume from.
        username: AMQP username.
        password: AMQP password.
        virtual_host: AMQP virtual host.
        durable: Declare the queue durable so it survives a broker restart.
            Must match how the queue was originally declared.
        prefetch: Max unacknowledged messages delivered at once (QoS). ``1``
            processes one job at a time.
        connection_attempts: How many times pika should try to connect.
        retry_delay: Seconds pika waits between connection attempts.
    """

    host: str = "localhost"
    port: int = 5672
    queue: str = "Swagman/WebExtract"
    username: str = "guest"
    password: str = "guest"
    virtual_host: str = "/"
    durable: bool = True
    prefetch: int = 1
    connection_attempts: int = 3
    retry_delay: float = 2.0

    @classmethod
    def from_mapping(cls, data: Optional[dict]) -> "ConsumerSettings":
        """Build settings from a plain dict, ignoring unknown keys.

        Missing keys fall back to the dataclass defaults, so a partial config
        (for example only ``host`` and ``queue``) is valid.
        """
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


def _decode_payload(body: bytes) -> dict:
    """Decode a raw AMQP message body into a job mapping.

    Args:
        body: The raw message bytes.

    Returns:
        The decoded JSON object as a dict.

    Raises:
        ValueError: If the body is not valid UTF-8 JSON describing an object.
    """
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


def _extract_job_fields(job: dict) -> tuple[str, str, str]:
    """Pull the three ``extract_web_page`` arguments out of a job mapping.

    Accepts a few friendly aliases so publishers are not tightly coupled to one
    exact key spelling.

    Args:
        job: The decoded message object.

    Returns:
        ``(task_identifier, web_url, destination_folder_name)``.

    Raises:
        ValueError: If any required field is missing or blank.
    """

    def pick(*keys: str) -> Optional[str]:
        for key in keys:
            value = job.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    task_identifier = pick("task_identifier", "task", "name")
    web_url = pick("web_url", "url")
    destination_folder_name = pick(
        "destination_folder_name", "destination", "folder"
    )

    missing = [
        label
        for label, value in (
            ("task_identifier", task_identifier),
            ("web_url", web_url),
            ("destination_folder_name", destination_folder_name),
        )
        if value is None
    ]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")

    return task_identifier, web_url, destination_folder_name


def _handle_message(channel, method, properties, body) -> None:
    """pika callback: process one delivery and ack/nack it.

    Bad payloads are acked (dropped) so they are not redelivered forever;
    unexpected processing failures are nacked without requeue.
    """
    delivery_tag = method.delivery_tag

    try:
        job = _decode_payload(body)
        task_identifier, web_url, destination_folder_name = _extract_job_fields(job)
    except ValueError as exc:
        logger.log_error(
            SCRIPT_NAME,
            f"Discarding malformed message (delivery_tag={delivery_tag}): {exc}",
        )
        # Ack so the poison message leaves the queue instead of looping.
        channel.basic_ack(delivery_tag=delivery_tag)
        return

    logger.log_info(
        SCRIPT_NAME,
        f"Processing web-extract job '{task_identifier}' "
        f"(url='{web_url}', destination='{destination_folder_name}').",
    )

    try:
        result_path = extract_web_page(
            task_identifier=task_identifier,
            web_url=web_url,
            destination_folder_name=destination_folder_name,
        )
    except Exception as exc:  # noqa: BLE001 - keep the consumer alive
        logger.log_error(
            SCRIPT_NAME,
            f"Job '{task_identifier}' raised an error: {exc}. "
            f"Nacking (no requeue).",
        )
        channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
        return

    if result_path:
        logger.log_info(
            SCRIPT_NAME,
            f"Job '{task_identifier}' completed: {result_path}",
        )
    else:
        # extract_web_page returned None: the capture failed but was handled.
        logger.log_warning(
            SCRIPT_NAME,
            f"Job '{task_identifier}' did not produce a file (see log above).",
        )

    # The job was handled (success or clean failure); remove it from the queue.
    channel.basic_ack(delivery_tag=delivery_tag)


def consume(settings: Optional[ConsumerSettings] = None) -> None:
    """Start consuming messages from the configured queue (blocking).

    Opens a blocking connection, declares the queue durable, sets QoS prefetch,
    and dispatches each message to :func:`_handle_message`. Runs until
    interrupted (Ctrl+C) or a connection error occurs.

    Args:
        settings: Connection and routing settings. Defaults are used if omitted.

    Raises:
        ConsumeError: If pika is unavailable or the broker cannot be reached.
    """
    if pika is None:
        raise ConsumeError(
            f"The 'pika' package is required to consume messages: {_IMPORT_ERROR}"
        )

    settings = settings or ConsumerSettings()

    credentials = pika.PlainCredentials(settings.username, settings.password)
    parameters = pika.ConnectionParameters(
        host=settings.host,
        port=settings.port,
        virtual_host=settings.virtual_host,
        credentials=credentials,
        connection_attempts=settings.connection_attempts,
        retry_delay=settings.retry_delay,
    )

    connection = None
    try:
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()

        # Ensure the queue exists and survives broker restarts. This must match
        # how the queue was originally declared, or the broker rejects it.
        channel.queue_declare(queue=settings.queue, durable=settings.durable)

        # One unacked message at a time so a slow web extract does not cause a
        # backlog of in-flight deliveries.
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
    except Exception as exc:  # pika raises a variety of connection errors
        raise ConsumeError(
            f"Failed to consume from queue '{settings.queue}' on "
            f"{settings.host}:{settings.port}: {exc}"
        ) from exc
    finally:
        if connection is not None and connection.is_open:
            try:
                connection.close()
            except Exception:
                # A close failure must not mask the original outcome.
                pass


def consume_forever(
    settings: Optional[ConsumerSettings] = None,
    *,
    reconnect_delay: float = 5.0,
) -> None:
    """Run the consumer as an always-on service (blocking, auto-reconnecting).

    This is the entry point for "run it once, leave it running, and every
    message is processed the instant the broker pushes it." A single
    :func:`consume` call blocks until the broker connection drops (restart,
    network blip, etc.); this wrapper simply reconnects and resumes, so the
    subscription is effectively permanent for the life of the process.

    The loop exits cleanly only on Ctrl+C (``KeyboardInterrupt``). Every other
    failure is logged and retried after ``reconnect_delay`` seconds.

    Args:
        settings: Connection and routing settings. Defaults are used if omitted.
        reconnect_delay: Seconds to wait before reconnecting after a failure.
    """
    settings = settings or ConsumerSettings()

    while True:
        try:
            consume(settings)
            # consume() returned without raising -> a clean stop (Ctrl+C
            # inside start_consuming). Exit the service loop.
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
    """Process every message currently waiting, then return (no blocking wait).

    This is the one-shot / scheduled-run counterpart to
    :func:`consume_forever`. It pulls messages with ``basic_get`` until the
    queue is empty, dispatching each through the same :func:`_handle_message`
    logic (so ack/nack behaviour is identical), then returns. Ideal for calling
    from ``main.py`` on the existing Windows Task Scheduler cadence when running
    a persistent service is not desired.

    Args:
        settings: Connection and routing settings. Defaults are used if omitted.

    Returns:
        The number of messages handled in this drain.

    Raises:
        ConsumeError: If pika is unavailable or the broker cannot be reached.
    """
    if pika is None:
        raise ConsumeError(
            f"The 'pika' package is required to consume messages: {_IMPORT_ERROR}"
        )

    settings = settings or ConsumerSettings()

    credentials = pika.PlainCredentials(settings.username, settings.password)
    parameters = pika.ConnectionParameters(
        host=settings.host,
        port=settings.port,
        virtual_host=settings.virtual_host,
        credentials=credentials,
        connection_attempts=settings.connection_attempts,
        retry_delay=settings.retry_delay,
    )

    handled = 0
    connection = None
    try:
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue=settings.queue, durable=settings.durable)

        while True:
            method, properties, body = channel.basic_get(
                queue=settings.queue, auto_ack=False
            )
            if method is None:
                break  # queue is empty
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
    """Build the CLI argument parser for launching the consumer directly."""
    defaults = ConsumerSettings()
    parser = argparse.ArgumentParser(
        description="Consume web-extract jobs from the Swagman/WebExtract queue."
    )
    parser.add_argument("--host", default=defaults.host, help="RabbitMQ host.")
    parser.add_argument("--port", type=int, default=defaults.port, help="AMQP port.")
    parser.add_argument("--queue", default=defaults.queue, help="Queue name.")
    parser.add_argument("--username", default=defaults.username, help="AMQP username.")
    parser.add_argument("--password", default=defaults.password, help="AMQP password.")
    parser.add_argument(
        "--virtual-host", default=defaults.virtual_host, help="AMQP virtual host."
    )
    parser.add_argument(
        "--prefetch",
        type=int,
        default=defaults.prefetch,
        help="Max unacknowledged messages delivered at once.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain all currently-queued messages and exit, instead of "
        "running as an always-on service.",
    )
    return parser


def main() -> int:
    """CLI wrapper around the consumer.

    By default this runs as an always-on, auto-reconnecting service
    (:func:`consume_forever`) so messages are processed the instant they are
    pushed. Pass ``--once`` to instead drain whatever is queued and exit
    (:func:`drain`), which suits scheduled/one-shot invocation.

    Returns:
        Process exit code: 0 on a clean stop/drain, 1 on a connection error.
    """
    args = _build_arg_parser().parse_args()
    settings = ConsumerSettings(
        host=args.host,
        port=args.port,
        queue=args.queue,
        username=args.username,
        password=args.password,
        virtual_host=args.virtual_host,
        prefetch=args.prefetch,
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
