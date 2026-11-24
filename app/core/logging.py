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
import re
import sys
from datetime import UTC, datetime


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
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
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
        # Merge structured ``extra=`` context (e.g. the failing component
        # of a health check) so it is preserved in the JSON output.
        for key, value in record.__dict__.items():
            if key not in log_record and key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "message", "taskName",
            }:
                log_record[key] = value
        return json.dumps(log_record)


_SECRET_QUERY = re.compile(r"((?:^|[?&])token=)[^&\s]*", re.IGNORECASE)


class RedactQuerySecretsFilter(logging.Filter):
    """Mask ``?token=`` values in uvicorn access lines.

    Google Pub/Sub push cannot send custom headers, so the RTDN endpoint
    accepts its shared secret as a query parameter; this keeps that secret
    out of the access log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(
                _SECRET_QUERY.sub(r"\1***", arg) if isinstance(arg, str) and "token=" in arg.lower() else arg
                for arg in record.args
            )
        return True


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

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactQuerySecretsFilter) for f in access_logger.filters):
        access_logger.addFilter(RedactQuerySecretsFilter())


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.

    This is a thin wrapper around ``logging.getLogger(name)`` so that
    other modules can import a single function instead of the
    ``logging`` module directly.
    """
    return logging.getLogger(name)
