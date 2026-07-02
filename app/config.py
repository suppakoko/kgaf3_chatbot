"""Application settings (pydantic-settings)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 5013
    log_level: str = "INFO"
    log_format: str = "json"  # json | console

    # OpenRouter (LLM)
    openrouter_api_key: str = ""  # empty 허용 (Phase 1 헬스체크는 통과)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_default_model: str = "anthropic/claude-sonnet-4-6"
    llm_allowed_models: str = (
        "anthropic/claude-sonnet-4-6,"
        "anthropic/claude-opus-4-7,"
        "openai/gpt-4o-mini,"
        "google/gemini-pro-1.5,"
        "meta-llama/llama-3.3-70b-instruct"
    )
    openrouter_app_name: str = "afmm_chat"
    openrouter_http_referer: str = "http://localhost:5013"

    # MCP Endpoints
    af3_mcp_url: str = "http://127.0.0.1:8002/mcp/"
    af3_mcp_auth_token: str = ""
    openmm_mcp_url: str = "http://127.0.0.1:8001/mcp/"
    openmm_mcp_auth_token: str = ""

    # GraphRAG (optional Neo4j knowledge graph, via bundled graphrag-mcp-server over SSE)
    # 활성 시 afmm_chat 은 self-contained Docker 스택(yoonjuho94/graphrag-neo4j:1.0 +
    # yoonjuho94/graphrag-mcp-server:1.0)이 노출하는 MCP SSE 서버에 붙는다.
    # KG 데이터·Neo4j·LLM 키는 모두 MCP 서버 컨테이너 안에 있다.
    graphrag_enabled: bool = False
    graphrag_mcp_url: str = "http://graphrag-mcp:8893/sse"
    graphrag_mcp_auth_token: str = ""
    graphrag_default_provider: str = "openrouter"
    # 표시/이력용 메타 — 실제 GraphRAG LLM 모델은 MCP 서버의 .env(OPENROUTER_MODEL)가 결정.
    graphrag_openrouter_model: str = "anthropic/claude-opus-4-7"

    # Storage
    afmm_db_path: Path = Field(default=Path("data/afmm.db"))
    afmm_library_dir: Path = Field(default=Path("data/uploads"))
    af3_output_root: Path = Field(default=Path("data/af3_output"))
    openmm_work_root: Path = Field(default=Path("data/openmm_runs"))

    # Auth
    afmm_api_token: str = ""

    # Scoring
    score_weight_iptm: float = 0.45
    score_weight_pae: float = 0.25
    score_weight_energy: float = 0.30
    iptm_floor: float = 0.55

    # Pipeline mode — Stage 5 rescoring engine
    # False(기본): smina --minimize (OpenMM 제거). True: 기존 OpenMM minimize+rescore(롤백/비교용).
    use_openmm: bool = False
    # smina --minimize 옵션
    smina_scoring: str = "vinardo"     # "vinardo" | "vina"(기존 default 연속성)
    smina_minimize_iters: int = 0      # 0 = 수렴까지 (smina 기본)
    smina_max_concurrency: int = 4     # per-ligand 병렬 smina 동시 실행 수 (CPU)

    # Budget (LLM cost protection)
    session_token_budget: int = 200_000
    llm_daily_token_quota: int = 100_000
    llm_daily_usd_quota: float = 5.0

    # GPU informational
    gpu_device_id: int = 0

    @property
    def allowed_models(self) -> list[str]:
        return [m.strip() for m in self.llm_allowed_models.split(",") if m.strip()]


settings = Settings()
