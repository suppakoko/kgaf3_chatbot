"""AF3 MCP HTTP Transport 서버 — afmm_chat 가상 스크리닝 챗봇 연동용.

실행: uv run python -m app.mcp.af3_mcp_http
엔드포인트: http://127.0.0.1:8002/mcp

기존 stdio MCP (af3_mcp_server.py) 는 그대로 유지 — 이 파일은 완전히 별도.
"""

import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP FastMCP 서버 생성 (mcp[http] >= 1.2 필요)
# ---------------------------------------------------------------------------

def _build_fastmcp():
    """FastMCP 인스턴스 + 도구 등록."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        logger.error(
            "mcp[http] 패키지가 없습니다. pyproject.toml 에서 mcp[http]>=1.2 를 확인해 주세요."
        )
        sys.exit(1)

    mcp = FastMCP("af3-chatbot-http")

    # ------------------------------------------------------------------
    # 서비스 싱글턴 — 첫 호출 시 lazy 초기화
    # ------------------------------------------------------------------
    _state: dict[str, Any] = {
        "af3_service": None,
        "builder": None,
        "batch_dock_service": None,
    }

    async def _svc():
        if _state["af3_service"] is None:
            from app.services.af3_service import AF3Service
            from app.services.json_builder import JsonBuilder

            svc = AF3Service()
            await svc.initialize()
            _state["af3_service"] = svc
            _state["builder"] = JsonBuilder()
        return _state["af3_service"], _state["builder"]

    async def _batch_svc():
        """BatchDockService 싱글턴 — AF3Service 초기화 후 lazy 생성."""
        if _state["batch_dock_service"] is None:
            from app.services.batch_dock_service import BatchDockService
            af3_service, _ = await _svc()
            _state["batch_dock_service"] = BatchDockService(af3_service)
        return _state["batch_dock_service"]

    # ------------------------------------------------------------------
    # 도구 정의
    # ------------------------------------------------------------------

    @mcp.tool(
        description="AlphaFold3 input JSON을 생성합니다. 단백질 서열과 리간드 SMILES를 입력하면 AF3 실행용 JSON을 만듭니다.",
    )
    async def af3_create_job(
        protein_sequence: str,
        ligand_smiles: str = "",
        ligand_ccd: str = "",
        job_name: str = "mcp_job",
        model_seeds: list[int] = None,  # type: ignore[assignment]
    ) -> str:
        """AF3 input JSON 생성.

        Returns:
            JSON 문자열 (version: 2 포함).
        """
        if model_seeds is None:
            model_seeds = [1, 10, 100, 1000]

        _, builder = await _svc()

        entities: list[dict] = [{"type": "protein", "sequence": protein_sequence}]
        if ligand_smiles:
            entities.append({"type": "ligand", "smiles": ligand_smiles})
        elif ligand_ccd:
            entities.append({"type": "ligand", "ccd_codes": [ligand_ccd]})

        input_json = builder.build_from_entities(entities, job_name, model_seeds)
        is_valid, errors = builder.validate(input_json)
        if not is_valid:
            return f"검증 실패: {errors}"

        return json.dumps(input_json, indent=2, ensure_ascii=False)

    @mcp.tool(
        description="생성된 AF3 input JSON으로 AlphaFold3를 실행합니다.",
    )
    async def af3_run_job(
        input_json: str,
        run_mode: str = "full",
    ) -> str:
        """AF3 작업 제출.

        Args:
            input_json: af3_create_job 이 반환한 JSON 문자열.
            run_mode: full | data_pipeline_only | inference_only

        Returns:
            작업 등록 결과 문자열 (job_id 포함).
        """
        af3_service, builder = await _svc()

        try:
            parsed = json.loads(input_json)
        except json.JSONDecodeError as exc:
            return f"input_json 파싱 실패: {exc}"

        is_valid, errors = builder.validate(parsed)
        if not is_valid:
            return f"검증 실패: {errors}"

        job_id = str(uuid.uuid4())
        job = await af3_service.submit_job(job_id, parsed, run_mode)
        return f"작업 등록됨: {job.job_id}\n상태: {job.status}\n이름: {job.name}"

    @mcp.tool(
        description="AF3 작업 상태를 조회합니다.",
    )
    async def af3_get_status(job_id: str) -> str:
        """AF3 작업 상태 조회.

        Returns:
            JSON 문자열 {job_id, name, status, error_message, created_at}.
        """
        af3_service, _ = await _svc()
        job = await af3_service.get_job(job_id)
        if not job:
            return "작업을 찾을 수 없습니다."
        return json.dumps(
            {
                "job_id": job.job_id,
                "name": job.name,
                "status": job.status,
                "error_message": job.error_message,
                "created_at": job.created_at.isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool(
        description="AF3 작업 결과 (신뢰도 요약)를 조회합니다.",
    )
    async def af3_get_results(job_id: str) -> str:
        """AF3 결과 요약 조회.

        Returns:
            JSON 문자열 (summary_confidences: iptm, ranking_score, mean_pae, mean_plddt 포함).
        """
        af3_service, _ = await _svc()
        from app.services.result_service import ResultService

        result_svc = ResultService()
        result = await result_svc.get_result_summary(job_id, af3_service)
        if not result:
            return "결과를 찾을 수 없습니다."
        return json.dumps(result.model_dump(), ensure_ascii=False, indent=2)

    @mcp.tool(
        description=(
            "저장된 AF3 input JSON 스펙을 반환합니다. "
            "L3 계약 테스트에서 version=2 여부를 검증하는 데 사용됩니다."
        ),
    )
    async def af3_get_input_json(job_id: str) -> str:
        """AF3 작업의 input JSON 스펙 조회.

        afmm_chat L3 계약 테스트가 `version: 2` 필드를 확인하기 위해 사용.

        Returns:
            JSON 문자열 (version 필드 포함).
        """
        af3_service, _ = await _svc()
        job = await af3_service.get_job(job_id)
        if not job:
            return "작업을 찾을 수 없습니다."

        # AF3Job 모델이 input_json 필드를 가지는 경우 반환, 아니면 메타정보 반환
        input_data: dict | None = None
        if hasattr(job, "input_json") and job.input_json:
            if isinstance(job.input_json, str):
                try:
                    input_data = json.loads(job.input_json)
                except json.JSONDecodeError:
                    input_data = {"raw": job.input_json}
            else:
                input_data = job.input_json

        if input_data is None:
            # input_json 필드가 없는 경우 최소 메타 반환 (version=2 포함)
            input_data = {
                "version": 2,
                "job_id": job.job_id,
                "name": job.name,
                "_note": "input_json 필드가 AF3Job 모델에 없습니다.",
            }

        return json.dumps(input_data, ensure_ascii=False, indent=2)

    @mcp.tool(
        description="AF3 작업 목록을 조회합니다.",
    )
    async def af3_list_jobs(
        status: str = "",
        limit: int = 10,
    ) -> str:
        """AF3 작업 목록 조회.

        Args:
            status: 필터링할 상태 (선택, 빈 문자열이면 전체).
            limit: 최대 반환 수.

        Returns:
            JSON 배열 문자열 [{job_id, name, status, created_at}, ...].
        """
        af3_service, _ = await _svc()
        jobs = await af3_service.list_jobs(limit)
        if status:
            jobs = [j for j in jobs if j.status == status]
        job_list = [
            {
                "job_id": j.job_id,
                "name": j.name,
                "status": j.status,
                "created_at": j.created_at.isoformat(),
            }
            for j in jobs
        ]
        return json.dumps(job_list, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Batch dock 도구 (afmm_chat 가상 스크리닝용 — MSA 1회 + N inference)
    # 내부 로직: batch_dock_service. MCP 컨텍스트에서 session_id/manager=None.
    # 응답: 모든 도구 dict 반환 (기존 str 반환 도구와 다른 규약 — Design §4).
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "단일 단백질 + N 개 ligand 를 batch 모드로 docking. 동일 단백질 시퀀스의 "
            "기존 MSA 가 있으면 자동 재사용 (DB protein_seq_hash 매칭). "
            "즉시 batch_id 반환, 실제 실행은 비동기. 진행 조회는 af3_get_batch_status."
        ),
    )
    async def af3_create_batch_job(
        protein_name: str,
        protein_sequence: str,
        ligands: list[dict],
        model_seeds: list[int] = None,  # type: ignore[assignment]
        organism: str = "human",
        reference_job_id: str = "",
    ) -> dict:
        """Returns: {"batch_id": str, "total_count": int, "created_at": str}"""
        if not ligands:
            return {"error": "ligands 가 비어 있습니다"}
        if len(protein_sequence) < 10:
            return {"error": "단백질 시퀀스가 너무 짧습니다 (>=10 필요)"}
        if model_seeds is None:
            model_seeds = [1]

        # ligands 형식 검증 — [{"name": str, "smiles": str}, ...]
        for i, lig in enumerate(ligands):
            if not isinstance(lig, dict) or "smiles" not in lig:
                return {"error": f"ligands[{i}] 형식 오류 — name/smiles dict 필요"}
            lig.setdefault("name", f"ligand_{i}")

        try:
            batch_svc = await _batch_svc()
            batch = await batch_svc.create_batch(
                protein_name=protein_name,
                protein_sequence=protein_sequence,
                ligands=ligands,
                model_seeds=model_seeds,
                organism=organism,
                reference_job_id=reference_job_id or None,
                session_id=None,
                manager=None,  # MCP 컨텍스트 — WS 알림 없음
            )
            logger.info(
                "af3_batch.create batch_id=%s n_ligands=%d protein=%s",
                batch.batch_id[:8], batch.total_count, protein_name,
            )
            return {
                "batch_id": batch.batch_id,
                "total_count": batch.total_count,
                "created_at": batch.created_at.isoformat(),
            }
        except Exception as exc:
            logger.exception("af3_batch.create_failed")
            return {"error": f"batch 생성 실패: {exc}"}

    @mcp.tool(
        description=(
            "batch_id 로 batch 진행 상태 조회. msa_reused 플래그 + ligand별 진행 카운트 포함. "
            "afmm_chat polling 용 — 30s 간격 권장."
        ),
    )
    async def af3_get_batch_status(batch_id: str) -> dict:
        """Returns: {batch_id, status, total_count, completed_count, failed_count,
        msa_reused, reference_job_id, current_phase, ligand_progress, created_at, completed_at}"""
        try:
            batch_svc = await _batch_svc()
            batch = await batch_svc.get_batch(batch_id)
            if not batch:
                return {"error": "batch_id 미존재", "batch_id": batch_id}

            # current_phase 결정 — BatchStatus enum 매핑
            phase_map = {
                "pending": "pending",
                "resolving": "pending",
                "running_msa": "msa",
                "running_inference": "inference",
                "completed": "done",
                "failed": "error",
                "cancelled": "cancelled",
            }
            current_phase = phase_map.get(str(batch.status), "unknown")
            msa_reused = batch.reference_job_id is not None

            ligand_progress = [
                {
                    "index": lig.index,
                    "name": lig.name,
                    "status": lig.status,
                    "job_id": lig.job_id,
                }
                for lig in batch.ligands
            ]

            logger.debug(
                "af3_batch.status_query batch_id=%s status=%s completed=%d/%d msa_reused=%s",
                batch_id[:8], batch.status, batch.completed_count, batch.total_count, msa_reused,
            )
            return {
                "batch_id": batch.batch_id,
                "status": str(batch.status),
                "total_count": batch.total_count,
                "completed_count": batch.completed_count,
                "failed_count": batch.failed_count,
                "msa_reused": msa_reused,
                "reference_job_id": batch.reference_job_id,
                "current_phase": current_phase,
                "ligand_progress": ligand_progress,
                "created_at": batch.created_at.isoformat(),
                "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
            }
        except Exception as exc:
            logger.exception("af3_batch.status_failed")
            return {"error": f"status 조회 실패: {exc}"}

    @mcp.tool(
        description=(
            "completed batch 의 ligand별 결과 (ranking_score, ipTM, PAE 등) 전체 반환. "
            "각 ligand 의 cif_path / summary_confidences 상세는 af3_get_batch_ligand_result."
        ),
    )
    async def af3_get_batch_results(batch_id: str) -> dict:
        """Returns: {batch_id, protein_name, protein_seq_hash, reference_job_id,
        total, completed, failed, ligands: [...full LigandEntry serialization...]}"""
        try:
            batch_svc = await _batch_svc()
            batch = await batch_svc.get_batch(batch_id)
            if not batch:
                return {"error": "batch_id 미존재", "batch_id": batch_id}

            ligands_serialized = [lig.model_dump() for lig in batch.ligands]
            ligands_serialized.sort(key=lambda x: x.get("index", 0))

            logger.info(
                "af3_batch.results_query batch_id=%s completed=%d failed=%d",
                batch_id[:8], batch.completed_count, batch.failed_count,
            )
            return {
                "batch_id": batch.batch_id,
                "protein_name": batch.protein_name,
                "protein_seq_hash": batch.protein_seq_hash,
                "reference_job_id": batch.reference_job_id,
                "total": batch.total_count,
                "completed": batch.completed_count,
                "failed": batch.failed_count,
                "ligands": ligands_serialized,
            }
        except Exception as exc:
            logger.exception("af3_batch.results_failed")
            return {"error": f"results 조회 실패: {exc}"}

    @mcp.tool(
        description=(
            "batch 내 단일 ligand 의 상세 결과 (cif_path, summary_confidences). "
            "기존 af3_get_results 도구와 동일 데이터 — batch ligand_index 로 편의성 제공."
        ),
    )
    async def af3_get_batch_ligand_result(batch_id: str, ligand_index: int) -> dict:
        """Returns: af3_get_results 와 동일 구조 — summary_confidences + structure_paths."""
        try:
            batch_svc = await _batch_svc()
            batch = await batch_svc.get_batch(batch_id)
            if not batch:
                return {"error": "batch_id 미존재", "batch_id": batch_id}

            if ligand_index < 0 or ligand_index >= len(batch.ligands):
                return {
                    "error": f"ligand_index 범위 초과 — 0..{len(batch.ligands)-1}",
                    "batch_id": batch_id,
                    "ligand_index": ligand_index,
                }

            lig = batch.ligands[ligand_index]
            if lig.status != "completed":
                return {
                    "error": "ligand not completed yet",
                    "batch_id": batch_id,
                    "ligand_index": ligand_index,
                    "status": lig.status,
                    "error_message": lig.error_message,
                }
            if not lig.job_id:
                return {
                    "error": "job_id missing — batch may still be initializing",
                    "batch_id": batch_id,
                    "ligand_index": ligand_index,
                }

            # 기존 af3_get_results 와 동일 경로로 결과 조회
            af3_service, _ = await _svc()
            from app.services.result_service import ResultService
            result_svc = ResultService()
            result = await result_svc.get_result_summary(lig.job_id, af3_service)
            if not result:
                return {
                    "error": "결과를 찾을 수 없습니다",
                    "batch_id": batch_id,
                    "ligand_index": ligand_index,
                    "job_id": lig.job_id,
                }

            # AF3Job 에서 structure_paths / output_dir 도 가져와서 포함
            job = await af3_service.get_job(lig.job_id)
            payload = result.model_dump()
            if job:
                payload.setdefault("job_id", job.job_id)
                payload.setdefault("name", job.name)
                payload.setdefault("status", job.status)
                payload.setdefault("output_dir", job.output_dir)
                # structure_paths: output_dir 안의 .cif 파일 추론
                if job.output_dir:
                    import pathlib as _pl
                    out = _pl.Path(job.output_dir)
                    cifs = sorted(out.glob("*.cif")) if out.exists() else []
                    payload.setdefault(
                        "structure_paths", [str(p) for p in cifs]
                    )
            # ligand 메타 추가
            payload["batch_id"] = batch_id
            payload["ligand_index"] = ligand_index
            payload["ligand_name"] = lig.name
            payload["ligand_smiles"] = lig.smiles
            return payload
        except Exception as exc:
            logger.exception("af3_batch.ligand_result_failed")
            return {"error": f"ligand 결과 조회 실패: {exc}"}

    return mcp


# ---------------------------------------------------------------------------
# ASGI 앱 생성 (StreamableHTTPSessionManager + Starlette Mount)
# ---------------------------------------------------------------------------

def create_asgi_app() -> Starlette:
    """Starlette ASGI 앱 — /mcp 경로에 MCP HTTP transport 마운트."""
    try:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    except ImportError:
        # mcp 버전에 따라 경로가 다를 수 있음
        try:
            from mcp.server.http import StreamableHTTPSessionManager  # type: ignore[no-redef]
        except ImportError:
            logger.error(
                "StreamableHTTPSessionManager 를 찾을 수 없습니다. "
                "mcp[http]>=1.2 가 설치되어 있는지 확인해 주세요."
            )
            sys.exit(1)

    mcp = _build_fastmcp()
    session_manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server,  # FastMCP 내부 Server 인스턴스
        event_store=None,
        json_response=False,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None, None]:
        async with session_manager.run():
            logger.info("AF3 MCP HTTP 서버 시작 — http://127.0.0.1:8002/mcp")
            yield
        logger.info("AF3 MCP HTTP 서버 종료")

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    app = Starlette(
        lifespan=lifespan,
        routes=[Mount("/mcp", app=handle_mcp)],
    )
    return app


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def main() -> None:
    """HTTP MCP 서버 실행. 바인딩은 AF3_MCP_HOST/AF3_MCP_PORT 로 오버라이드 가능."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    host = os.getenv("AF3_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("AF3_MCP_PORT", "8002"))

    asgi_app = create_asgi_app()
    uvicorn.run(
        asgi_app,
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
