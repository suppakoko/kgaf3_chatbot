"""채팅 WebSocket 라우터.

WS /ws/chat/{session_id}

Envelope (양방향):
    {"type": "...", "request_id": "ulid", "payload": {...}}

지원 타입:
    uplink:  chat.user_message
    downlink:
        chat.assistant_chunk, chat.assistant_done, error,
        screening.submitted (자동 도킹 시작 알림),
        screening.progress (stage 변화 / ligand 진행),
        screening.complete (최종 랭킹),
        screening.error.

자연어 도킹 트리거:
    user_message 에 FASTA + SMILES + docking 키워드 존재 시
    LLM 호출 대신 즉시 library upload + screening 시작 +
    파이프라인 stage 이벤트를 채팅 WS 로 forward (event_bus 경유).

interface-contracts §2 권위.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiosqlite
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.models.ws import WSMessage, WSError
from app.routers._deps import get_llm
from app.services.chat_intent import (
    DockingIntent,
    build_smi_blob,
    detect_docking_intent,
    diagnose_docking_intent,
)
from app.services.event_bus import bus as event_bus
from app.services.graphrag_service import GraphRAGService, GraphRAGServiceError
from app.services.history_service import HistoryService
from app.services.library_service import LibraryService
from app.services.screening_service import (
    ScreeningJobContext,
    ScreeningService,
)
from app.services.target_service import TargetInput
from app.services.llm_service import LLMService
from app.utils.ids import ulid_str
from app.utils.log import get_logger

log = get_logger("router.chat")

router = APIRouter(tags=["chat"])


def _build_msg(type_: str, payload: dict[str, Any], request_id: str | None = None) -> str:
    """JSON 직렬화된 downlink 메시지 생성."""
    msg = {
        "type": type_,
        "request_id": request_id or ulid_str(),
        "payload": payload,
    }
    return json.dumps(msg, ensure_ascii=False)


async def _send_error(
    ws: WebSocket,
    code: str,
    message: str,
    request_id: str | None = None,
) -> None:
    """클라이언트에 error 메시지 전송."""
    err = WSError(code=code, message=message, request_id=request_id)
    await ws.send_text(
        json.dumps(err.model_dump(), ensure_ascii=False)
    )


async def _handle_user_message(
    ws: WebSocket,
    payload: dict[str, Any],
    request_id: str,
    llm: LLMService,
    history: HistoryService,
    session_id: str,
) -> None:
    """chat.user_message 처리 — LLM 스트림 or echo fallback. 양 메시지 모두 DB 영속화.

    payload.mode (선택, 기본 "afmm"):
        "afmm"     — 기존 AF3+OpenMM 스크리닝 chatbot 동작 (자연어 도킹 의도 감지 포함)
        "graphrag" — GraphRAG 모드: KIST NPI Neo4j 지식그래프에 자연어 질의
    """
    text: str = payload.get("text", "")
    model: str = payload.get("model", settings.llm_default_model)
    mode: str = (payload.get("mode") or "afmm").lower()

    if not text:
        await _send_error(ws, "INVALID_INPUT", "text field is required", request_id)
        return

    # 세션 upsert + user 메시지 저장 (사이드바 / 메시지 복원용)
    try:
        await history.touch_session(session_id, model)
        await history.save_message(
            session_id, "user", text, meta={"request_id": request_id, "mode": mode},
        )
    except Exception as exc:
        log.warning("chat.history.save_user_failed", exc_info=exc, session_id=session_id)

    # ── GraphRAG 모드: Neo4j 지식그래프 자연어 질의 ───────────────────────
    if mode == "graphrag":
        graphrag: GraphRAGService = ws.app.state.graphrag
        await _handle_graphrag_query(
            ws=ws,
            request_id=request_id,
            session_id=session_id,
            text=text,
            graphrag=graphrag,
            history=history,
            model_hint=model,
        )
        return

    # ── 자연어 도킹 의도 감지: FASTA + SMILES + docking 키워드 ─────────────
    intent = detect_docking_intent(text)
    if intent is not None:
        await _start_chat_screening(
            ws=ws,
            request_id=request_id,
            session_id=session_id,
            intent=intent,
            history=history,
            library=ws.app.state.library,
            screening=ws.app.state.screening,
        )
        return

    # ── 도킹 의도 부분 매칭 진단: 키워드는 있는데 시퀀스 또는 SMILES 가
    # 부족한 경우 → 사용자에게 "무엇이 빠졌는지" 정확히 안내 (alphafold
    # 자체가 실행되지 않는 가장 흔한 원인. 침묵 폴백 대신 명시적 안내).
    diag = diagnose_docking_intent(text)
    if diag.has_intent_keyword and not (diag.has_target_sequence and diag.has_ligands):
        guide = _build_docking_guide_message(diag)
        await ws.send_text(_build_msg(
            "chat.assistant_chunk", {"delta": guide}, request_id,
        ))
        await ws.send_text(_build_msg(
            "chat.assistant_done",
            {"model": "system/intent_guide", "tokens_in": 0, "tokens_out": 0},
            request_id,
        ))
        try:
            await history.save_message(
                session_id, "assistant", guide,
                meta={"request_id": request_id, "mode": "afmm",
                      "intent_miss": True,
                      "has_seq": diag.has_target_sequence,
                      "has_lig": diag.has_ligands,
                      "lig_count": diag.found_ligand_count},
            )
        except Exception:
            pass
        return

    # placeholder 형식 ("sk-or-..." 정확 일치) 도 echo 모드로 분기
    api_key = settings.openrouter_api_key.strip()
    has_key = bool(api_key) and api_key != "sk-or-..." and len(api_key) >= 20

    assistant_text_parts: list[str] = []

    if not has_key:
        # Phase 1 fallback: echo 응답
        log.info("chat.echo_fallback", session="ws", request_id=request_id)
        echo = f"echo: {text}"
        assistant_text_parts.append(echo)
        await ws.send_text(
            _build_msg(
                "chat.assistant_chunk",
                {"delta": echo},
                request_id,
            )
        )
        await ws.send_text(
            _build_msg(
                "chat.assistant_done",
                {"model": model, "tokens_in": 0, "tokens_out": 0},
                request_id,
            )
        )
        try:
            await history.save_message(
                session_id, "assistant", "".join(assistant_text_parts),
                meta={"request_id": request_id, "model": model, "mode": "echo"},
            )
        except Exception as exc:
            log.warning("chat.history.save_assistant_failed", exc_info=exc)
        return

    # LLM 스트림 경로
    messages = [{"role": "user", "content": text}]
    tokens_in = 0
    tokens_out = 0

    try:
        async for chunk in llm.chat_stream(messages=messages, model=model):
            # chunk 는 {"delta": str} 또는 {"done": True, "tokens_in": int, "tokens_out": int}
            if chunk.get("done"):
                tokens_in = chunk.get("tokens_in", 0)
                tokens_out = chunk.get("tokens_out", 0)
            else:
                delta = chunk.get("delta", "")
                if delta:
                    assistant_text_parts.append(delta)
                await ws.send_text(
                    _build_msg(
                        "chat.assistant_chunk",
                        {"delta": delta},
                        request_id,
                    )
                )
    except Exception as exc:
        log.error("chat.llm_stream_error", exc_info=exc, request_id=request_id)
        await _send_error(ws, "OPENROUTER_FAILED", "LLM stream error", request_id)
        return

    await ws.send_text(
        _build_msg(
            "chat.assistant_done",
            {"model": model, "tokens_in": tokens_in, "tokens_out": tokens_out},
            request_id,
        )
    )

    try:
        await history.save_message(
            session_id, "assistant", "".join(assistant_text_parts),
            meta={
                "request_id": request_id,
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
        )
    except Exception as exc:
        log.warning("chat.history.save_assistant_failed", exc_info=exc)


def _build_docking_guide_message(diag: Any) -> str:
    """도킹 의도 부분 매칭 시 사용자에게 무엇이 빠졌는지 markdown 안내.

    diag: DockingIntentMiss
    """
    missing: list[str] = []
    if not diag.has_target_sequence:
        missing.append(
            "❌ **단백질 시퀀스가 없습니다** — FASTA 헤더(`>이름`) 와 "
            "30자 이상의 아미노산 단문자 시퀀스(`MSTGD...`) 를 함께 입력하세요."
        )
    else:
        missing.append(f"✅ 단백질 시퀀스 검출 ({diag.detected_seq_len}aa)")

    if not diag.has_ligands:
        missing.append(
            "❌ **화합물 SMILES 가 없습니다** — 한 줄에 하나씩 SMILES 와 "
            "(선택) 이름을 공백으로 구분해 입력하세요. 예: "
            "`C[C@H](C1CCC(CC1)C(=O)NC2=CC=NC=C2)N Y-27632`"
        )
    else:
        missing.append(f"✅ SMILES 검출 ({diag.found_ligand_count}개)")

    body = (
        "## 🛑 도킹 시작 안내 — 입력 정보가 부족합니다\n\n"
        "도킹 키워드(`도킹` / `순위` / `docking` …)는 감지됐지만, "
        "AlphaFold3 + OpenMM 파이프라인을 시작하려면 **다음 3 가지가 모두** 필요합니다.\n\n"
        + "\n".join(f"- {m}" for m in missing)
        + "\n\n---\n\n### 📋 올바른 입력 예시\n\n"
        "```\n"
        ">ROCK1_HUMAN_1-415\n"
        "MSTGDSFETRFEKMDNLLRDPKSEVNSDCLLDGLDALVYDLDFPALRKNKNIDNFLSRYK\n"
        "DTINKIRDLRMKAEDYEVVKVIGRGAFGEVQLVRHKSTRKVYAMKLLSKFEMIKRSDSAF\n"
        "... (전체 시퀀스 30자 이상) ...\n"
        "\n"
        "CN(C)CCOC1=C(C=CC(=C1)C2=CNN=C2)NC(=O)[C@H]3CC4=C(C=CC(=C4)OC)OC3 Chroman_1\n"
        "C[C@H]1CNCCCN1S(=O)(=O)C2=CC=CC3=C2C(=CN=C3)C H1152\n"
        "C[C@H](C1CCC(CC1)C(=O)NC2=CC=NC=C2)N Y-27632\n"
        "\n"
        "위 단백질에 화합물 3개를 docking 해서 순위를 매겨주세요\n"
        "```\n\n"
        "💡 라이브러리 업로드 버튼으로 SDF/SMI 파일을 첨부하는 것도 가능합니다."
    )
    return body


async def _send_graphrag_stage(
    ws: WebSocket,
    request_id: str,
    *,
    step: str,
    status: str,
    **extra: Any,
) -> None:
    """graphrag.stage 메시지 송신.

    Args:
        step: cypher_gen | neo4j_exec | answer_synth | complete
        status: running | done | warning | error | skipped
        extra: duration_s, row_count, cypher, sample, error, message, hint, tokens_in, tokens_out
    """
    payload = {"step": step, "status": status, **extra}
    await ws.send_text(_build_msg("graphrag.stage", payload, request_id))


def _graphrag_error_hint(error_type: str) -> str:
    """MCP 오류 유형별 사용자 힌트."""
    return {
        "DISABLED": "GraphRAG 모드가 비활성화되어 있습니다 (.env: GRAPHRAG_ENABLED=true).",
        "TRANSPORT": (
            "graphrag-mcp 컨테이너(:8893)에 연결할 수 없습니다. "
            "`docker compose --profile graphrag up -d` 로 기동됐는지, "
            "GRAPHRAG_MCP_URL 이 올바른지 확인하세요."
        ),
        "TIMEOUT": (
            "MCP 질의가 시간 초과했습니다. graphrag-mcp/neo4j 컨테이너 상태와 "
            "OpenRouter 응답 지연을 확인하세요."
        ),
        "QUERY_FAILED": (
            "graphrag_query 실행 실패 — MCP 서버 .env 의 OPENROUTER_API_KEY 또는 "
            "Neo4j 연결 상태를 확인하세요."
        ),
    }.get(
        error_type,
        "graphrag-mcp 서버 로그(`docker logs graphrag-mcp`)를 확인하세요.",
    )


async def _handle_graphrag_query(
    ws: WebSocket,
    request_id: str,
    session_id: str,
    text: str,
    graphrag: GraphRAGService,
    history: HistoryService,
    model_hint: str,
) -> None:
    """GraphRAG 자연어 질의 — 번들 graphrag-mcp(SSE)에 원샷 질의.

    MCP 서버의 ``graphrag_query`` 는 자연어→Cypher→실행→답변을 한 번에 수행하므로
    (개별 generate/synthesize 도구 없음), 여기서는 1회 호출 후 반환 메타데이터
    (cypher, row_count, rows_preview, token_usage)를 프론트의 기존 진행 카드
    단계(cypher_gen/neo4j_exec/answer_synth/complete)로 **되매핑**한다.

    Stage WS 메시지 흐름 (프론트 계약 유지):
        graphrag.stage step=cypher_gen   status=running → done | error
        graphrag.stage step=neo4j_exec   status=done | warning(0 rows)
        graphrag.stage step=answer_synth status=done
        graphrag.stage step=complete     status=ok | empty | <error_type>
        chat.assistant_chunk { delta: final markdown answer }
        chat.assistant_done  { tokens_in, tokens_out }
    """
    import time

    if not graphrag.enabled:
        await _send_error(
            ws,
            "GRAPHRAG_DISABLED",
            "GraphRAG 모드가 비활성화되어 있습니다. .env 의 GRAPHRAG_ENABLED 를 확인하세요.",
            request_id,
        )
        return

    t_start = time.perf_counter()

    # 원샷 MCP 질의는 자연어→Cypher→실행→답변을 서버가 한 번에 처리한다.
    # 프론트에는 먼저 "진행 중" 한 줄만 띄우고, 완료 후 메타로 단계를 채운다.
    await _send_graphrag_stage(
        ws, request_id, step="cypher_gen", status="running",
        message="GraphRAG MCP 서버에 질의 중 (자연어 → Cypher → 실행 → 답변)...",
    )

    try:
        answer, meta = await graphrag.query(text)
    except GraphRAGServiceError as exc:
        dt = time.perf_counter() - t_start
        await _send_graphrag_stage(
            ws, request_id, step="cypher_gen", status="error",
            duration_s=round(dt, 2),
            error=exc.message,
            error_type=exc.error_type,
            hint=_graphrag_error_hint(exc.error_type),
        )
        # complete status 는 프론트 FINAL 맵이 아는 값이어야 에러 카드로 렌더된다
        # (ok/empty/*_failed/error). error_type 자체는 이력 meta 로만 남긴다.
        await _send_graphrag_stage(
            ws, request_id, step="complete", status="error",
            duration_s=round(dt, 2),
        )
        body = (
            "## ❌ GraphRAG 처리 실패\n\n"
            f"- {exc.message}\n\n"
            f"💡 {_graphrag_error_hint(exc.error_type)}"
        )
        await ws.send_text(_build_msg("chat.assistant_chunk", {"delta": body}, request_id))
        await ws.send_text(_build_msg(
            "chat.assistant_done",
            {"model": f"graphrag:{graphrag.openrouter_model}", "tokens_in": 0, "tokens_out": 0},
            request_id,
        ))
        try:
            await history.save_message(
                session_id, "assistant", body,
                meta={"request_id": request_id, "mode": "graphrag",
                      "graphrag_status": exc.error_type.lower(),
                      "error": exc.message, "duration_s": round(dt, 2)},
            )
        except Exception:
            pass
        return

    # ── 성공 — 메타를 단계 카드로 되매핑 ─────────────────────────────────────
    total_dt = time.perf_counter() - t_start
    cypher: str = str(meta.get("cypher") or "")
    row_count: int = int(meta.get("row_count") or 0)
    rows_preview: list[dict[str, Any]] = meta.get("rows_preview") or []
    usage = meta.get("token_usage") or {}
    tokens = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }

    # 1) Cypher 생성 결과
    if cypher:
        await _send_graphrag_stage(
            ws, request_id, step="cypher_gen", status="done",
            duration_s=round(total_dt, 2), cypher=cypher,
        )
    else:
        await _send_graphrag_stage(
            ws, request_id, step="cypher_gen", status="warning",
            duration_s=round(total_dt, 2),
            message="MCP 서버가 Cypher 를 반환하지 않았습니다 (질문을 더 구체적으로).",
        )

    # 2) Neo4j 실행 결과 (rows_preview 로 샘플 표시)
    if row_count > 0:
        sample = []
        for r in rows_preview[:3]:
            try:
                s = {
                    k: (str(v)[:120] if not isinstance(v, (int, float, bool, type(None))) else v)
                    for k, v in r.items()
                }
                sample.append(s)
            except Exception:
                sample.append({"_raw": str(r)[:200]})
        await _send_graphrag_stage(
            ws, request_id, step="neo4j_exec", status="done",
            row_count=row_count, sample=sample,
        )
        final_status = "ok"
    else:
        await _send_graphrag_stage(
            ws, request_id, step="neo4j_exec", status="warning",
            row_count=0,
            message="Cypher 는 실행됐지만 매칭 데이터가 없습니다 (필터가 너무 좁거나 라벨 불일치).",
            hint="질병/단백질 이름을 영어로 시도 (예: 'glaucoma', 'ROCK1'). Open Targets 는 EFO/MONDO 영문 라벨.",
        )
        final_status = "empty"

    # 3) 답변 합성 결과
    if answer:
        await _send_graphrag_stage(
            ws, request_id, step="answer_synth", status="done",
            tokens_in=tokens["input_tokens"], tokens_out=tokens["output_tokens"],
        )

    # 4) 완료
    await _send_graphrag_stage(
        ws, request_id, step="complete", status=final_status,
        duration_s=round(total_dt, 2),
        cypher=cypher, row_count=row_count,
        tokens_in=tokens["input_tokens"], tokens_out=tokens["output_tokens"],
    )

    # 최종 답변 본문: MCP 서버가 0 row 여도 LLM 답변을 합성해 반환하므로 보통 answer 존재.
    if answer:
        body = answer
    elif final_status == "empty":
        body = (
            "## ℹ️ 매칭 결과 없음\n\n"
            "쿼리는 정상 실행되었지만 그래프에서 일치하는 데이터를 찾지 못했습니다.\n\n"
            "**시도해볼 만한 것:**\n"
            "- 질병/단백질 이름을 영어로 시도 (예: `glaucoma`, `Alzheimer disease`, `ROCK1`)\n"
            "- 더 일반적인 카테고리로 확장 (예: `top natural products for kinase targets`)\n"
            "- 진행 카드에서 생성된 Cypher 를 확인하고 필터 조건을 점검\n"
        )
    else:
        body = "## ⚠️ 답변이 비어 있습니다\n\n자세한 정보는 진행 카드를 확인하세요."

    await ws.send_text(_build_msg(
        "chat.assistant_chunk", {"delta": body}, request_id,
    ))
    await ws.send_text(_build_msg(
        "chat.assistant_done",
        {
            "model": f"graphrag:{meta.get('model_id') or graphrag.openrouter_model}",
            "tokens_in": tokens["input_tokens"],
            "tokens_out": tokens["output_tokens"],
        },
        request_id,
    ))

    try:
        await history.save_message(
            session_id, "assistant", body,
            meta={
                "request_id": request_id,
                "mode": "graphrag",
                "model": meta.get("model_id") or graphrag.openrouter_model,
                "provider": meta.get("provider") or graphrag.default_provider,
                "cypher": cypher,
                "row_count": row_count,
                "tokens_in": tokens["input_tokens"],
                "tokens_out": tokens["output_tokens"],
                "graphrag_status": final_status,
                "duration_s": round(total_dt, 2),
            },
        )
    except Exception as exc:
        log.warning("chat.graphrag.history_save_failed", exc_info=exc)


async def _start_chat_screening(
    ws: WebSocket,
    request_id: str,
    session_id: str,
    intent: DockingIntent,
    history: HistoryService,
    library: LibraryService,
    screening: ScreeningService,
) -> None:
    """채팅에서 추출한 docking intent → library 저장 + screening 잡 시작.

    1) SMILES 리스트 → SMI blob → LibraryService.parse + save_to_db
    2) screening_jobs INSERT (status=queued)
    3) event_bus 에 (job_id ↔ session_id) 매핑 등록
    4) 친화적 챗봇 메시지 push (chat.assistant_chunk + done)
    5) screening.run_pipeline_full 백그라운드 실행 (ws_broadcast 가 bus 로 publish)
    """
    from datetime import datetime, timezone

    target_label = intent.target_name or "user_target"
    n_lig = len(intent.ligands)
    seq_len = len(intent.target_sequence)

    # 1. library 저장 (SMI blob 경로 사용 — RDKit 정규화/dedup 그대로 활용)
    smi_bytes = build_smi_blob(intent.ligands)
    try:
        lib = library.parse(
            file_bytes=smi_bytes,
            filename="chat_inline.smi",
            max_entries=n_lig,
        )
        await library.save_to_db(lib)
        library_id = lib.library_id
    except Exception as exc:
        log.error("chat.intent.library_failed", exc_info=exc, session_id=session_id)
        await _send_error(ws, "LIBRARY_PARSE_FAILED", str(exc), request_id)
        return

    # 2. screening_jobs INSERT
    job_id = ulid_str()
    now = datetime.now(timezone.utc).isoformat()
    try:
        async with aiosqlite.connect(history._db_path) as db:
            await db.execute(
                """
                INSERT INTO screening_jobs
                    (job_id, session_id, library_id, target_json, config_json,
                     status, stage, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, session_id, library_id,
                 intent.target_sequence, None,
                 "queued", "ingest", now),
            )
            await db.commit()
    except Exception as exc:
        log.error("chat.intent.job_insert_failed", exc_info=exc, session_id=session_id)
        await _send_error(ws, "JOB_INSERT_FAILED", str(exc), request_id)
        return

    # 3. event_bus 매핑
    event_bus.link_job(job_id, session_id)

    # 4. 사용자에게 즉시 응답 (assistant 버블)
    summary = (
        f"🧪 도킹 작업을 시작했습니다.\n\n"
        f"• 단백질: **{target_label}** ({seq_len}aa)\n"
        f"• 화합물: **{n_lig}개** "
        + ", ".join(name for _, name in intent.ligands)
        + f"\n• Job ID: `{job_id}`\n\n"
        "🔄 3단계로 진행됩니다 (시간 제한 없음 — 각 단계는 완료될 때까지 대기):\n"
        f"  1. **AF3 MSA + 구조 예측** — 리간드 {n_lig}개를 순차 실행 (참고: 리간드당 ~8분 소요 *가능*, "
        "실제 시간은 MSA 캐시/시퀀스 길이에 따라 달라짐)\n"
        "  2. **Smina 에너지 최소화 + 결합 에너지** — 리간드별 로컬 최소화 후 binding affinity 산출\n"
        "  3. **종합 랭킹 + LLM 해석**\n\n"
        "각 단계마다 진행 패널이 sub-stage 와 하트비트를 표시하므로, 정지 여부를 즉시 확인할 수 있습니다."
    )
    await ws.send_text(_build_msg(
        "screening.submitted",
        {"job_id": job_id, "library_id": library_id,
         "target_name": target_label, "seq_len": seq_len,
         "n_ligands": n_lig,
         "ligand_names": [name for _, name in intent.ligands]},
        request_id,
    ))
    await ws.send_text(_build_msg(
        "chat.assistant_chunk",
        {"delta": summary},
        request_id,
    ))
    await ws.send_text(_build_msg(
        "chat.assistant_done",
        {"model": "system", "tokens_in": 0, "tokens_out": 0},
        request_id,
    ))
    try:
        await history.save_message(
            session_id, "assistant", summary,
            meta={"request_id": request_id, "mode": "screening_submit",
                  "job_id": job_id, "library_id": library_id},
        )
    except Exception:
        pass

    # 5. 백그라운드 파이프라인 실행 (ws_broadcast → event_bus.publish_for_job).
    # session_id 직접 캡처 대신 job_id 매핑을 통해 publish — 사용자가 사이드바에서
    # 세션을 잠시 떠났다가 같은 세션으로 돌아오면 새 WS 가 같은 session_id 큐를
    # 다시 구독하므로 이후 이벤트가 정상 수신된다. publish_for_job 은 link_job 에
    # 등록된 매핑을 참조하므로 ws_chat 핸들러가 reconnect 시 link_job 을
    # 재호출하면 다른 session_id 로의 라우팅도 가능 (현재는 동일 세션 가정).
    async def _bcast(event: dict) -> None:
        # screening_service 가 보내는 다양한 stage 이벤트를
        # 프론트가 바로 다룰 수 있는 screening.progress 로 포장.
        # sub_stage / ligand_index / total_ligands / elapsed_s 까지 노출하여
        # jobs_panel.js 가 4단계 sub-row 와 하트비트를 직접 렌더 가능.
        await event_bus.publish_for_job(job_id, {
            "type": "screening.progress",
            "job_id": job_id,
            "payload": event,
            **{k: v for k, v in event.items()
               if k in (
                   "event", "stage", "ligand_id", "from_stage", "to_stage",
                   "sub_stage", "ligand_index", "total_ligands",
                   "elapsed_s", "progress", "raw_status", "n_ligands",
                   "n_results",
               )},
        })

    target_input = TargetInput(sequence=intent.target_sequence)
    ctx = ScreeningJobContext(
        job_id=job_id,
        session_id=session_id,
        library_id=library_id,
        target_input=target_input,
        config={},
        ws_broadcast=_bcast,
    )

    async def _run() -> None:
        try:
            log.info("chat.screening.spawn", job_id=job_id, session_id=session_id)
            result = await screening.run_pipeline_full(ctx)
            final_status = result.get("status", "unknown")
            async with aiosqlite.connect(history._db_path) as db:
                await db.execute(
                    "UPDATE screening_jobs SET status=?, ended_at=?, error=? WHERE job_id=?",
                    (final_status,
                     datetime.now(timezone.utc).isoformat(),
                     ", ".join(result.get("errors", [])) or None,
                     job_id),
                )
                await db.commit()

            # 최종 결과 알림 — publish_for_job 사용 (session_id 가 변경된 경우에도
            # event_bus 의 job→session 매핑이 갱신되어 있다면 추적 가능)
            await event_bus.publish_for_job(job_id, {
                "type": "screening.complete",
                "job_id": job_id,
                "status": final_status,
                "total": n_lig,
                "errors": result.get("errors", []),
            })
            await event_bus.publish_for_job(job_id, {
                "type": "chat.assistant_chunk",
                "request_id": ulid_str(),
                "payload": {
                    "delta": f"\n\n✅ 작업 완료 (status={final_status}). "
                             "결과 패널에서 smina 기반 랭킹을 확인하세요."
                },
                "delta": f"\n\n✅ 작업 완료 (status={final_status}). "
                         "결과 패널에서 smina 기반 랭킹을 확인하세요.",
            })
            await event_bus.publish_for_job(job_id, {
                "type": "chat.assistant_done",
                "request_id": ulid_str(),
                "payload": {"model": "system", "tokens_in": 0, "tokens_out": 0},
            })
        except Exception as exc:
            log.error("chat.screening.crash", exc_info=exc, job_id=job_id)
            try:
                async with aiosqlite.connect(history._db_path) as db:
                    await db.execute(
                        "UPDATE screening_jobs SET status=?, ended_at=?, error=? WHERE job_id=?",
                        ("failed",
                         datetime.now(timezone.utc).isoformat(),
                         str(exc)[:500], job_id),
                    )
                    await db.commit()
            except Exception:
                pass
            await event_bus.publish_for_job(job_id, {
                "type": "screening.error",
                "job_id": job_id,
                "message": str(exc)[:500],
            })

    asyncio.create_task(_run())


