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
    publish_local_ai,
    LOCAL_AI_CONFIG_KEY,
    publish_fallback,
    FALLBACK_CONFIG_KEY,
)

__all__ = [
    "QueueSettings",
    "publish_message",
    "PublishError",
    "publish_web_extract",
    "WEB_EXTRACT_CONFIG_KEY",
    "publish_local_ai",
    "LOCAL_AI_CONFIG_KEY",
    "publish_fallback",
    "FALLBACK_CONFIG_KEY",
]

# The consumer dispatcher is Kelpie-specific (it imports the consumer modules)
# and is not part of the vendored publish-only surface, so its import is
# optional: a vendored copy of this package without the dispatcher still works.
try:
    from .consumer_dispatcher import (
        start_if_idle,
        is_running,
        run_loop,
        drain_all_once,
    )

    __all__ += [
        "start_if_idle",
        "is_running",
        "run_loop",
        "drain_all_once",
    ]
except Exception:  # pragma: no cover - dispatcher absent in vendored copies
    pass
