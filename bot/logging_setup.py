"""structlog configuration shared by the bot and the OAuth web service.

Redacts anything that looks like a credential before it ever reaches a sink.
"""
from __future__ import annotations

import logging
import re

import structlog

_SENSITIVE_KEYS = {
    "token", "access_token", "refresh_token", "session", "user_session",
    "session_string", "fernet_key", "client_secret", "code", "code_verifier",
    "authorization", "upload_session_uri",
}

_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9\-_\.]+", re.IGNORECASE)


def _redact_processor(logger, method_name, event_dict):
    for key in list(event_dict.keys()):
        lowered = key.lower()
        if any(s in lowered for s in _SENSITIVE_KEYS):
            val = event_dict[key]
            if isinstance(val, str) and val:
                event_dict[key] = val[:6] + "…redacted"
            else:
                event_dict[key] = "…redacted"
    event = event_dict.get("event")
    if isinstance(event, str):
        event_dict["event"] = _BEARER_RE.sub(r"\1…redacted", event)
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=log_level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(log_level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
