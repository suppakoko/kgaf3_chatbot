"""Target resolve REST endpoint 응답 wrapper.

interface-contracts §2 (Phase 2 신규 router 의존) 권위.
TargetInput / TargetPrepResult 는 app.services.target_service 에 정의되어 있어
여기서는 REST 응답을 감싸는 wrapper 만 둔다.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.services.target_service import TargetPrepResult


class TargetResolveResponse(BaseModel):
    """POST /api/target/resolve 응답."""

    ok: bool = True
    target: TargetPrepResult
