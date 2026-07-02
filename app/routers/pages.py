"""페이지 라우터 — Jinja2 HTML 응답.

GET /  → templates/index.html
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.utils.log import get_logger

log = get_logger("router.pages")

router = APIRouter(tags=["pages"])

templates = Jinja2Templates(directory="templates")


def _compute_asset_version() -> str:
    """정적 자산 cache-busting 토큰.

    가장 최근 수정된 static/js 또는 static/css 파일의 mtime 을 사용.
    서버 재시작 + 파일 변경마다 새 토큰이 발급돼 브라우저가 새로 받아간다.
    실패하면 fallback 으로 process 시작 시간 사용.
    """
    try:
        roots = [Path("static/js"), Path("static/css")]
        latest = 0.0
        for r in roots:
            if not r.exists():
                continue
            for p in r.rglob("*"):
                if p.is_file():
                    m = p.stat().st_mtime
                    if m > latest:
                        latest = m
        if latest > 0:
            return str(int(latest))
    except Exception:
        pass
    import time as _t
    return str(int(_t.time()))


_ASSET_V = _compute_asset_version()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """메인 페이지 — 채팅 + 스크리닝 UI."""
    log.debug("pages.index.serve")
    # Starlette 0.x+ TemplateResponse: request 가 첫 인자
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "default_model": settings.llm_default_model,
            "allowed_models": settings.allowed_models,
            "asset_v": _ASSET_V,
        },
    )
