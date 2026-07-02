"""AF3 MCP 서버 — NP_web_ui 통합용.

실행: uv run python -m app.mcp.af3_mcp_server
"""

import asyncio
import json
import logging
import sys
import uuid

logger = logging.getLogger(__name__)


def create_mcp_server():
    """MCP 서버 생성."""
    try:
        from mcp.server import Server
        from mcp.types import TextContent, Tool
    except ImportError:
        logger.error("mcp 패키지가 설치되지 않았습니다. `uv add mcp` 로 설치해주세요.")
        sys.exit(1)

    server = Server("af3-chatbot")

    # 서비스 인스턴스 (MCP 프로세스 내에서 독립 실행)
    _af3_service = None
    _builder = None

    async def _get_services():
        nonlocal _af3_service, _builder
        if _af3_service is None:
            from app.services.af3_service import AF3Service
            from app.services.json_builder import JsonBuilder
            _af3_service = AF3Service()
            await _af3_service.initialize()
            _builder = JsonBuilder()
        return _af3_service, _builder

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="af3_create_job",
                description="AlphaFold3 input JSON을 생성합니다. 단백질 서열과 리간드 SMILES를 입력하면 AF3 실행용 JSON을 만듭니다.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "protein_sequence": {
                            "type": "string",
                            "description": "단백질 아미노산 서열",
                        },
                        "ligand_smiles": {
                            "type": "string",
                            "description": "리간드 SMILES 문자열 (선택)",
                        },
                        "ligand_ccd": {
                            "type": "string",
                            "description": "리간드 CCD 코드 (선택, SMILES와 상호 배타)",
                        },
                        "job_name": {
                            "type": "string",
                            "description": "작업 이름",
                            "default": "mcp_job",
                        },
                        "model_seeds": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "모델 시드 배열",
                            "default": [1, 10, 100, 1000],
                        },
                    },
                    "required": ["protein_sequence"],
                },
            ),
            Tool(
                name="af3_run_job",
                description="생성된 AF3 input JSON으로 AlphaFold3를 실행합니다.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "input_json": {
                            "type": "string",
                            "description": "AF3 input JSON 문자열",
                        },
                        "run_mode": {
                            "type": "string",
                            "enum": ["full", "data_pipeline_only", "inference_only"],
                            "default": "full",
                        },
                    },
                    "required": ["input_json"],
                },
            ),
            Tool(
                name="af3_get_status",
                description="AF3 작업 상태를 조회합니다.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "작업 ID"},
                    },
                    "required": ["job_id"],
                },
            ),
            Tool(
                name="af3_get_results",
                description="AF3 작업 결과 (신뢰도 요약)를 조회합니다.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "작업 ID"},
                    },
                    "required": ["job_id"],
                },
            ),
            Tool(
                name="af3_list_jobs",
                description="AF3 작업 목록을 조회합니다.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "description": "필터링할 상태 (선택)",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 10,
                        },
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        af3_service, builder = await _get_services()

        if name == "af3_create_job":
            entities = [{"type": "protein", "sequence": arguments["protein_sequence"]}]
            if smiles := arguments.get("ligand_smiles"):
                entities.append({"type": "ligand", "smiles": smiles})
            elif ccd := arguments.get("ligand_ccd"):
                entities.append({"type": "ligand", "ccd_codes": [ccd]})

            job_name = arguments.get("job_name", "mcp_job")
            seeds = arguments.get("model_seeds", [1, 10, 100, 1000])
            input_json = builder.build_from_entities(entities, job_name, seeds)

            is_valid, errors = builder.validate(input_json)
            if not is_valid:
                return [TextContent(type="text", text=f"검증 실패: {errors}")]

            return [TextContent(type="text", text=json.dumps(input_json, indent=2))]

        elif name == "af3_run_job":
            input_json = json.loads(arguments["input_json"])
            run_mode = arguments.get("run_mode", "full")
            job_id = str(uuid.uuid4())

            is_valid, errors = builder.validate(input_json)
            if not is_valid:
                return [TextContent(type="text", text=f"검증 실패: {errors}")]

            job = await af3_service.submit_job(job_id, input_json, run_mode)
            return [TextContent(
                type="text",
                text=f"작업 등록됨: {job.job_id}\n상태: {job.status}\n이름: {job.name}",
            )]

        elif name == "af3_get_status":
            job = await af3_service.get_job(arguments["job_id"])
            if not job:
                return [TextContent(type="text", text="작업을 찾을 수 없습니다.")]
            return [TextContent(type="text", text=json.dumps({
                "job_id": job.job_id,
                "name": job.name,
                "status": job.status,
                "error_message": job.error_message,
                "created_at": job.created_at.isoformat(),
            }, ensure_ascii=False, indent=2))]

        elif name == "af3_get_results":
            from app.services.result_service import ResultService
            result_svc = ResultService()
            result = await result_svc.get_result_summary(arguments["job_id"], af3_service)
            if not result:
                return [TextContent(type="text", text="결과를 찾을 수 없습니다.")]
            return [TextContent(type="text", text=json.dumps(
                result.model_dump(), ensure_ascii=False, indent=2,
            ))]

        elif name == "af3_list_jobs":
            limit = arguments.get("limit", 10)
            jobs = await af3_service.list_jobs(limit)
            status_filter = arguments.get("status")
            if status_filter:
                jobs = [j for j in jobs if j.status == status_filter]
            job_list = [{
                "job_id": j.job_id,
                "name": j.name,
                "status": j.status,
                "created_at": j.created_at.isoformat(),
            } for j in jobs]
            return [TextContent(type="text", text=json.dumps(job_list, ensure_ascii=False, indent=2))]

        return [TextContent(type="text", text=f"알 수 없는 도구: {name}")]

    return server


async def main():
    from mcp.server.stdio import stdio_server
    server = create_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
