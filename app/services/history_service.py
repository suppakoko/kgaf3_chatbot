"""히스토리 서비스 — SQLite 세션·메시지 영속화 thin wrapper.

backend_plan.md §8 권위.
Phase 1: init / close / touch_session / save_message.
Phase 2+: get_session / list_messages / delete_session / search (stub).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import structlog

from app.database import init_database
from app.utils.ids import ulid_str

log = structlog.get_logger("service.history")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HistoryService:
    """SQLite 세션·메시지 CRUD 래퍼.

    사용 패턴:
        svc = HistoryService(db_path)
        await svc.init()
        ...
        await svc.close()
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    # ── 라이프사이클 ──────────────────────────────────────────────────────────

    async def init(self) -> None:
        """DB 초기화 (스키마 마이그레이션 포함) + 연결 오픈."""
        await init_database(self._db_path)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        log.info("history.init", db_path=str(self._db_path))

    async def close(self) -> None:
        """DB 연결 종료."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            log.info("history.closed")

    # ── Phase 1 메서드 ────────────────────────────────────────────────────────

    async def touch_session(self, session_id: str, model: str) -> None:
        """세션 생성(없으면) 또는 last_seen_at 갱신.

        sessions 테이블 upsert.
        """
        assert self._db is not None, "call init() first"
        now = _now_iso()
        await self._db.execute(
            """
            INSERT INTO sessions (session_id, created_at, last_seen_at, model)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                model = excluded.model
            """,
            (session_id, now, now, model),
        )
        await self._db.commit()
        log.debug("history.touch_session", session_id=session_id, model=model)

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        meta: dict | None = None,
    ) -> str:
        """채팅 메시지 저장 → msg_id 반환.

        role: "user" | "assistant" | "system"
        meta: 추가 메타데이터 (선택)
        """
        assert self._db is not None, "call init() first"
        msg_id = ulid_str()
        now = _now_iso()
        meta_json = json.dumps(meta) if meta else None

        await self._db.execute(
            """
            INSERT INTO chat_messages
                (msg_id, session_id, role, content, created_at, meta_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (msg_id, session_id, role, content, now, meta_json),
        )
        await self._db.commit()
        log.debug(
            "history.save_message",
            msg_id=msg_id,
            session_id=session_id,
            role=role,
        )
        return msg_id

    # ── Phase 2 메서드 ────────────────────────────────────────────────────────

    async def list_sessions(self, limit: int = 100) -> list[dict]:
        """최근 last_seen_at 내림차순으로 세션 목록 반환.

        각 항목에는 마지막 user 메시지 미리보기와 메시지 수를 포함한다.
        """
        assert self._db is not None, "call init() first"
        sql = """
            SELECT
                s.session_id,
                s.created_at,
                s.last_seen_at,
                s.model,
                (
                    SELECT COUNT(*) FROM chat_messages m
                    WHERE m.session_id = s.session_id
                ) AS message_count,
                (
                    SELECT m.content FROM chat_messages m
                    WHERE m.session_id = s.session_id
                      AND m.role = 'user'
                    ORDER BY m.created_at DESC
                    LIMIT 1
                ) AS last_user_message
            FROM sessions s
            ORDER BY s.last_seen_at DESC
            LIMIT ?
        """
        async with self._db.execute(sql, (limit,)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_session(self, session_id: str) -> dict | None:
        """세션 메타데이터 조회."""
        assert self._db is not None, "call init() first"
        async with self._db.execute(
            "SELECT session_id, created_at, last_seen_at, model FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_messages(
        self,
        session_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        """세션 메시지 목록 조회 (시간순 오름차순)."""
        assert self._db is not None, "call init() first"
        async with self._db.execute(
            """
            SELECT msg_id, session_id, role, content, created_at, meta_json
            FROM chat_messages
            WHERE session_id = ?
            ORDER BY created_at ASC
            LIMIT ? OFFSET ?
            """,
            (session_id, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_session(self, session_id: str) -> None:
        """세션 및 연관 메시지 삭제."""
        assert self._db is not None, "call init() first"
        await self._db.execute(
            "DELETE FROM chat_messages WHERE session_id = ?",
            (session_id,),
        )
        await self._db.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        await self._db.commit()
        log.info("history.delete_session", session_id=session_id)

    async def list_jobs(
        self,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """스크리닝 잡 목록 조회 (started_at 내림차순).

        session_id 가 주어지면 해당 세션 잡만, 없으면 전체.
        """
        assert self._db is not None, "call init() first"
        if session_id is None:
            sql = """
                SELECT job_id, session_id, library_id, status, stage,
                       started_at, ended_at, error
                FROM screening_jobs
                ORDER BY started_at DESC
                LIMIT ?
            """
            params: tuple = (limit,)
        else:
            sql = """
                SELECT job_id, session_id, library_id, status, stage,
                       started_at, ended_at, error
                FROM screening_jobs
                WHERE session_id = ?
                ORDER BY started_at DESC
                LIMIT ?
            """
            params = (session_id, limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def search_messages(self, query: str, session_id: str | None = None) -> list[dict]:
        """메시지 전문 검색. (Phase 3)"""
        raise NotImplementedError("Phase 3")
