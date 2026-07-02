"""GraphRAG MCP client — SSE transport to the bundled graphrag-mcp-server container.

afmm_chat 의 GraphRAG 모드는 더 이상 graphrag.py 를 in-process 로 임포트하지 않는다.
대신 self-contained Docker 스택(``yoonjuho94/graphrag-neo4j:1.0`` +
``yoonjuho94/graphrag-mcp-server:1.0``)이 노출하는 **MCP SSE 서버**(기본
``http://graphrag-mcp:8893/sse``)에 붙어 아래 3개 도구를 호출한다.

  - ``graphrag_query(question, provider)``
        자연어 → Text2Cypher → 실행 → NL 답변 (원샷, Algorithm 4).
        반환 JSON: ``{answer, cypher, row_count, rows_preview, token_usage,
        model_id, timestamp, provider}`` (또는 실패 시 ``{error}``).
  - ``get_kg_stats()``  : Neo4j KG 노드/관계 카운트.
  - ``run_cypher(query, params)`` : 읽기 전용 Cypher 직접 실행.

설계 결정 — **stateless per-call**:
    각 호출마다 새 SSE 세션을 연다 (connect → initialize → call_tool → close).
    GraphRAG 질의는 채팅 기반의 저빈도 요청이라 per-call 연결 오버헤드가
    허용 가능하고, FastAPI app.state 에 장수명 SSE 세션을 유지하는 lifecycle
    복잡도(끊김/재연결/idle 만료)를 피할 수 있다. (af3/openmm 의
    streamable-http MCPHttpClient 와 달리 SSE 전송은 두 채널이라 세션 유지가
    더 까다롭다.)

mcp SDK 는 이미 의존성(``mcp>=1.0.0``)에 포함되어 있으므로 추가 패키지는 없다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

log = structlog.get_logger("service.graphrag_mcp")

# SSE 세션이 이벤트 사이에서 대기하는 최대 시간(초). graphrag_query 는 내부에서
# LLM 을 2회(Cypher 생성 + 답변 합성) 호출하므로 넉넉히 잡는다. 전체 상한은
# 호출부의 asyncio.wait_for(timeout=self.timeout) 가 별도로 강제한다.
_SSE_READ_TIMEOUT = 300.0
# 최초 SSE 연결(HTTP GET /sse) 자체의 timeout. 서버가 떠 있으면 즉시 붙는다.
_SSE_CONNECT_TIMEOUT = 10.0


class GraphRAGMCPError(Exception):
    """GraphRAG MCP 클라이언트 통일 예외.

    error_type 으로 호출자가 분기한다:
        DISABLED, TIMEOUT, TRANSPORT, TOOL_ERROR, QUERY_FAILED,
        STATS_FAILED, CYPHER_FAILED, BAD_RESPONSE.
    """

    def __init__(self, error_type: str, message: str):
        super().__init__(f"[{error_type}] {message}")
        self.error_type = error_type
        self.message = message


def _content_text_parts(result: Any) -> list[str]:
    """CallToolResult.content 에서 text 조각만 뽑는다 (pydantic TextContent)."""
    parts: list[str] = []
    for c in getattr(result, "content", None) or []:
        if getattr(c, "type", None) == "text":
            parts.append(getattr(c, "text", "") or "")
    return parts


def _flatten_error(result: Any) -> str:
    """isError=True 응답의 content 를 사람이 읽을 수 있는 텍스트로 평탄화."""
    parts = [p for p in _content_text_parts(result) if p]
    return " ".join(parts) or "(no error message)"


def _unwrap(result: Any) -> Any:
    """tools/call 응답 content 언래핑.

    - 단일 text content + JSON 파싱 가능 → dict/list 반환
    - 단일 text content + plain text  → str 반환
    - 그 외 → text 조각 리스트
    """
    parts = _content_text_parts(result)
    if len(parts) == 1:
        text = parts[0]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
    return parts


class GraphRAGMCPClient:
    """SSE 전송 GraphRAG MCP 클라이언트 (stateless per-call).

    Args:
        url: MCP SSE 엔드포인트 (예: ``http://graphrag-mcp:8893/sse``).
        auth_token: Bearer 토큰 (서버가 요구할 때만; 기본 빈 값).
        timeout: 단일 도구 호출의 전체 상한(초). graphrag_query 는 LLM 2회를
                 포함하므로 기본 180s.
        name: 로그/에러 메시지용 표시 이름.
    """

    def __init__(
        self,
        url: str,
        auth_token: str = "",
        timeout: float = 180.0,
        name: str = "graphrag-mcp",
    ):
        self.url = url
        self.auth_token = auth_token
        self.timeout = timeout
        self.name = name

    def _headers(self) -> dict[str, str] | None:
        return {"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else None

    async def _call(self, tool: str, args: dict | None, *, timeout: float | None = None) -> Any:
        """도구 1회 호출 — 새 SSE 세션을 열고 닫는다.

        Raises:
            GraphRAGMCPError: TIMEOUT / TRANSPORT / TOOL_ERROR.
        """
        # lazy import: mcp SDK 를 모듈 로드 시점이 아니라 실제 호출 시점에 가져와
        # (선택 기능인) GraphRAG 미사용 배포에서 import 부담을 없앤다.
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        to = timeout or self.timeout

        async def _run() -> Any:
            async with sse_client(
                self.url,
                headers=self._headers(),
                timeout=_SSE_CONNECT_TIMEOUT,
                sse_read_timeout=_SSE_READ_TIMEOUT,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, args or {})
                    if getattr(result, "isError", False):
                        raise GraphRAGMCPError("TOOL_ERROR", _flatten_error(result))
                    return _unwrap(result)

        try:
            return await asyncio.wait_for(_run(), timeout=to)
        except GraphRAGMCPError:
            raise
        except asyncio.TimeoutError as exc:
            raise GraphRAGMCPError(
                "TIMEOUT", f"{self.name} {tool} timed out after {to}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — 전송 계층 오류 통일 래핑
            raise GraphRAGMCPError("TRANSPORT", f"{self.name} {tool}: {exc}") from exc

    # ── 헬스체크 ──────────────────────────────────────────────────────────────

    async def ping(self, *, timeout: float = 6.0) -> bool:
        """SSE 연결 + initialize + tools/list 로 헬스체크. 실패해도 False 반환."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async def _run() -> bool:
            async with sse_client(
                self.url,
                headers=self._headers(),
                timeout=_SSE_CONNECT_TIMEOUT,
                sse_read_timeout=_SSE_READ_TIMEOUT,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()
                    return True

        try:
            return bool(await asyncio.wait_for(_run(), timeout=timeout))
        except Exception:  # noqa: BLE001 — ping 은 절대 예외를 던지지 않음
            return False

    # ── 도구 ──────────────────────────────────────────────────────────────────

    async def graphrag_query(
        self, question: str, provider: str = "openrouter", *, timeout: float | None = None
    ) -> dict[str, Any]:
        """자연어 질의 → 답변 + 메타데이터 (원샷).

        Returns:
            {answer, cypher, row_count, rows_preview, token_usage, model_id,
             timestamp, provider}.
        Raises:
            GraphRAGMCPError: QUERY_FAILED / BAD_RESPONSE / TIMEOUT / TRANSPORT.
        """
        res = await self._call(
            "graphrag_query", {"question": question, "provider": provider}, timeout=timeout
        )
        if isinstance(res, dict) and res.get("error"):
            raise GraphRAGMCPError("QUERY_FAILED", str(res["error"]))
        if not isinstance(res, dict):
            raise GraphRAGMCPError("BAD_RESPONSE", f"unexpected response: {str(res)[:200]}")
        return res

    async def get_kg_stats(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Neo4j KG 노드/관계 카운트. 실패 시 GraphRAGMCPError."""
        res = await self._call("get_kg_stats", {}, timeout=timeout or 15.0)
        if isinstance(res, dict) and res.get("error"):
            raise GraphRAGMCPError("STATS_FAILED", str(res["error"]))
        return res if isinstance(res, dict) else {}

    async def run_cypher(
        self, query: str, params: dict | None = None, *, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        """읽기 전용 Cypher 직접 실행. 실패 시 GraphRAGMCPError."""
        res = await self._call(
            "run_cypher", {"query": query, "params": params or {}}, timeout=timeout
        )
        if isinstance(res, dict) and res.get("error"):
            raise GraphRAGMCPError("CYPHER_FAILED", str(res["error"]))
        return res if isinstance(res, list) else []
