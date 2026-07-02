"""복합 스코어링 서비스 — 순수 함수, DB 호출 없음.

pipeline_design.md §6 composite formula 권위.

Canonical 3-term formula (weights.smina == 0):
    composite = 0.45·ranking_score + 0.25·(1 - PAE/30) + 0.30·sigmoid(-E_inter/50)

AF3 confidence term: ranking_score (AF3 internal best-sample score, iptm+ptm+clash+disorder 종합).
ranking_score 결측 시 iptm 으로 fallback. floor 검사도 동일.

선택적 4-term smina blend (weights.smina > 0):
    smina_term = sigmoid(-smina_affinity / 5)   [smina ~ -3 to -12 kcal/mol]
    나머지 3항의 가중치를 (1 - weights.smina) 비율로 조정(pro-rate).
"""

from __future__ import annotations

import math

import structlog
from pydantic import BaseModel, model_validator

log = structlog.get_logger("service.scoring")


# ── 가중치 모델 ───────────────────────────────────────────────────────────────

class ScoreWeights(BaseModel):
    """복합 스코어 가중치.

    기본값 (canonical 3-term):
        iptm=0.45, pae=0.25, inter=0.30, smina=0.0  합계=1.0
    smina > 0 이면 4-term blended 모드로 전환.
    """

    iptm: float = 0.45
    pae: float = 0.25
    inter: float = 0.30    # OpenMM e_interaction sigmoid term
    smina: float = 0.0     # 기본 0; 사용자가 재조정 가능

    @model_validator(mode="after")
    def _check_sum(self) -> "ScoreWeights":
        total = self.iptm + self.pae + self.inter + self.smina
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"ScoreWeights must sum to 1.0, got {total:.4f}. "
                "Adjust iptm/pae/inter/smina proportionally."
            )
        return self


# ── 스코어링 서비스 ───────────────────────────────────────────────────────────

