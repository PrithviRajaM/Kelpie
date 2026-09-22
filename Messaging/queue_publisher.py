r"""
Shared RabbitMQ publisher for the Numbat bots.

This module is deliberately self-contained: it depends only on the Python
standard library and the ``pika`` package. It has **no** dependency on any
bot-specific code (Kelpie config, Kelpie logger, etc.), so the identical file
can be vendored into each bot that publishes to the shared queue
(Kelpie - Task Scheduler, Magpie - AI Agent, ...).

Vendoring strategy (option 3 - shared script copied between bots)
-----------------------------------------------------------------
Keep one canonical copy of this ``Messaging`` folder and copy it verbatim into
every bot repo. Because the module has no external coupling, the copies stay
interchangeable. When the publish contract changes, update the canonical copy
and re-sync. (When drift becomes painful, this same module can be lifted into
an installable package with no code changes.)

Usage
-----
Generic (any queue) - the universal path:

    from Messaging import QueueSettings, publish_message

    settings = QueueSettings(
        host="localhost",
        port=5672,
        queue="magpie.tasks",
    )
    publish_message(settings, {"session_id": 12, "next_action": "Analyse..."})

Convenience (the Swagman/WebExtract queue) - a thin wrapper that reads its
connection and queue settings from ``queue.config``, so the caller passes only
the payload:

    from Messaging import publish_web_extract

    publish_web_extract({
        "task_identifier": "Extract_Coles_Menu",
        "web_url": "https://www.coles.com.au/browse",
        "destination_folder_name": "coles_item_categories",
    })

The publisher opens a short-lived connection per call, declares the queue as
durable, and publishes a persistent JSON message. That keeps callers stateless
and avoids sharing a fragile long-lived connection across scheduler runs.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

try:
    import pika
except ImportError as exc:  # pragma: no cover - surfaced at call time
    pika = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


class PublishError(Exception):
    """Raised when a message could not be published to the queue."""


# ---------------------------------------------------------------------------
# Broker configuration (Messaging/queue.config)
#
# Connection details (host, port, credentials, vhost) and per-queue settings
# live in ``queue.config`` next to this module, so callers no longer pass the
# endpoint/credentials around. The file is JSON for consistency with the rest
# of the project (kelpie_config.json, ollama_config.json).
# ---------------------------------------------------------------------------

#: Path to the messaging config, co-located with this module.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue.config")

# Fallback defaults, used only if the file is missing or a key is absent.
_CONNECTION_DEFAULTS = {
    "host": "localhost",
    "port": 5672,
    "username": "guest",
    "password": "guest",
    "virtual_host": "/",
    "connection_attempts": 3,
    "retry_delay": 2.0,
}

# Cache so the file is read at most once per process.
_config_cache = None


def _load_config() -> dict:
    """Read ``queue.config`` once and return it as a dict.

    A missing or malformed file is tolerated: callers fall back to the
    connection defaults so publishing still works without the file present.
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    data: dict = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        # A broken config must not crash publishing; fall back to defaults.
        pass

    _config_cache = data
    return _config_cache


def _settings_for(config_key: str) -> "QueueSettings":
    """Build :class:`QueueSettings` for a named queue defined in ``queue.config``.

    The shared ``connection`` block is layered first, then the per-queue block
    under ``queues.<config_key>`` (which may override anything, e.g. vhost),
    and finally passed through ``QueueSettings.from_mapping`` so unknown keys
    are ignored and missing ones fall back to dataclass defaults.

    Args:
        config_key: Key under the ``queues`` object in ``queue.config``.

    Returns:
        A ready-to-use ``QueueSettings`` for that queue.

    Raises:
        PublishError: If the named queue is not defined in the config.
    """
    config = _load_config()

    connection = dict(_CONNECTION_DEFAULTS)
    file_connection = config.get("connection")
    if isinstance(file_connection, dict):
        connection.update(file_connection)

    queues = config.get("queues")
    queue_conf = queues.get(config_key) if isinstance(queues, dict) else None
    if not isinstance(queue_conf, dict):
        raise PublishError(
            f"Queue '{config_key}' is not defined in {CONFIG_PATH}."
        )

    merged = dict(connection)
    merged.update(queue_conf)
    return QueueSettings.from_mapping(merged)


