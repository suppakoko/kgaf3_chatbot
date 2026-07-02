"""LLM 서비스 — OpenRouter (OpenAI-compatible).

LLM_plan.md §1 권위.
Phase 1: chat_stream / assert_allowed / from_settings / aclose.
Phase 2: chat_json (JSON 응답 강제 + 1회 self-correct 재시도).
Phase 4+: budget tracking (미구현).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import structlog
from openai import AsyncOpenAI
from pydantic import BaseModel

if TYPE_CHECKING:
    from app.config import Settings

log = structlog.get_logger("service.llm")


class LLMError(Exception):
    """LLM 클라이언트 통일 예외."""

    def __init__(self, error_type: str, message: str):
        super().__init__(f"[{error_type}] {message}")
        self.error_type = error_type
        self.message = message


class LLMService:
    """OpenRouter 기반 LLM 서비스.

    - model_id 화이트리스트 검증 (assert_allowed)
    - 스트리밍 응답 제공 (chat_stream)
    - 예산 트래킹은 Phase 4에서 추가
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        default_model: str,
        allowed_models: list[str],
        app_name: str,
        http_referer: str,
    ):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={
                "HTTP-Referer": http_referer,
                "X-Title": app_name,
            },
        )
        self.default_model = default_model
        self._allowed: set[str] = set(allowed_models)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "LLMService":
        """Settings 객체로부터 LLMService 생성."""
        return cls(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            default_model=settings.llm_default_model,
            allowed_models=settings.allowed_models,
            app_name=settings.openrouter_app_name,
            http_referer=settings.openrouter_http_referer,
        )

    def assert_allowed(self, model: str | None) -> str:
        """모델 ID 화이트리스트 검증.

        None 이면 default_model 사용.
        허용 목록에 없으면 LLMError("MODEL_NOT_ALLOWED") 발생.
        """
        m = model or self.default_model
        if m not in self._allowed:
            raise LLMError("MODEL_NOT_ALLOWED", f"{m!r} is not in allowed_models")
        return m

    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 1500,
    ) -> AsyncGenerator[dict, None]:
        """스트리밍 채팅 — dict 청크 비동기 제너레이터.

        Yields:
            {"delta": str}                                            # 진행 청크
            {"done": True, "tokens_in": int|None, "tokens_out": int|None}  # 마지막

        LLM_plan.md §1.3 / §11 참조. routers/chat.py 와의 contract 일치.
        Usage 는 모델별로 누락 가능 — None 폴백.
        """
        validated_model = self.assert_allowed(model)
        log.debug(
            "llm.chat_stream.start",
            model=validated_model,
            n_messages=len(messages),
            max_tokens=max_tokens,
        )

        stream = await self.client.chat.completions.create(
            model=validated_model,
            messages=messages,
            stream=True,
            max_tokens=max_tokens,
        )

        chunk = None
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield {"delta": delta}

        # 마지막 chunk 에서 usage 추출 (일부 모델은 usage 누락 — None 폴백)
        usage = getattr(chunk, "usage", None) if chunk is not None else None
        tokens_in = getattr(usage, "prompt_tokens", None)
        tokens_out = getattr(usage, "completion_tokens", None)
        yield {"done": True, "tokens_in": tokens_in, "tokens_out": tokens_out}
        log.debug(
            "llm.chat_stream.done",
            model=validated_model,
            tokens_in=tokens_in,
            tokens_out=getattr(usage, "completion_tokens", None),
        )

    async def chat_json(
        self,
        messages: list[dict],
        model: str | None = None,
        schema: type[BaseModel] | None = None,
        max_tokens: int = 2000,
        max_retries: int = 1,
    ) -> dict[str, Any]:
        """JSON 응답 강제 — 1회 self-correct 재시도.

        전략 (LLM_plan.md §1.3):
        - response_format=json_object 기본 미사용 (모델 호환성 이슈)
        - System prompt 로 JSON 구조 강제 → 코드블록 strip → json.loads → schema validate
        - 첫 시도 실패 시 1회 재시도 (LLM 에 self-correct 지시)
        - 최종 실패 → LLMError("JSON_PARSE_FAILED")

        placeholder API 키 분기:
        - API 키가 'sk-placeholder' 이거나 'echo-fallback' 포함 시 → deterministic fallback dict 반환.

        Returns:
            파싱된 dict. schema 있으면 schema.model_validate(dict).model_dump() 적용.
        """
        # placeholder 키 분기 — 실 LLM 호출 없이 fallback 반환
        api_key_str = self.client.api_key or ""
        if "placeholder" in api_key_str.lower() or api_key_str in ("sk-or-v1-placeholder", "echo"):
            log.warning("llm.chat_json.placeholder_key_fallback")
            return _placeholder_fallback(schema)

        validated_model = self.assert_allowed(model)
        log.debug(
            "llm.chat_json.start",
            model=validated_model,
            n_messages=len(messages),
            max_tokens=max_tokens,
        )

        attempt_messages = list(messages)
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = await self.client.chat.completions.create(
                    model=validated_model,
                    messages=attempt_messages,
                    stream=False,
                    max_tokens=max_tokens,
                )
                raw_text = response.choices[0].message.content or ""
                parsed = _parse_json_response(raw_text)

                if schema is not None:
                    validated = schema.model_validate(parsed)
                    log.debug(
                        "llm.chat_json.done",
                        model=validated_model,
                        attempt=attempt,
                    )
                    return validated.model_dump()

                log.debug(
                    "llm.chat_json.done",
                    model=validated_model,
                    attempt=attempt,
                )
                return parsed

            except (json.JSONDecodeError, ValueError) as exc:
                last_exc = exc
                # raw_text preview — 추출 실패 원인 진단에 필수 (markdown fence,
                # truncation, prose-only 응답 등 LLM 별 quirk 식별)
                raw_preview = (raw_text[:500] + "…") if len(raw_text) > 500 else raw_text  # type: ignore[possibly-undefined]
                log.warning(
                    "llm.chat_json.parse_failed",
                    attempt=attempt,
                    error=str(exc),
                    raw_preview=raw_preview or "(empty)",
                    raw_length=len(raw_text or ""),  # type: ignore[possibly-undefined]
                )
                if attempt < max_retries:
                    # self-correct: 직전 응답을 assistant 메시지로 추가 후 재시도 요청
                    bad_text = ""
                    try:
                        bad_text = response.choices[0].message.content or ""  # type: ignore[possibly-undefined]
                    except Exception:
                        pass
                    attempt_messages = list(messages)
                    if bad_text:
                        attempt_messages.append({"role": "assistant", "content": bad_text})
                    attempt_messages.append({
                        "role": "user",
                        "content": "JSON 형식 위반. 마크다운 펜스 없이 정확한 JSON 스키마만 출력하세요.",
                    })

        raise LLMError("JSON_PARSE_FAILED", f"JSON 파싱 실패 (재시도 소진): {last_exc}")

    async def aclose(self) -> None:
        """OpenAI 클라이언트 정리."""
        await self.client.close()


