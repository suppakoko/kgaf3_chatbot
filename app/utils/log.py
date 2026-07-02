"""structlog JSON logging configuration.

Zero-Script QA 호환: stdout JSON line + redact processor (sk-or-, Bearer ...).
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

# 정규식 redact: API key / Bearer token 본문이 메시지/예외 trace 에 노출되는 케이스 방어
_REDACT_PATTERNS = [
    re.compile(r"sk-or-[A-Za-z0-9_-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
]


def _redact_value(value: Any) -> Any:
    """문자열 값에 대해 정규식 패턴 매치 시 ***REDACTED*** 로 치환."""
    if isinstance(value, str):
        for pat in _REDACT_PATTERNS:
            value = pat.sub("***REDACTED***", value)
        return value
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _redact_processor(logger, method_name, event_dict):
    """구조화 필드 + 키 이름 + 패턴 redact."""
    sensitive_keys = {"password", "token", "secret", "authorization", "api_key", "key"}
    for key in list(event_dict.keys()):
        if any(s in key.lower() for s in sensitive_keys):
            event_dict[key] = "***REDACTED***"
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """structlog + stdlib logging 통합 설정.

    Args:
        level: INFO | DEBUG | WARNING | ERROR
        fmt: "json" (production) | "console" (dev)
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso")

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        timestamper,
        _redact_processor,
        structlog.processors.format_exc_info,
    ]

    # structlog>=23 의 dict_tracebacks 가 있으면 사용 (json 친화적)
    if hasattr(structlog.processors, "dict_tracebacks"):
        shared_processors.append(structlog.processors.dict_tracebacks)

    if fmt == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        stream=sys.stdout,
        level=level.upper(),
        format="%(message)s",
    )


def get_logger(name: str = "afmm_chat") -> structlog.stdlib.BoundLogger:
    """구조화 로거 획득 (서비스 표준 필드 자동 부착)."""
    return structlog.get_logger(name).bind(service="afmm_chat")
