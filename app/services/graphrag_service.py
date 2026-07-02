"""GraphRAG 서비스 — 번들된 graphrag-mcp-server(SSE :8893)에 붙는 얇은 wrapper.

afmm_chat 의 채팅 UI 에서 "GraphRAG" 모드 선택 시 KIST NPI Neo4j 지식그래프
(NPASS 3.0 + Open Targets release 25.12, 273,519 노드 / 1,493,463 관계)에
자연어로 질의한다.

설계 결정 (이전 in-process 방식에서 변경):
- 더 이상 graphrag.py 모듈을 in-process 로 임포트하거나 Neo4j 에 직접 bolt 로
  연결하지 않는다. 대신 self-contained Docker 스택이 노출하는 MCP SSE 서버
  (``GRAPHRAG_MCP_URL``, 기본 ``http://graphrag-mcp:8893/sse``)의 도구를 호출한다.
  → KG 데이터·Neo4j 드라이버·LLM 키(graphrag_query 용)는 모두 MCP 서버 컨테이너
    안에 있고, afmm_chat 은 순수 오케스트레이션 계층으로 남는다.
- MCP 서버의 ``graphrag_query`` 는 자연어→Cypher→실행→답변을 **원샷**으로
  수행한다 (개별 generate_cypher/synthesize 도구는 노출되지 않음). 따라서
  이 서비스도 원샷 ``query()`` 만 제공하고, chat 라우터가 반환 메타데이터
  (cypher, row_count, rows_preview, token_usage)로 진행 카드 단계를 렌더한다.

Triggers:
- ws/chat: payload.mode == "graphrag" 일 때 ScreeningService 대신 이 서비스 호출.
- /api/system/info: ping 으로 MCP/Neo4j 연결 상태 표시.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.graphrag_mcp import GraphRAGMCPClient, GraphRAGMCPError

log = structlog.get_logger("service.graphrag")


class GraphRAGServiceError(Exception):
    """GraphRAGService 공통 예외 (chat 라우터가 error_type 으로 분기)."""

    def __init__(self, error_type: str, message: str):
        super().__init__(f"[{error_type}] {message}")
        self.error_type = error_type
        self.message = message


class GraphRAGService:
    """graphrag-mcp-server(SSE) wrapper.

    Args:
        mcp_url: MCP SSE 엔드포인트 (예: ``http://graphrag-mcp:8893/sse``).
        enabled: False 이면 어떤 호출도 시도하지 않음 (GraphRAG 미배포 환경).
        auth_token: MCP 서버가 Bearer 인증을 요구할 때만 (기본 빈 값).
        default_provider: graphrag_query 기본 LLM provider ("openrouter").
        openrouter_model: 표시/이력용 메타 (실제 모델은 MCP 서버의 .env 가 결정).
        timeout: 단일 질의 상한(초).
    """

    def __init__(
        self,
        mcp_url: str,
        *,
        enabled: bool = True,
        auth_token: str = "",
        default_provider: str = "openrouter",
        openrouter_model: str = "anthropic/claude-opus-4-7",
        timeout: float = 180.0,
    ):
        self.mcp_url = mcp_url
        self.enabled = enabled
        self.default_provider = default_provider
        self.openrouter_model = openrouter_model
        self._client = GraphRAGMCPClient(
            url=mcp_url, auth_token=auth_token, timeout=timeout, name="graphrag-mcp"
        )

    # ── 헬스체크 / 상태 ────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """MCP SSE 서버 연결 확인. disabled 이거나 실패 시 False (예외 없음)."""
        if not self.enabled:
            return False
        return await self._client.ping()

    async def stats(self) -> dict[str, Any]:
        """KG 노드/관계 카운트. 실패 시 빈 dict (UI graceful 처리)."""
        if not self.enabled:
            return {}
        try:
            return await self._client.get_kg_stats()
        except GraphRAGMCPError as exc:
            log.warning("graphrag.stats_failed", error=exc.message, error_type=exc.error_type)
            return {}

    # ── 질의 (원샷) ────────────────────────────────────────────────────────────

    async def query(
        self, question: str, *, provider: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        """자연어 질문 → Text2Cypher → 실행 → NL 답변 (원샷, Algorithm 4).

        Returns:
            (answer_markdown, metadata_dict).
            metadata = {cypher, row_count, rows_preview, token_usage, model_id,
                        timestamp, provider}.

        Raises:
            GraphRAGServiceError: DISABLED / QUERY_FAILED / TIMEOUT / TRANSPORT.
        """
        if not self.enabled:
            raise GraphRAGServiceError(
                "DISABLED", "GraphRAG is disabled (GRAPHRAG_ENABLED=false)"
            )
        prov = provider or self.default_provider
        try:
            res = await self._client.graphrag_query(question, provider=prov)
        except GraphRAGMCPError as exc:
            log.error("graphrag.query_failed", error=exc.message, error_type=exc.error_type)
            raise GraphRAGServiceError(exc.error_type, exc.message) from exc

        answer = str(res.get("answer") or "")
        meta = {k: v for k, v in res.items() if k != "answer"}
        log.info(
            "graphrag.query_done",
            provider=prov,
            row_count=meta.get("row_count"),
            tokens_out=(meta.get("token_usage") or {}).get("output_tokens"),
        )
        return answer, meta

    async def run_cypher(
        self, query: str, params: dict | None = None
    ) -> list[dict[str, Any]]:
        """읽기 전용 Cypher 직접 실행 (디버깅/계약 테스트 보조; UI 비노출)."""
        if not self.enabled:
            raise GraphRAGServiceError("DISABLED", "GraphRAG is disabled")
        try:
            return await self._client.run_cypher(query, params or {})
        except GraphRAGMCPError as exc:
            raise GraphRAGServiceError(exc.error_type, exc.message) from exc

    async def aclose(self) -> None:
        """정리할 장수명 리소스 없음 (stateless per-call SSE 세션). no-op."""
        return None
