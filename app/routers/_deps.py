"""FastAPI Depends 헬퍼 — app.state 에서 서비스 추출.

모든 라우터는 이 모듈의 Depends 함수를 통해 서비스 인스턴스에 접근한다.
"""

from __future__ import annotations

from fastapi import Request

from app.services.history_service import HistoryService
from app.services.af3_client import AF3MCPClient
from app.services.openmm_client import OpenMMMCPClient
from app.services.llm_service import LLMService
from app.services.library_service import LibraryService
from app.services.smina_service import SminaService
from app.services.scoring_service import ScoringService
from app.services.ranking_service import RankingService
from app.services.target_service import TargetService
from app.services.screening_service import ScreeningService
from app.services.graphrag_service import GraphRAGService


def get_history(request: Request) -> HistoryService:
    """app.state.history 반환."""
    return request.app.state.history


def get_af3(request: Request) -> AF3MCPClient:
    """app.state.af3 반환."""
    return request.app.state.af3


def get_openmm(request: Request) -> OpenMMMCPClient:
    """app.state.openmm 반환."""
    return request.app.state.openmm


def get_llm(request: Request) -> LLMService:
    """app.state.llm 반환."""
    return request.app.state.llm


def get_library(request: Request) -> LibraryService:
    """app.state.library 반환."""
    return request.app.state.library


def get_smina(request: Request) -> SminaService:
    """app.state.smina 반환."""
    return request.app.state.smina


def get_scoring(request: Request) -> ScoringService:
    """app.state.scoring 반환."""
    return request.app.state.scoring


def get_ranking(request: Request) -> RankingService:
    """app.state.ranking 반환."""
    return request.app.state.ranking


def get_target(request: Request) -> TargetService:
    """app.state.target 반환."""
    return request.app.state.target


def get_screening(request: Request) -> ScreeningService:
    """app.state.screening 반환."""
    return request.app.state.screening


def get_graphrag(request: Request) -> GraphRAGService:
    """app.state.graphrag 반환."""
    return request.app.state.graphrag