class ScoringService:
    """복합 스코어 계산 서비스.

    순수 함수 집합; DB 호출 없음.
    lifespan 에서 app.state.scoring 으로 등록.

    Args:
        weights: ScoreWeights 인스턴스.
        iptm_floor: ipTM 하한. 이 값 미만이면 None 반환.
    """

    def __init__(
        self,
        weights: ScoreWeights | None = None,
        iptm_floor: float = 0.55,
    ) -> None:
        self._weights = weights or ScoreWeights()
        self._iptm_floor = iptm_floor

    @property
    def weights(self) -> ScoreWeights:
        return self._weights

    @property
    def iptm_floor(self) -> float:
        return self._iptm_floor

    # ── 공개 메서드 ───────────────────────────────────────────────────────────

    def composite_score(
        self,
        af3: dict,
        openmm: dict,
        smina: dict | None = None,
        mode: str = "canonical",
    ) -> float | None:
        """복합 스코어 계산.

        Args:
            af3: {"iptm": float, "mean_pae": float}
            openmm: {"e_interaction": float}  (kJ/mol)
            smina: {"affinity_kcal_mol": float} | None
            mode: "canonical" | "smina_only" | "combined"
                - canonical: 3-term (weights.smina 무시)
                - smina_only: smina affinity sigmoid 만 반환
                - combined: weights 기반 4-term blend

        Returns:
            [0, 1] 범위 복합 스코어 또는 None (ranking_score floor 미달 / 데이터 부족).
        """
        # AF3 confidence term: ranking_score 우선, 결측 시 iptm fallback
        af3_conf = _safe_float(af3.get("ranking_score"))
        if af3_conf is None:
            af3_conf = _safe_float(af3.get("iptm"))

        # AF3 confidence floor 검사 (ranking_score 또는 iptm)
        if af3_conf is None or af3_conf < self._iptm_floor:
            log.debug(
                "scoring.af3_conf_below_floor",
                af3_conf=af3_conf,
                floor=self._iptm_floor,
            )
            return None

        if mode == "smina_only":
            return self._smina_only(smina)

        # OpenMM 부재(smina --minimize 모드) 견고화: e_interaction 결측이지만
        # smina affinity 가 있으면 smina_only 로 폴백해 composite 가 NULL 로만 차지 않게 함.
        if _safe_float(openmm.get("e_interaction")) is None and smina is not None \
                and _safe_float(smina.get("affinity_kcal_mol")) is not None:
            return self._smina_only(smina)

        if mode == "combined":
            return self._combined(af3_conf, af3, openmm, smina)

        # canonical (default)
        return self._canonical(af3_conf, af3, openmm)

    # ── 내부 스코어 계산 ──────────────────────────────────────────────────────

    def _canonical(self, af3_conf: float, af3: dict, openmm: dict) -> float | None:
        """3-term canonical 공식. af3_conf 는 ranking_score 우선, iptm fallback."""
        pae = _safe_float(af3.get("mean_pae"))
        e_inter = _safe_float(openmm.get("e_interaction"))

        if pae is None or e_inter is None:
            return None

        w = self._weights
        # 가중치가 0이면 canonical이 의미 없으므로 기본값 사용
        w_af3 = w.iptm if w.iptm > 0 else 0.45  # iptm 가중치 = AF3 confidence 가중치 (ranking_score 자리)
        w_pae  = w.pae  if w.pae  > 0 else 0.25
        w_inter = w.inter if w.inter > 0 else 0.30

        score = (
            w_af3  * af3_conf
            + w_pae  * (1.0 - pae / 30.0)
            + w_inter * _sigmoid(-e_inter / 50.0)
        )
        return float(max(0.0, min(1.0, score)))

    def _smina_only(self, smina: dict | None) -> float | None:
        """smina affinity sigmoid 만 반환."""
        if smina is None:
            return None
        aff = _safe_float(smina.get("affinity_kcal_mol"))
        if aff is None:
            return None
        return float(_sigmoid(-aff / 5.0))

    def _combined(
        self,
        af3_conf: float,
        af3: dict,
        openmm: dict,
        smina: dict | None,
    ) -> float | None:
        """4-term blended: weights.smina > 0 이면 기존 3항 pro-rate.

        af3_conf 는 ranking_score 우선, iptm fallback.
        """
        w = self._weights

        if w.smina <= 0.0 or smina is None:
            # smina 가중치 없거나 데이터 없으면 canonical 로 폴백
            return self._canonical(af3_conf, af3, openmm)

        pae = _safe_float(af3.get("mean_pae"))
        e_inter = _safe_float(openmm.get("e_interaction"))
        aff = _safe_float(smina.get("affinity_kcal_mol"))

        if pae is None or e_inter is None or aff is None:
            # 불완전 데이터: canonical 폴백
            return self._canonical(af3_conf, af3, openmm)

        # 나머지 3항을 (1 - w.smina) 비율로 pro-rate
        scale = 1.0 - w.smina
        # 원래 3항 비율: af3_conf:pae:inter = 0.45:0.25:0.30 → 합계 1.0
        # 현재 weights 합이 1이므로 smina 제외 3항 합 = scale
        w_af3   = w.iptm  / scale if scale > 0 else 0.45
        w_pae   = w.pae   / scale if scale > 0 else 0.25
        w_inter = w.inter / scale if scale > 0 else 0.30

        smina_term = _sigmoid(-aff / 5.0)

        score = (
            (1.0 - w.smina) * (
                w_af3   * af3_conf
                + w_pae  * (1.0 - pae / 30.0)
                + w_inter * _sigmoid(-e_inter / 50.0)
            )
            + w.smina * smina_term
        )
        log.debug(
            "scoring.combined",
            af3_conf=round(af3_conf, 3),
            pae=round(pae, 2),
            e_inter=round(e_inter, 2),
            smina_aff=round(aff, 3),
            score=round(score, 4),
        )
        return float(max(0.0, min(1.0, score)))


# ── 수학 유틸 ─────────────────────────────────────────────────────────────────

def _sigmoid(x: float) -> float:
    """표준 시그모이드 σ(x) = 1 / (1 + e^(-x))."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _safe_float(val: object) -> float | None:
    """값을 float 로 변환. 실패 시 None."""
    if val is None:
        return None
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