@router.websocket("/ws/chat/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: str) -> None:
    """채팅 WebSocket 엔드포인트.

    세션 연결 → 메시지 수신 루프 → 타입별 dispatch → 응답 전송.
    """
    await websocket.accept()
    log.info("ws.chat.connected", session_id=session_id)

    # 서비스 인스턴스: app.state 에서 직접 접근 (WebSocket 은 Depends 직접 사용 불가)
    llm: LLMService = websocket.app.state.llm
    history: HistoryService = websocket.app.state.history

    # event_bus 구독: screening 파이프라인이 발행하는 stage 이벤트를 chat WS 로 전달
    event_queue = event_bus.subscribe(session_id)

    # WS reconnect 시 (사이드바 세션 전환 등) 같은 session_id 로 들어오는 진행중 잡의
    # job→session 매핑이 살아있도록 DB에서 ‘이 세션 소유의 미종료 잡’을 다시 link.
    # publish_for_job 이 매핑을 참조하므로 새 WS 가 이전에 등록된 잡 이벤트를
    # 받기 위해 필요. (link_job 은 멱등 — dict overwrite.)
    try:
        async with aiosqlite.connect(history._db_path) as db:
            async with db.execute(
                """
                SELECT job_id FROM screening_jobs
                 WHERE session_id = ?
                   AND status IN ('queued', 'running')
                """,
                (session_id,),
            ) as cur:
                rows = await cur.fetchall()
        for r in rows:
            event_bus.link_job(r[0], session_id)
        if rows:
            log.info("ws.chat.jobs_relinked", session_id=session_id, count=len(rows))
    except Exception as exc:
        log.warning("ws.chat.jobs_relink_failed", exc_info=exc, session_id=session_id)

    async def _forward_loop() -> None:
        """bus → websocket 단방향 forwarder. 연결 종료 시 자연 취소."""
        try:
            while True:
                ev = await event_queue.get()
                try:
                    await websocket.send_text(json.dumps(ev, ensure_ascii=False))
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    forward_task = asyncio.create_task(_forward_loop())

    try:
        while True:
            raw = await websocket.receive_text()

            # 파싱
            try:
                data = json.loads(raw)
                msg = WSMessage.model_validate(data)
            except Exception as exc:
                log.warning("ws.chat.parse_error", exc_info=exc)
                await _send_error(websocket, "INVALID_ENVELOPE", "invalid message format")
                continue

            msg_type = msg.type
            payload = msg.payload
            request_id = msg.request_id

            log.debug("ws.chat.recv", type=msg_type, session_id=session_id, request_id=request_id)

            if msg_type == "chat.user_message":
                await _handle_user_message(
                    websocket, payload, request_id, llm, history, session_id,
                )

            else:
                # Phase 1: 미구현 타입 → NOT_IMPLEMENTED 에러
                log.info(
                    "ws.chat.not_implemented",
                    type=msg_type,
                    session_id=session_id,
                    request_id=request_id,
                )
                await _send_error(
                    websocket,
                    "NOT_IMPLEMENTED",
                    f"message type '{msg_type}' is not implemented in Phase 1",
                    request_id,
                )

    except WebSocketDisconnect:
        log.info("ws.chat.disconnected", session_id=session_id)
    except Exception as exc:
        log.error("ws.chat.unexpected_error", exc_info=exc, session_id=session_id)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        forward_task.cancel()
        event_bus.unsubscribe(session_id, event_queue)
