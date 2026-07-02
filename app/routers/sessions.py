"""세션/메시지 라우터 — 좌측 대화창 리스트 및 세션 메시지 조회용.

GET    /api/sessions             — 최근 세션 목록 (사이드바)
GET    /api/sessions/{sid}       — 세션 메타데이터
GET    /api/sessions/{sid}/messages — 세션 메시지 (대화 복원)
DELETE /api/sessions/{sid}       — 세션 + 메시지 삭제
GET    /api/sessions/{sid}/jobs  — 세션의 스크리닝 잡 목록
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.routers._deps import get_history
from app.services.history_service import HistoryService
from app.utils.log import get_logger

log = get_logger("router.sessions")

router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/sessions")
async def list_sessions(
    history: Annotated[HistoryService, Depends(get_history)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JSONResponse:
    """최근 사용 순 세션 목록 (사이드바 대화창 리스트)."""
    rows = await history.list_sessions(limit=limit)
    return JSONResponse({"ok": True, "sessions": rows})


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    history: Annotated[HistoryService, Depends(get_history)],
) -> JSONResponse:
    """세션 메타데이터 조회."""
    sess = await history.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "session not found"})
    return JSONResponse({"ok": True, "session": sess})


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    history: Annotated[HistoryService, Depends(get_history)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JSONResponse:
    """세션의 메시지 목록 (대화 복원용, 시간순)."""
    rows = await history.list_messages(session_id, limit=limit, offset=offset)
    return JSONResponse({"ok": True, "session_id": session_id, "messages": rows})


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    history: Annotated[HistoryService, Depends(get_history)],
) -> JSONResponse:
    """세션 + 연관 메시지 삭제."""
    await history.delete_session(session_id)
    return JSONResponse({"ok": True, "session_id": session_id})


@router.get("/sessions/{session_id}/jobs")
async def list_session_jobs(
    session_id: str,
    history: Annotated[HistoryService, Depends(get_history)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JSONResponse:
    """해당 세션에 속한 스크리닝 잡 목록."""
    rows = await history.list_jobs(session_id=session_id, limit=limit)
    return JSONResponse({"ok": True, "session_id": session_id, "jobs": rows})


@router.get("/jobs")
async def list_all_jobs(
    history: Annotated[HistoryService, Depends(get_history)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JSONResponse:
    """전체 스크리닝 잡 목록 (상태 패널 전역 표시용)."""
    rows = await history.list_jobs(session_id=None, limit=limit)
    return JSONResponse({"ok": True, "jobs": rows})