@dataclass
class QueueSettings:
    """Connection + routing settings for the shared RabbitMQ service.

    Attributes:
        host: RabbitMQ host (the service runs locally by default).
        port: AMQP port (RabbitMQ default is 5672).
        queue: Name of the queue Magpie consumes from.
        username: AMQP username.
        password: AMQP password.
        virtual_host: AMQP virtual host.
        exchange: Exchange to publish through. Empty string uses the default
            (direct) exchange, in which case the routing key is the queue name.
        routing_key: Routing key to use when publishing through a non-default
            ``exchange``. Ignored for the default exchange (the queue name is
            used). Empty string falls back to the queue name.
        durable: Declare the queue durable so it survives a broker restart.
        connection_attempts: How many times pika should try to connect.
        retry_delay: Seconds pika waits between connection attempts.
    """

    host: str = "localhost"
    port: int = 5672
    queue: str = "magpie.tasks"
    username: str = "guest"
    password: str = "guest"
    virtual_host: str = "/"
    exchange: str = ""
    routing_key: str = ""
    durable: bool = True
    connection_attempts: int = 3
    retry_delay: float = 2.0

    @classmethod
    def from_mapping(cls, data: Optional[dict]) -> "QueueSettings":
        """Build settings from a plain dict, ignoring unknown keys.

        Missing keys fall back to the dataclass defaults, so a partial config
        (for example only ``host`` and ``queue``) is valid.
        """
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


def publish_message(
    settings: QueueSettings,
    payload: Any,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Publish a single message to the configured queue.

    A short-lived connection is opened, the queue is declared durable, the
    message is published as persistent JSON, and the connection is closed.

    Args:
        settings: Connection and routing settings.
        payload: The message body. ``dict``/``list`` are serialized to JSON;
            a ``str`` is sent as-is; anything else is ``json.dumps``-ed.
        log: Optional callback invoked with human-readable progress/info
            strings. Bots can pass their own logger here (e.g. a lambda that
            forwards to kelpie_logger) without this module importing it.

    Raises:
        PublishError: If pika is unavailable or publishing fails.
    """
    if pika is None:
        raise PublishError(
            f"The 'pika' package is required to publish messages: {_IMPORT_ERROR}"
        )

    body = _encode_payload(payload)

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

        # Ensure the queue exists and survives broker restarts.
        channel.queue_declare(queue=settings.queue, durable=settings.durable)

        # With the default exchange (""), the routing key must be the queue
        # name so the broker routes straight to it. With a custom exchange,
        # honor an explicitly configured routing_key and fall back to the
        # queue name only when none was provided.
        if settings.exchange == "":
            routing_key = settings.queue
        else:
            routing_key = settings.routing_key or settings.queue

        channel.basic_publish(
            exchange=settings.exchange,
            routing_key=routing_key,
            body=body.encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,  # persistent
            ),
        )

        if log:
            log(
                f"Published message to queue '{settings.queue}' on "
                f"{settings.host}:{settings.port}."
            )
    except PublishError:
        raise
    except Exception as exc:  # pika raises a variety of connection errors
        raise PublishError(
            f"Failed to publish to queue '{settings.queue}' on "
            f"{settings.host}:{settings.port}: {exc}"
        ) from exc
    finally:
        if connection is not None and connection.is_open:
            try:
                connection.close()
            except Exception:
                # Closing failures must not mask a successful publish.
                pass


def _encode_payload(payload: Any) -> str:
    """Serialize a payload to a JSON string (str payloads pass through)."""
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Convenience: the Swagman/WebExtract queue
#
# The generic ``publish_message`` above can post to *any* queue; this wrapper
# targets one specific queue. Its connection and queue settings come entirely
# from ``queue.config`` (the ``web_extract`` entry), so callers pass only the
# payload. Queue identity is documented in the saved RabbitMQ management page
# ``Reference/RabbitMQ... Swagman_WebExtract ...html`` (URL
# ``/#/queues/%2F/Swagman%2FWebExtract``): virtual host ``/``, queue name
# ``Swagman/WebExtract``. Consumed by ``Swagman/queue_consumer.py``.
# ---------------------------------------------------------------------------

#: Config key (under ``queues`` in ``queue.config``) for the web-extract queue.
WEB_EXTRACT_CONFIG_KEY = "web_extract"


def publish_web_extract(
    payload: Any,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Publish a single web-extract job to the ``Swagman/WebExtract`` queue.

    Convenience wrapper around :func:`publish_message`. Connection and queue
    settings are read from ``queue.config`` (the ``web_extract`` entry), so the
    caller supplies only the payload. The message contract expected by the
    consumer is::

        {
            "task_identifier": "Extract_Coles_Menu",
            "web_url": "https://www.coles.com.au/browse",
            "destination_folder_name": "coles_item_categories"
        }

    Args:
        payload: The job body (typically the dict shown above).
        log: Optional progress/info callback forwarded to
            :func:`publish_message`.

    Raises:
        PublishError: If pika is unavailable, the queue is not configured, or
            publishing fails.
    """
    settings = _settings_for(WEB_EXTRACT_CONFIG_KEY)
    publish_message(settings, payload, log=log)
