"""세션 단위 인메모리 pub/sub.

스크리닝 파이프라인이 발행한 stage 이벤트를 채팅 WebSocket 으로 전달하기 위한 단순 브로커.
- session_id 키별 구독자 큐 목록 관리
- screening_service 의 ws_broadcast 콜백 → publish(session_id, event)
- chat WS 핸들러가 subscribe(session_id) 한 큐를 polling 하여 ws.send_text

WS 가 끊기면 unsubscribe 로 큐 정리. 큐 가득 차면 drop (백프레셔보다 streaming 우선).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class EventBus:
    """단일 프로세스 내 세션 pub/sub. uvicorn --workers 1 가정."""

    def __init__(self, queue_size: int = 512) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._job_to_session: dict[str, str] = {}
        self._queue_size = queue_size

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subs[session_id].append(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        if session_id in self._subs:
            try:
                self._subs[session_id].remove(q)
            except ValueError:
                pass
            if not self._subs[session_id]:
                del self._subs[session_id]

    def link_job(self, job_id: str, session_id: str) -> None:
        """job_id → session_id 매핑 저장 (screening service 가 session 모를 때 라우팅용)."""
        self._job_to_session[job_id] = session_id

    def session_for_job(self, job_id: str) -> str | None:
        return self._job_to_session.get(job_id)

    async def publish(self, session_id: str, event: dict[str, Any]) -> None:
        """session_id 의 모든 구독 큐에 이벤트 enqueue. 큐 가득 차면 해당 큐 drop (이벤트 손실 허용)."""
        for q in list(self._subs.get(session_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def publish_for_job(self, job_id: str, event: dict[str, Any]) -> None:
        """job_id 매핑 기반 publish. 매핑 없으면 noop."""
        sid = self._job_to_session.get(job_id)
        if sid:
            await self.publish(sid, event)


# 모듈 싱글톤 (uvicorn worker=1 전제)
bus = EventBus()
