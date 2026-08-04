"""
Structured (JSON) logging configuration.

This module configures the root Python logger to emit log records as
JSON lines to stdout.  JSON-formatted logs are easily parsed by log
aggregators (e.g. ELK stack, Datadog, CloudWatch) and provide a
consistent, machine-readable structure.

Key components:
    - :class:`JsonFormatter`: Converts a :class:`logging.LogRecord` into a
      JSON string with standard fields (timestamp, level, message, etc.).
    - :func:`configure_logging`: Sets up the root logger with a
      :class:`StreamHandler` that uses :class:`JsonFormatter`.
    - :func:`get_logger`: Convenience wrapper around
      ``logging.getLogger(name)``.
"""

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Custom log formatter that serialises each record as a JSON object.

    The output includes:
        - ``timestamp``: ISO-8601 UTC timestamp of when the record was created.
        - ``level``: Log level name (e.g. ``"INFO"``, ``"WARNING"``).
        - ``message``: The formatted log message.
        - ``module``: Name of the Python module that emitted the record.
        - ``process``: OS process ID.
        - ``thread``: Thread ID.
        - ``exception``: (optional) Formatted traceback, included only
          when the record carries exception info (``exc_info``).
    """

    def format(self, record: logging.LogRecord) -> str:
        """Build a JSON string from a :class:`logging.LogRecord`."""
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "process": record.process,
            "thread": record.thread,
        }
        # If the log record was created with ``logger.exception()`` or
        # ``logger.error(..., exc_info=True)``, include the traceback.
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record)


def configure_logging() -> None:
    """Configure the root logger for structured JSON output.

    - Attaches a single :class:`StreamHandler` writing to ``stdout``.
    - Sets the handler's formatter to :class:`JsonFormatter`.
    - Sets the root logger level to ``INFO``.
    - Disables propagation to prevent duplicate log entries from
      parent loggers.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)
    root_logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.

    This is a thin wrapper around ``logging.getLogger(name)`` so that
    other modules can import a single function instead of the
    ``logging`` module directly.
    """
    return logging.getLogger(name)
