"""
Swagman package - web-extraction bot and its RabbitMQ consumer.

Contains the web-extraction logic (``swagman.py``, ``web_extract.py``) and the
consumer that drives it from the ``Swagman/WebExtract`` queue
(``queue_consumer.py``).

Submodules are intentionally *not* imported here. ``queue_consumer`` pulls in
``pika`` and other Kelpie modules at import time, so importing the package
should stay cheap; callers import what they need explicitly, e.g.::

    from Swagman.queue_consumer import ConsumerSettings, drain
"""
