"""The RTDN shared secret never reaches the access log."""

import logging

from app.core.logging import RedactQuerySecretsFilter


def test_access_log_masks_query_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", "/api/v1/billing/google/rtdn?token=s3cret&x=1", "1.1", 200),
        None,
    )
    RedactQuerySecretsFilter().filter(record)
    message = record.getMessage()
    assert "s3cret" not in message
    assert "token=***&x=1" in message
