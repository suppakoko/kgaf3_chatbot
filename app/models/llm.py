"""LLM 응답 Pydantic 모델.

LLM_plan.md §5.4 / §4.4 권위.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ScreeningSummary(BaseModel):
    """Stage 7 LLM 스크리닝 요약 결과.

    LLM_plan.md §5.4 SummarySchema.
    """

    headline: str
    highlights: list[str]
    table_md: str
    caveats: list[str]
    next_actions: list[str]


class IntentResult(BaseModel):
    """사용자 의도 분석 결과.

    Phase 4 에서 사용. Stage 7 에서는 미사용.
    LLM_plan.md §4.4 참조.
    """

    task_type: Literal[
        "general_qa",
        "start_screening",
        "import_af3_results",
        "result_question",
        "change_model",
        "cancel_job",
        "clarify",
    ]
    params: dict = Field(default_factory=dict)
    missing: list[str] = []
    rationale: str