# ── 모듈 레벨 헬퍼 ────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)
_FENCE_ANY_RE = re.compile(r"```(?:[a-z]*)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _extract_balanced_json(text: str) -> str | None:
    """첫 `{` 부터 brace-counting 으로 균형 잡힌 `}` 까지 추출.

    문자열 리터럴 내부 `{` `}` 는 depth 카운팅에서 제외 (escape 처리 포함).
    LLM 응답에 앞뒤 prose 가 있어도 첫 정상 JSON object 만 정확히 잘라냄.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _json_fixup_variants(s: str) -> list[str]:
    """흔한 LLM JSON 실수 fixup variants 생성 (원본 + 보정본).

    - trailing comma 제거 (`, }` `, ]`)
    - smart quote → straight quote (U+201C/D, U+2018/9)
    중복 제거 후 시도 순서대로 반환.
    """
    out: list[str] = [s]

    fixed_comma = _TRAILING_COMMA_RE.sub(r"\1", s)
    if fixed_comma != s:
        out.append(fixed_comma)

    fixed_quotes = (
        s.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    if fixed_quotes != s:
        out.append(fixed_quotes)
        # comma + quote 둘 다 적용한 변형도 시도
        both = _TRAILING_COMMA_RE.sub(r"\1", fixed_quotes)
        if both != fixed_quotes:
            out.append(both)

    return out


def _parse_json_response(text: str) -> dict[str, Any]:
    """LLM 응답에서 JSON 추출 (강화 버전 v2).

    추출 candidate 우선순위:
        1. ```json ... ``` / ``` ... ``` 펜스 (모든 매치 — 여러 펜스 가능)
        2. brace-balanced 추출 (앞뒤 prose 가 섞여도 정확히 첫 JSON object)
        3. greedy `\\{.*\\}` (legacy fallback)
        4. raw stripped text

    각 candidate 마다 trailing-comma / smart-quote fixup variant 도 시도.
    모두 실패 시 마지막 JSONDecodeError 를 raise (호출자가 raw_preview 로깅).
    빈 응답은 즉시 JSONDecodeError.
    """
    stripped = (text or "").strip()
    if not stripped:
        raise json.JSONDecodeError("empty response", text or "", 0)

    candidates: list[str] = []

    # 1. 펜스 매치 (전체 + 부분, 모든 occurrence)
    full_fence = _FENCE_RE.match(stripped)
    if full_fence:
        candidates.append(full_fence.group(1).strip())
    for m in _FENCE_ANY_RE.finditer(stripped):
        c = m.group(1).strip()
        if c and c not in candidates:
            candidates.append(c)

    # 2. brace-balanced (가장 정확)
    balanced = _extract_balanced_json(stripped)
    if balanced and balanced not in candidates:
        candidates.append(balanced)

    # 3. greedy {.*} (legacy)
    m_greedy = _JSON_OBJ_RE.search(stripped)
    if m_greedy:
        c = m_greedy.group(0).strip()
        if c not in candidates:
            candidates.append(c)

    # 4. raw stripped
    if stripped not in candidates:
        candidates.append(stripped)

    last_exc: json.JSONDecodeError | None = None
    for cand in candidates:
        if not cand:
            continue
        for variant in _json_fixup_variants(cand):
            try:
                parsed = json.loads(variant)
            except json.JSONDecodeError as e:
                last_exc = e
                continue
            if isinstance(parsed, dict):
                return parsed  # type: ignore[return-value]
            # JSON 이 array/scalar 면 거부 — schema 가 dict 만 허용

    if last_exc is None:
        last_exc = json.JSONDecodeError(
            "no JSON object found in response", stripped[:200], 0
        )
    raise last_exc


def _placeholder_fallback(schema: type[BaseModel] | None) -> dict[str, Any]:
    """API 키 placeholder 시 결정론적 fallback dict 반환.

    schema 가 ScreeningSummary 이면 경고가 포함된 요약 구조 반환.
    그 외 schema 는 빈 dict.
    """
    # import 여기서 (순환 참조 방지)
    try:
        from app.models.llm import ScreeningSummary as SS  # noqa: N812
        if schema is SS:
            return SS(
                headline="[주의] OpenRouter API 키가 설정되지 않아 LLM 해석을 실행하지 못했습니다.",
                highlights=[
                    "API 키 미설정으로 자동 요약 생성 불가",
                    ".env 파일의 OPENROUTER_API_KEY 를 실제 키로 교체하세요",
                    "랭킹 결과는 DB 에 저장되어 있으며 /api/screening/{job_id}/results 로 조회 가능",
                ],
                table_md="| rank | ligand | 참고 |\n|---|---|---|\n| - | - | API 키 필요 |",
                caveats=[
                    "OpenMM e_interaction 은 vacuum NoCutoff 기반이라 동일 target 내 *상대 랭킹* 만 의미가 있습니다. 타겟 간 절대 비교 부적합.",
                    "Smina docking score (Vinardo) 는 score-only 모드입니다 — pose 재탐색 없이 현재 좌표를 그대로 평가하므로 도킹 친화도와 다를 수 있습니다.",
                    "ipTM ≥ 0.55 통과 ligand 만 composite score 에 포함됩니다. 실패 리간드는 별도 검토 필요.",
                    "[fallback] LLM API 키 미설정 — 이 요약은 자동 생성된 것이 아닙니다.",
                ],
                next_actions=[
                    ".env 에 OPENROUTER_API_KEY 를 설정하고 서비스를 재시작하세요.",
                    "랭킹 결과를 직접 /api/screening/{job_id}/results 로 확인하세요.",
                ],
            ).model_dump()
    except ImportError:
        pass

    return {
        "headline": "[fallback] LLM API 키 미설정",
        "highlights": ["API 키 placeholder — LLM 호출 생략"],
        "table_md": "",
        "caveats": ["[fallback] OPENROUTER_API_KEY 미설정"],
        "next_actions": [".env 파일에 실제 API 키를 설정하세요."],
    }
