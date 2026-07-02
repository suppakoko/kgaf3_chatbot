"""AF3 MCP HTTP 클라이언트 어댑터.

mcp_integration.md §3 권위.
Phase 1: connect / ping / list_tools 구현.
Phase 2: create_job / run_job / wait_until_done / get_results 구현.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import structlog

from app.services.mcp_http import MCPHttpClient, MCPError  # noqa: F401 (re-export)

log = structlog.get_logger("service.af3")


class AF3MCPClient(MCPHttpClient):
    """af3_chatbot MCP HTTP 어댑터.

    base_url: AF3_MCP_URL (예: http://127.0.0.1:8002/mcp)
    """

    def __init__(self, base_url: str, auth_token: str = "", timeout: float = 600.0):
        super().__init__(base_url=base_url, auth_token=auth_token, timeout=timeout, name="af3")

    # ── Phase 1: 연결 / 상태 확인 ────────────────────────────────────────────

    async def connect(self) -> None:
        """MCP initialize + tools/list 캐시."""
        await super().connect()
        log.info("af3.connected", tool_count=len(self._tool_index))

    async def ping(self) -> bool:
        """5초 타임아웃 헬스체크."""
        ok = await super().ping()
        log.debug("af3.ping", ok=ok)
        return ok

    async def list_tools(self) -> list[dict]:
        """캐시된 af3 도구 목록 반환."""
        return await super().list_tools()

    # ── Phase 2: job 생성/실행/폴링/결과 ────────────────────────────────────

    async def create_job(
        self,
        *,
        target_seq: str,
        ligand_smiles: str,
        job_name: str,
        num_seeds: int = 1,
    ) -> str:
        """af3_create_job → job_id 반환.

        af3_chatbot MCP 의 af3_create_job 은 input JSON 문자열을 반환한다.
        job_id 추출: 반환 값이 JSON dict 이면 "job_id" 키 사용,
        아니면 af3_run_job 으로 실제 job_id 를 얻는다.

        Notes:
            af3_chatbot 서버(af3_mcp_http.py) 는 af3_create_job 에서
            input JSON 문자열을 반환하고 job_id 를 별도 키로 제공하지 않는다.
            af3_run_job 에서 "job_id:" 포함 문자열을 반환하므로
            create_job 은 중간 결과를 저장하고 run_job 에서 ID 를 가져오는 방식을 사용한다.
            여기서는 create_job 결과(input_json 문자열)를 저장하고
            run_job 에서 실제 job_id 를 파싱해 반환하도록 두 단계를 통합한다.

        Returns:
            job_id string (af3_run_job 응답에서 파싱).
        """
        log.info(
            "af3.create_job",
            job_name=job_name,
            seq_len=len(target_seq),
            num_seeds=num_seeds,
        )

        # Step 1: input JSON 생성
        create_res = await self.call(
            "af3_create_job",
            {
                "protein_sequence": target_seq,
                "ligand_smiles": ligand_smiles,
                "job_name": job_name,
                "model_seeds": list(range(1, num_seeds + 1)),
            },
        )

        # create_res 는 JSON dict 또는 JSON 문자열일 수 있다.
        if isinstance(create_res, dict) and "job_id" in create_res:
            return str(create_res["job_id"])

        # 문자열인 경우 → 나중에 af3_run_job 에 전달할 input JSON
        input_json_str: str
        if isinstance(create_res, str):
            input_json_str = create_res
        else:
            # dict 이지만 job_id 없음 → JSON 직렬화
            input_json_str = json.dumps(create_res)

        # Step 2: run_job 으로 실제 job_id 획득
        job_id = await self._run_and_extract_id(input_json_str)
        return job_id

    async def run_job(self, job_id: str) -> dict:
        """af3_run_job → {started, job_id, ...}.

        create_job 이 input_json 을 이미 제출한 경우
        이 메서드는 no-op 이 될 수 있다. job_id 로 상태를 확인해 진행 중이면 그대로 반환.

        Returns:
            {"job_id": job_id, "started": True} dict.
        """
        log.info("af3.run_job", job_id=job_id)
        # job_id 가 이미 실행 중인지 확인 (create_job 에서 이미 run 까지 완료된 경우)
        try:
            status_res = await self.get_status(job_id)
            if status_res.get("status") not in ("", None, "not_found"):
                # 이미 등록/실행 중
                return {"job_id": job_id, "started": True, "status": status_res.get("status")}
        except MCPError:
            pass

        # 미등록 상태면 af3_run_job 직접 호출 (input_json 으로 재실행 불가 — 로그만)
        log.warning("af3.run_job.no_input_json", job_id=job_id)
        return {"job_id": job_id, "started": False, "error": "input_json not available for re-run"}

    async def get_status(self, job_id: str) -> dict:
        """af3_get_status → {status, stage?, progress?, ...}.

        Returns:
            상태 dict. status 키: "pending", "running", "completed", "done", "failed", "error".
        """
        res = await self.call("af3_get_status", {"job_id": job_id})

        # 응답이 JSON 문자열일 수 있음 (af3_chatbot 은 str 반환)
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except json.JSONDecodeError:
                # "작업을 찾을 수 없습니다." 같은 plain string
                return {"status": "not_found", "raw": res}

        if isinstance(res, dict):
            return res

        return {"status": "unknown", "raw": str(res)}

    async def wait_until_done(
        self,
        job_id: str,
        *,
        poll_s: float = 30.0,
        timeout_s: float = 5400.0,
        on_poll: Callable[[dict, float], Any] | None = None,
    ) -> dict:
        """get_status 폴링 → status∈{done,completed} 시 get_results 반환.

        Args:
            job_id: AF3 잡 ID.
            poll_s: 폴링 간격 (초, 기본 30s).
            timeout_s: 최대 대기 시간 (초, 기본 5400s = 90min). UI 가시화 강화 후에도
                여전히 안전망으로 두지만, MSA(~30min) + structure inference(~5min/seed)
                를 충분히 수용한다. 호출자는 None 또는 매우 큰 값을 넘겨 사실상 무제한
                대기로 만들 수 있다.
            on_poll: 매 poll 마다 호출되는 async/sync 콜백.
                signature: (status_dict, elapsed_s) -> Awaitable[None] | None.
                status_dict 예: {"status": "running", "stage": "msa", "progress": 0.3, ...}.
                예외는 무시 (heartbeat 손실이 잡 자체를 죽이지 않도록).

        Returns:
            af3_get_results 응답 dict.

        Raises:
            MCPError("AF3_FAILED"): status 가 failed 또는 error 인 경우.
            MCPError("AF3_TIMEOUT"): timeout_s 초과 시.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        elapsed = 0.0

        log.info("af3.wait_until_done", job_id=job_id, timeout_s=timeout_s, poll_s=poll_s)

        while True:
            st = await self.get_status(job_id)
            status = st.get("status", "unknown")

            log.debug(
                "af3.poll",
                job_id=job_id,
                status=status,
                stage=st.get("stage"),
                progress=st.get("progress"),
                elapsed_s=round(elapsed, 1),
            )

            # 콜백 — heartbeat / sub-stage 브로드캐스트용. 실패는 무시.
            if on_poll is not None:
                try:
                    cb_result = on_poll(st, elapsed)
                    if asyncio.iscoroutine(cb_result):
                        await cb_result
                except Exception as cb_exc:
                    log.warning("af3.on_poll_failed", job_id=job_id, error=str(cb_exc))

            if status in {"done", "completed"}:
                log.info("af3.job_done", job_id=job_id, elapsed_s=round(elapsed, 1))
                return await self.get_results(job_id)

            if status in {"failed", "error"}:
                msg = st.get("error_message") or st.get("message") or f"AF3 job {job_id} failed"
                log.error("af3.job_failed", job_id=job_id, status=status, message=msg)
                raise MCPError("AF3_FAILED", msg)

            if loop.time() > deadline:
                log.error("af3.timeout", job_id=job_id, timeout_s=timeout_s)
                raise MCPError("AF3_TIMEOUT", f"job {job_id} timed out after {timeout_s}s")

            await asyncio.sleep(poll_s)
            elapsed += poll_s

    async def get_results(self, job_id: str) -> dict:
        """af3_get_results → 전체 결과 dict.

        반환 키 (af3_chatbot 기준):
            job_id, name, status, summary_confidences, model_path, structure_paths, output_dir
        """
        res = await self.call("af3_get_results", {"job_id": job_id})

        if isinstance(res, str):
            try:
                res = json.loads(res)
            except json.JSONDecodeError:
                return {"raw": res, "job_id": job_id}

        if isinstance(res, dict):
            return res

        return {"raw": str(res), "job_id": job_id}

    async def get_input_json(self, job_id: str) -> dict:
        """af3_get_input_json → input JSON dict (version=2 검증용).

        L3 계약 테스트에서 사용.
        """
        res = await self.call("af3_get_input_json", {"job_id": job_id})

        if isinstance(res, str):
            try:
                res = json.loads(res)
            except json.JSONDecodeError:
                return {"raw": res}

        if isinstance(res, dict):
            return res

        return {"raw": str(res)}

    # ── Batch dock 메서드 (MSA 1회 + N inference) ─────────────────────────────

    async def create_batch_job(
        self,
        *,
        protein_name: str,
        target_seq: str,
        ligands: list[tuple[str, str]],
        num_seeds: int = 1,
        organism: str = "human",
        reference_job_id: str | None = None,
    ) -> str:
        """af3_create_batch_job → batch_id 반환.

        Args:
            protein_name: 단백질 라벨 (DB 저장용).
            target_seq: 단백질 시퀀스.
            ligands: [(smiles, name), ...] — N 개 ligand.
            num_seeds: 각 ligand 별 model_seeds 길이. 기본 1.
            organism: 미사용에 가까움 — DB 메타.
            reference_job_id: 이미 존재하는 단일 잡 ID. 지정 시 그것의 MSA 재사용.

        Returns:
            batch_id (uuid).
        """
        log.info(
            "af3.create_batch_job",
            protein_name=protein_name,
            n_ligands=len(ligands),
            seq_len=len(target_seq),
        )
        ligands_payload = [
            {"name": name, "smiles": smi} for smi, name in ligands
        ]
        args: dict[str, Any] = {
            "protein_name": protein_name,
            "protein_sequence": target_seq,
            "ligands": ligands_payload,
            "model_seeds": list(range(1, num_seeds + 1)),
            "organism": organism,
        }
        if reference_job_id:
            args["reference_job_id"] = reference_job_id

        res = await self.call("af3_create_batch_job", args)
        if isinstance(res, dict):
            if "error" in res:
                raise MCPError("AF3_BATCH_CREATE_FAILED", str(res["error"]))
            batch_id = res.get("batch_id")
            if batch_id:
                return str(batch_id)
        raise MCPError("RPC_ERROR", f"af3_create_batch_job 응답에 batch_id 없음: {res!r}")

    async def get_batch_status(self, batch_id: str) -> dict:
        """af3_get_batch_status → 상태 dict."""
        res = await self.call("af3_get_batch_status", {"batch_id": batch_id})
        if isinstance(res, dict):
            return res
        return {"raw": str(res), "batch_id": batch_id}

    async def get_batch_results(self, batch_id: str) -> dict:
        """af3_get_batch_results → ligand별 결과 메타."""
        res = await self.call("af3_get_batch_results", {"batch_id": batch_id})
        if isinstance(res, dict):
            return res
        return {"raw": str(res), "batch_id": batch_id}

    async def get_batch_ligand_result(
        self, batch_id: str, ligand_index: int,
    ) -> dict:
        """af3_get_batch_ligand_result → 단일 ligand 상세 (cif_path 포함)."""
        res = await self.call(
            "af3_get_batch_ligand_result",
            {"batch_id": batch_id, "ligand_index": ligand_index},
        )
        if isinstance(res, dict):
            return res
        return {"raw": str(res), "batch_id": batch_id, "ligand_index": ligand_index}

    async def wait_batch_done(
        self,
        batch_id: str,
        *,
        poll_s: float = 30.0,
        timeout_s: float = 14400.0,
        on_poll: Callable[[dict, float], Any] | None = None,
    ) -> dict:
        """batch status 가 terminal 상태 (completed/failed/cancelled) 가 될 때까지 폴.

        Args:
            batch_id: batch ID.
            poll_s: 폴 간격 (초). 기본 30s.
            timeout_s: 최대 대기 시간 (초). 기본 4h (100 ligand 충분).
            on_poll: 매 폴 콜백 (status_dict, elapsed_s).

        Returns:
            최종 batch status dict.

        Raises:
            MCPError("AF3_BATCH_TIMEOUT"): timeout_s 초과 시.
            MCPError("AF3_BATCH_FAILED"): status 가 failed/cancelled 인 경우.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        elapsed = 0.0
        log.info("af3.wait_batch_done", batch_id=batch_id, timeout_s=timeout_s, poll_s=poll_s)

        terminal_ok = {"completed"}
        terminal_err = {"failed", "cancelled"}

        while True:
            st = await self.get_batch_status(batch_id)
            status = str(st.get("status", "unknown"))

            log.debug(
                "af3.batch_poll",
                batch_id=batch_id,
                status=status,
                phase=st.get("current_phase"),
                completed=st.get("completed_count"),
                total=st.get("total_count"),
                elapsed_s=round(elapsed, 1),
            )

            if on_poll is not None:
                try:
                    cb_result = on_poll(st, elapsed)
                    if asyncio.iscoroutine(cb_result):
                        await cb_result
                except Exception as cb_exc:
                    log.warning("af3.batch_on_poll_failed", batch_id=batch_id, error=str(cb_exc))

            if status in terminal_ok:
                log.info("af3.batch_done", batch_id=batch_id, elapsed_s=round(elapsed, 1))
                return st
            if status in terminal_err:
                msg = st.get("error") or f"batch {batch_id} {status}"
                log.error("af3.batch_failed", batch_id=batch_id, status=status, message=msg)
                raise MCPError("AF3_BATCH_FAILED", str(msg))

            if loop.time() > deadline:
                log.error("af3.batch_timeout", batch_id=batch_id, timeout_s=timeout_s)
                raise MCPError("AF3_BATCH_TIMEOUT", f"batch {batch_id} timed out after {timeout_s}s")

            await asyncio.sleep(poll_s)
            elapsed += poll_s

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────────────

    async def _run_and_extract_id(self, input_json_str: str) -> str:
        """af3_run_job 호출 → 응답 문자열에서 job_id 파싱.

        af3_chatbot 응답 예:
            "작업 등록됨: <uuid>\n상태: pending\n이름: ..."
        또는 JSON: {"job_id": "...", "status": "pending"}
        """
        res = await self.call(
            "af3_run_job",
            {"input_json": input_json_str, "run_mode": "full"},
        )

        if isinstance(res, dict):
            job_id = res.get("job_id")
            if job_id:
                return str(job_id)
            raise MCPError("RPC_ERROR", f"af3_run_job 응답에 job_id 없음: {res}")

        if isinstance(res, str):
            # "작업 등록됨: <uuid>" 파싱
            for line in res.splitlines():
                line = line.strip()
                if line.startswith("작업 등록됨:") or line.lower().startswith("job registered:"):
                    job_id = line.split(":", 1)[-1].strip()
                    if job_id:
                        return job_id
            raise MCPError("RPC_ERROR", f"af3_run_job 응답에서 job_id 파싱 실패: {res!r}")

        raise MCPError("RPC_ERROR", f"af3_run_job 예상치 못한 응답 타입: {type(res)}")
