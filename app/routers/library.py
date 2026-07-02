"""라이브러리 라우터 — 화합물 라이브러리 업로드/관리.

POST /api/library  — 파일 업로드 + 파싱 + DB 저장
GET  /api/library/{id}  — 라이브러리 상세 (Phase 2+ 구현)

interface-contracts §1 권위.
보안: security_plan §4.4 (safe_resolve) + 파일 크기/MIME 제한.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers._deps import get_library
from app.services.library_service import LibraryService
from app.utils.log import get_logger
from app.utils.validators import safe_resolve, ValidationError

log = get_logger("router.library")

router = APIRouter(prefix="/api", tags=["library"])

# 파일 크기 상한 (50 MB)
_MAX_FILE_BYTES = 50 * 1024 * 1024

# 허용 확장자 (소문자 비교)
_ALLOWED_EXTENSIONS = {".sdf", ".smi", ".csv", ".txt"}

# 파일명 sanitize: 알파벳·숫자·-_. 만 허용
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_\-.]")


def _sanitize_filename(name: str) -> str:
    """경로 구분자 및 특수문자 제거."""
    base = Path(name).name  # 경로 구분자 제거
    return _SAFE_FILENAME_RE.sub("_", base)


@router.post("/library")
async def upload_library(
    request: Request,
    file: UploadFile = File(...),
    library: LibraryService = Depends(get_library),
) -> JSONResponse:
    """화합물 라이브러리 파일 업로드.

    - 허용 형식: .sdf, .smi, .csv, .txt
    - 크기 제한: 50 MB
    - 응답: {"ok": true, "data": {"library_id", "n_entries", "n_unique", "n_duplicates"}}
    """
    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()

    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": {
                    "code": "LIBRARY_PARSE_FAILED",
                    "message": f"unsupported file type: {suffix!r}. allowed: {sorted(_ALLOWED_EXTENSIONS)}",
                },
            },
        )

    raw_bytes = await file.read()

    if len(raw_bytes) > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "ok": False,
                "error": {
                    "code": "LIBRARY_TOO_LARGE",
                    "message": f"file exceeds 50 MB limit ({len(raw_bytes):,} bytes)",
                },
            },
        )

    # 파일명 sanitize + 경로 traversal 방어
    safe_name = _sanitize_filename(filename)
    try:
        dest_path = safe_resolve(settings.afmm_library_dir, safe_name)
    except ValidationError as exc:
        log.warning("library.path_validation_failed", filename=filename, error=str(exc))
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": {"code": "LIBRARY_PARSE_FAILED", "message": "invalid filename"},
            },
        )

    log.info(
        "library.upload.received",
        filename=filename,
        size=len(raw_bytes),
        suffix=suffix,
    )

    # 파싱 (sync — RDKit CPU bound)
    try:
        lib = library.parse(raw_bytes, filename)
    except Exception as exc:
        log.error("library.parse_failed", filename=filename, exc_info=exc)
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": {
                    "code": "LIBRARY_PARSE_FAILED",
                    "message": f"parse error: {exc}",
                },
            },
        )

    # DB 저장 (save_to_db 는 None 반환; lib 객체에 모든 메타 존재)
    try:
        await library.save_to_db(lib)
    except Exception as exc:
        log.error("library.save_failed", exc_info=exc)
        raise HTTPException(
            status_code=500,
            detail={
                "ok": False,
                "error": {"code": "INTERNAL_ERROR", "message": "failed to save library"},
            },
        )

    saved = {
        "library_id": lib.library_id,
        "n_entries": lib.n_entries,
        "n_unique": lib.n_unique,
        "n_duplicates": lib.n_duplicates,
        "source_filename": lib.source_filename,
    }
    log.info("library.upload.done", **saved)

    return JSONResponse(
        content={"ok": True, "data": saved},
        status_code=201,
    )


@router.get("/library/{library_id}")
async def get_library_detail(
    library_id: str,
    library: LibraryService = Depends(get_library),
) -> JSONResponse:
    """라이브러리 상세 조회 (Phase 2 구현 예정, Phase 1 stub)."""
    log.info("library.get_detail.stub", library_id=library_id)
    raise HTTPException(
        status_code=501,
        detail={
            "ok": False,
            "error": {
                "code": "NOT_IMPLEMENTED",
                "message": "GET /api/library/{id} is not implemented in Phase 1",
            },
        },
    )
