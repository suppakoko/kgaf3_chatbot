"""ID 생성 헬퍼 (ULID 우선, fallback uuid4)."""

from __future__ import annotations

import secrets
import time


def ulid_str() -> str:
    """26자 Crockford-Base32 ULID (ULID 사양 호환).

    ulid-py 미설치 환경 fallback. 시간 정렬 가능 + 충돌 회피.
    """
    # 48-bit timestamp ms + 80-bit random (총 128-bit → Crockford-Base32 26자)
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF  # 48 bit
    rand = secrets.randbits(80)  # 80 bit
    value = (ts_ms << 80) | rand

    # Crockford-Base32 encode (대문자, 0,1,O,I 사용)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    out = []
    for _ in range(26):
        out.append(alphabet[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def short_id(prefix: str = "") -> str:
    """디버그/로그용 짧은 ID (16 hex chars)."""
    rid = secrets.token_hex(8)
    return f"{prefix}{rid}" if prefix else rid
