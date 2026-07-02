"""WebSocket message envelope (Pydantic).

backend_plan §3.3, design 03-interface-contracts §2 권위.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.utils.ids import ulid_str


class WSMessage(BaseModel):
    """양방향 WebSocket 메시지 envelope."""

    type: str
    request_id: str = Field(default_factory=ulid_str)
    payload: dict[str, Any] = Field(default_factory=dict)


class WSError(BaseModel):
    """WS error 메시지 (downlink only)."""

    type: Literal["error"] = "error"
    request_id: str | None = None
    code: str
    message: str
