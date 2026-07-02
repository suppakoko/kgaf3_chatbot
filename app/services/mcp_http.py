"""MCP HTTP 공통 베이스 클라이언트 (JSON-RPC 2.0 over Streamable HTTP).

mcp_integration.md §2.1 권위.
afmm_chat 은 단방향 request/response 호출만 사용 (server-initiated 알림 X).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog

log = structlog.get_logger("service.mcp")


# ── 예외 ─────────────────────────────────────────────────────────────────────

class MCPError(Exception):
    """MCP 클라이언트 통일 예외. error_type 으로 호출자가 분기.

    Types:
        TIMEOUT       - httpx.TimeoutException (재시도 가능)
        TRANSPORT     - httpx.TransportError (재시도 가능)
        TOOL_5XX      - HTTP 5xx 서버 에러
        RPC_ERROR     - JSON-RPC error 필드 존재
        BAD_REQUEST   - HTTP 4xx
        BAD_ARGS      - jsonschema ValidationError (재시도 불가)
        TOOL_ERROR    - isError=true 응답 (재시도 불가)
        UNKNOWN_TOOL  - tool_index 에 없는 도구 이름
    """

    def __init__(self, error_type: str, message: str, *, code: int | None = None):
        super().__init__(f"[{error_type}] {message}")
        self.error_type = error_type
        self.message = message
        self.code = code


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _flatten_text(res: dict) -> str:
    """isError 응답의 content 배열을 텍스트로 평탄화."""
    parts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
    return " ".join(parts) or "(no error message)"


def _parse_sse_response(text: str) -> dict:
    """Server-Sent Events 응답에서 JSON-RPC body 추출.

    SSE 형식 (MCP streamable-http):
        event: message
        data: {"jsonrpc":"2.0","id":1,"result":{...}}

    또는 단일 data 라인. 마지막 data 라인의 JSON 만 사용.
    """
    last_json: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    last_json = parsed
            except json.JSONDecodeError:
                continue
    if not last_json:
        # fallback: try whole-body JSON parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
    return last_json


def _unwrap_content(res: dict) -> Any:
    """MCP tools/call 응답 content 언래핑.

    - 단일 text content + JSON 파싱 가능 → dict 반환
    - 단일 text content + plain text  → str 반환
    - 여러 content                    → list 반환
    """
    contents = res.get("content", [])
    if len(contents) == 1 and contents[0].get("type") == "text":
        text = contents[0]["text"]
        try:
            return json.loads(text)
        except Exception:
            return text
    return contents


# ── 클라이언트 ────────────────────────────────────────────────────────────────

class MCPHttpClient:
    """Streamable-HTTP MCP client (JSON-RPC 2.0).

    base_url 은 /mcp 경로를 포함한 전체 엔드포인트 (예: http://127.0.0.1:8000/mcp).
    httpx.AsyncClient(base_url=...) + post("", ...) 로 정확한 경로에 POST.
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str = "",
        timeout: float = 600.0,
        name: str = "",
    ):
        # streamable-http MCP는 path 끝에 `/`가 필요할 수 있음 (FastMCP 기본 mount).
        # rstrip 제거 → URL을 그대로 사용. 307 redirect 발생 시 follow_redirects 가 처리.
        self.base_url = base_url
        self.name = name or base_url
        self.timeout = timeout

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            http2=False,
            follow_redirects=True,  # 307 (POST /mcp → /mcp/) 자동 follow
        )
        self._next_id: int = 0
        self._tool_index: dict[str, dict] = {}
        # MCP streamable-http: initialize 응답에 mcp-session-id 헤더가 들어오고
        # 후속 RPC 는 이 헤더를 포함해야 한다. 캡처 + 자동 forward.
        self._session_id: str | None = None

        # ── 자동 재연결 상태 ──────────────────────────────────────────────────
        # connect() 성공 여부. False 이면 _rpc() 진입 시 lazy reconnect 시도.
        self._connected: bool = False
        # 동시 재연결 race 방지 (두 RPC 가 동시에 connect() 를 중복 호출하지 않도록)
        self._reconnect_lock: asyncio.Lock = asyncio.Lock()
        # ── ping 캐시 ──────────────────────────────────────────────────────────
        # 시스템 상태바가 5초 주기로 /api/system/info → ping() 을 호출하면
        # MCP 서버 (FastMCP) 의 idle session 이 만료되는 타이밍에 transport_error +
        # reconnect 로그가 지속적으로 발생한다. ping 결과를 짧은 TTL 로 캐시해
        # 실제 RPC 호출 빈도를 낮춤. TTL 동안은 캐시 hit, 실패시 즉시 무효화.
        self._ping_cache_ok: bool | None = None
        self._ping_cache_at: float = 0.0
        self._ping_ttl: float = 30.0  # 30초 캐시

    # ── 라이프사이클 ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """initialize RPC + tools/list 캐시.

        성공 시 _connected = True 로 세팅.
        실패해도 예외를 re-raise — 호출자(lifespan / _ensure_connected)가 처리.
        """
        # 재연결 시 이전 세션 ID 초기화
        self._session_id = None
        self._connected = False

        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "afmm_chat", "version": "0.1.0"},
                "capabilities": {},
            },
        )
        # initialize 성공 → 세션 ID 확보됨. _refresh_tools 의 _rpc("tools/list") 가
        # 진입부에서 _ensure_connected 재귀를 트리거하지 않도록 먼저 True 로 세팅.
        # _refresh_tools 가 실패하면 except 에서 다시 False 로 되돌림.
        self._connected = True
        try:
            await self._refresh_tools()
        except Exception:
            self._connected = False
            raise

    async def aclose(self) -> None:
        """httpx 클라이언트 정리."""
        await self._client.aclose()

    # ── 도구 관리 ─────────────────────────────────────────────────────────────

    async def _refresh_tools(self) -> None:
        """tools/list 결과를 _tool_index 에 캐시."""
        res = await self._rpc("tools/list", {})
        self._tool_index = {t["name"]: t for t in res.get("tools", [])}
        log.info("mcp.tools_loaded", server=self.name, count=len(self._tool_index))

    async def list_tools(self) -> list[dict]:
        """캐시된 도구 목록 반환 (connect() 이후 유효)."""
        return list(self._tool_index.values())

    # ── 자동 재연결 ───────────────────────────────────────────────────────────

    async def _ensure_connected(self) -> None:
        """_connected=False 일 때 재연결을 시도한다. Lock 으로 race 방지.

        backoff: 1s, 2s, 4s (최대 3회 시도).
        모든 시도 실패 시 마지막 예외를 raise.
        """
        async with self._reconnect_lock:
            # 다른 coroutine 이 lock 을 보유하고 있었던 사이에 재연결 완료됐으면 스킵
            if self._connected:
                return

            last_exc: Exception | None = None
            for attempt in range(1, 4):  # 최대 3 회
                wait = 2 ** (attempt - 1)  # 1s, 2s, 4s
                log.info(
                    "mcp.reconnect.attempt",
                    server=self.name,
                    attempt=attempt,
                    wait_secs=wait,
                )
                try:
                    await self.connect()
                    log.info("mcp.reconnect.success", server=self.name, attempt=attempt)
                    return
                except Exception as exc:
                    last_exc = exc
                    log.warning(
                        "mcp.reconnect.failed",
                        server=self.name,
                        attempt=attempt,
                        error=str(exc),
                    )
                    if attempt < 3:
                        await asyncio.sleep(wait)

            # 3 회 모두 실패
            log.error("mcp.reconnect.exhausted", server=self.name)
            raise last_exc  # type: ignore[misc]

    # ── 핑 ───────────────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """5초 타임아웃 tools/list 헬스체크 — 30초 TTL 캐시.

        _connected=False 이면 먼저 _ensure_connected() 로 재연결을 시도한다.
        재연결 자체가 실패하면 False 반환. 캐시 hit 시 RPC 를 건너뛴다.
        실제 도구 호출에서 실패하면 _connected 가 False 로 떨어져
        다음 ping 호출이 캐시 무시하고 재검증한다.
        """
        import time as _time
        now = _time.monotonic()
        if (
            self._ping_cache_ok is not None
            and self._connected
            and (now - self._ping_cache_at) < self._ping_ttl
        ):
            return self._ping_cache_ok
        try:
            if not self._connected:
                await self._ensure_connected()
            await self._rpc("tools/list", {}, timeout=5.0)
            self._ping_cache_ok = True
        except Exception:
            self._ping_cache_ok = False
        self._ping_cache_at = now
        return self._ping_cache_ok

    # ── 도구 호출 ─────────────────────────────────────────────────────────────

    async def call(self, tool: str, args: dict, *, timeout: float | None = None) -> Any:
        """MCP 도구 호출 (jsonschema 사전 검증 포함).

        Returns:
            _unwrap_content 로 언래핑된 결과 (dict | str | list)
        Raises:
            MCPError: UNKNOWN_TOOL / BAD_ARGS / TOOL_5XX / RPC_ERROR / TOOL_ERROR / TIMEOUT / TRANSPORT
        """
        if tool not in self._tool_index:
            raise MCPError("UNKNOWN_TOOL", f"{self.name}: {tool!r}")

        # jsonschema 사전 검증 (inputSchema 캐시 활용)
        schema = self._tool_index[tool].get("inputSchema")
        if schema:
            try:
                from jsonschema import validate, ValidationError as JSValidationError
                validate(args, schema)
            except JSValidationError as e:
                raise MCPError("BAD_ARGS", str(e.message)) from e

        result = await self._rpc(
            "tools/call", {"name": tool, "arguments": args}, timeout=timeout
        )

        if result.get("isError"):
            raise MCPError("TOOL_ERROR", _flatten_text(result))

        return _unwrap_content(result)

    # ── 내부 RPC ──────────────────────────────────────────────────────────────

    async def _rpc(
        self,
        method: str,
        params: dict,
        *,
        timeout: float | None = None,
        _reconnect_attempted: bool = False,
    ) -> Any:
        """JSON-RPC 2.0 POST 요청.

        streamable-http MCP 세션 처리:
        - initialize 응답 헤더의 mcp-session-id 캡처 → self._session_id
        - 후속 RPC 는 mcp-session-id 헤더 자동 포함

        자동 재연결:
        - _connected=False 이면 호출 전 _ensure_connected() 로 lazy reconnect
        - TransportError 또는 "Missing session ID" 포함 4xx 응답 시
          세션 무효화 → 1 회 재연결 후 원 요청 재시도
          (initialize 자신에 대해선 재시도 루프 방지를 위해 스킵)
        """
        # initialize 가 아닌데 연결이 안 된 상태면 먼저 재연결 시도 (lazy reconnect)
        if method != "initialize" and not self._connected and not _reconnect_attempted:
            log.info("mcp.lazy_reconnect", server=self.name, trigger="not_connected", method=method)
            await self._ensure_connected()

        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        log.debug("mcp.rpc", server=self.name, method=method, id=self._next_id)

        # 세션 ID 가 있으면 헤더에 포함
        request_headers: dict[str, str] = {}
        if self._session_id is not None:
            request_headers["mcp-session-id"] = self._session_id

        try:
            r = await self._client.post(
                "",
                json=payload,
                timeout=timeout or self.timeout,
                headers=request_headers if request_headers else None,
            )
        except httpx.TimeoutException as e:
            raise MCPError(
                "TIMEOUT",
                f"{self.name} {method} timed out after {timeout or self.timeout}s",
            ) from e
        except httpx.TransportError as e:
            # TransportError: 서버 재시작 / IPv6→IPv4 fallback 실패 / FastMCP idle session
            # 만료 등. 첫 시도면 한 번 재연결 후 재시도 — 정상 자동복구 경로이므로
            # info 로 기록 (이전엔 warning 이었지만 idle session 만료가 잦아 noise).
            # 재시도 후에도 실패하면 raise 가 발생해 호출자가 처리 (그건 진짜 문제).
            if method != "initialize" and not _reconnect_attempted:
                log.info(
                    "mcp.transport_error.reconnecting",
                    server=self.name,
                    method=method,
                    error=str(e),
                )
                self._connected = False
                self._session_id = None
                # ping 캐시 무효화 (다음 ping 호출이 실값 검증하도록)
                self._ping_cache_ok = None
                await self._ensure_connected()
                return await self._rpc(method, params, timeout=timeout, _reconnect_attempted=True)
            raise MCPError("TRANSPORT", f"{self.name} transport error: {e}") from e

        # initialize 응답에서 세션 ID 캡처 (대소문자 무관, httpx 는 소문자 정규화)
        if method == "initialize":
            sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
                log.debug("mcp.session_id_captured", server=self.name, session_id=sid)

        # 5xx → TOOL_5XX (재시도 신호), 4xx → BAD_REQUEST
        if 500 <= r.status_code < 600:
            raise MCPError(
                "TOOL_5XX",
                f"{self.name} {method} server error {r.status_code}",
                code=r.status_code,
            )
        if 400 <= r.status_code < 500:
            body_preview = r.text[:500]
            # "Missing session ID" 포함 4xx: 세션 좀비 상태 → 재연결 후 재시도
            if (
                method != "initialize"
                and not _reconnect_attempted
                and "missing session" in body_preview.lower()
            ):
                log.warning(
                    "mcp.missing_session_id.reconnecting",
                    server=self.name,
                    method=method,
                    status_code=r.status_code,
                    body=body_preview,
                )
                self._connected = False
                self._session_id = None
                await self._ensure_connected()
                return await self._rpc(method, params, timeout=timeout, _reconnect_attempted=True)
            raise MCPError(
                "BAD_REQUEST",
                f"{self.name} {method} client error {r.status_code}: {body_preview}",
                code=r.status_code,
            )

        # MCP streamable-http 는 응답을 SSE (text/event-stream) 또는 plain JSON 으로 반환.
        # SSE 의 경우 'data: <json>\n\n' 형태이므로 JSON 파싱 전에 추출 필요.
        ctype = r.headers.get("content-type", "").lower()
        if "text/event-stream" in ctype:
            body = _parse_sse_response(r.text)
        else:
            try:
                body = r.json()
            except Exception as e:
                raise MCPError(
                    "BAD_REQUEST",
                    f"{self.name} {method} non-JSON response: {r.text[:200]}",
                ) from e

        if "error" in body:
            err = body["error"]
            raise MCPError(
                "RPC_ERROR",
                err.get("message", "rpc error"),
                code=err.get("code"),
            )

        return body.get("result", {})
