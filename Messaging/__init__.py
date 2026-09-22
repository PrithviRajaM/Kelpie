"""
Messaging package - shared RabbitMQ helpers for the Numbat bots.

This package is intentionally self-contained (standard library + ``pika`` only)
so the exact same files can be vendored, unchanged, into every bot that needs
to talk to the shared RabbitMQ service (Kelpie, Magpie, ...).

See ``queue_publisher.py`` for the publish API.
"""

from .queue_publisher import (
    QueueSettings,
    publish_message,
    PublishError,
    publish_web_extract,
    WEB_EXTRACT_CONFIG_KEY,
)

__all__ = [
    "QueueSettings",
    "publish_message",
    "PublishError",
    "publish_web_extract",
    "WEB_EXTRACT_CONFIG_KEY",
]
