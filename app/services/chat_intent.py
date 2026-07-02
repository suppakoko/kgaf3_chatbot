"""채팅 메시지에서 docking intent + FASTA + SMILES 추출.

LLM 호출 없이 정규식/휴리스틱 기반 1차 추출. 매칭 실패 시 일반 LLM 채팅으로 폴백.
사용자 입력 예시:
    >ROCK1_HUMAN_1-415 ...
    MSTGD...NDNR

    CN(C)... Chroman_1
    C[C@H]... H1152
    C[C@H]... Y-27632

    위 단백질에 화합물 3개를 docking 해서 순위를 매겨주세요
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# docking/screening intent 키워드 (다국어 일부)
_INTENT_KEYWORDS = re.compile(
    r"(docking|도킹|screening|스크리닝|순위|랭킹|ranking|결합|affinity|"
    r"虚拟筛选|ドッキング|virtual\s*screen)",
    re.IGNORECASE,
)

# FASTA: '>' 헤더 + 그 아래 아미노산 시퀀스 (대문자 + 일부 기호)
# 멀티라인 시퀀스 합쳐 하나로 추출
_FASTA_BLOCK = re.compile(
    r"^>\s*([^\n]+)\n((?:[A-Za-z\*\-\.\s]+\n?)+)",
    re.MULTILINE,
)

# 단일 시퀀스 라인만 (헤더 없는 경우): 30+ 글자 연속 아미노산 단문자
_AA_INLINE = re.compile(r"\b([A-IK-NP-Z]{30,})\b")

# SMILES + 선택적 name 라인:
#   문자: A-Z a-z 0-9 + 결합기호 ( ) [ ] = # @ + - / \ % .
#   end: 공백 + 이름 (영문/숫자/_/-) 옵션
_SMILES_LINE = re.compile(
    r"^\s*([A-Za-z0-9@+\-\[\]\(\)=#/\\%.]+)(?:\s+([A-Za-z0-9_\-\.]+))?\s*$"
)


@dataclass
class DockingIntent:
    """추출된 docking 작업 명세."""
    target_sequence: str
    target_name: str | None
    ligands: list[tuple[str, str]]  # [(smiles, name), ...]
    raw_text: str


@dataclass
class DockingIntentMiss:
    """detect_docking_intent 가 None 을 반환했을 때, 어떤 조건이 부족했는지 진단.

    has_intent_keyword: docking/도킹/순위 등 docking-intent 키워드 매칭 여부
    has_target_sequence: FASTA 블록 또는 30+자 AA 라인이 발견됐는지
    has_ligands: SMILES 후보 라인이 1개 이상 발견됐는지
    found_ligand_count: 추출된 SMILES 후보 개수 (0 이면 없음)
    detected_seq_len: 발견된 단백질 시퀀스 길이 (없으면 None)
    """
    has_intent_keyword: bool
    has_target_sequence: bool
    has_ligands: bool
    found_ligand_count: int
    detected_seq_len: int | None


def detect_docking_intent(text: str) -> DockingIntent | None:
    """채팅 메시지에서 FASTA + SMILES + docking 의도 동시 충족 시 DockingIntent 반환.

    엄격한 조건:
    - docking 키워드 1개 이상
    - FASTA(또는 충분히 긴 AA 라인) 1개 이상
    - SMILES 라인 1개 이상
    """
    if not _INTENT_KEYWORDS.search(text):
        return None

    # 단일-라인 페이스트(Enter 없이 한 줄로 붙여넣은 경우) 정규화.
    # 멀티-라인 입력은 변경하지 않음.
    text = _normalize_single_line_blob(text)

    target_sequence, target_name = _extract_target_sequence(text)
    if not target_sequence:
        return None

    ligands = _extract_ligands(text)
    if not ligands:
        return None

    return DockingIntent(
        target_sequence=target_sequence,
        target_name=target_name,
        ligands=ligands,
        raw_text=text,
    )


def diagnose_docking_intent(text: str) -> DockingIntentMiss:
    """detect_docking_intent 가 None 을 반환했을 때 어떤 조건이 빠졌는지 보고.

    chat.py 의 AFMM 모드 핸들러가 intent 감지 실패 시 사용자에게
    "무엇이 부족한지" 정확히 안내하기 위함.

    Note: 본 함수는 부작용 없이 진단 정보만 반환한다. 호출자는 결과를
    바탕으로 안내 메시지를 생성한다.
    """
    has_keyword = bool(_INTENT_KEYWORDS.search(text))
    normalized = _normalize_single_line_blob(text)
    seq, _ = _extract_target_sequence(normalized)
    ligands = _extract_ligands(normalized)
    return DockingIntentMiss(
        has_intent_keyword=has_keyword,
        has_target_sequence=bool(seq),
        has_ligands=bool(ligands),
        found_ligand_count=len(ligands),
        detected_seq_len=len(seq) if seq else None,
    )


# 정규화: 한 줄로 붙여넣은 입력을 멀티라인 형태로 재구성
_AA_TOKEN_RE = re.compile(r"^[A-IK-NP-Z]+$")
_NAME_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")
_SMILES_SYNTAX_CHARS = "()[]@=#\\/+-."
# Structural SMILES chars — 화합물 이름(Y-27632, Chroman_1, H1152, Fasudil) 에는
# 거의 나타나지 않는 토큰. 이게 있으면 "확실히 SMILES", 없으면 모호.
# 주의: '+' 와 '-' 는 이름에도 등장(예: Y-27632) → 구조 판정에서 제외.
_DEFINITE_SMILES_CHARS = set("()[]=#/\\.%@")


def _looks_like_aa_stretch(token: str, min_len: int = 30) -> bool:
    """토큰이 ≥30자 순수 AA 단문자(대문자) 인지."""
    if len(token) < min_len:
        return False
    up = token.upper()
    return _AA_TOKEN_RE.match(up) is not None


_SMILES_CHARSET_FULL_RE = re.compile(r"^[A-Za-z0-9\[\]\(\)=#/\\:@+\-.%]+$")


def _looks_like_smiles(token: str, min_len: int = 2) -> bool:
    """토큰이 SMILES 일 가능성 — 단일 라인 paste 정규화에서만 사용.

    조건 (strict):
    - `_passes_smiles_basics` 통과 (charset, lowercase atom 룰, alpha-uppercase 룰).
    - AND structural char `()[]=#/\\.%@` 포함 OR atom-only 알파벳 토큰(`CCO`, `CN`).
    - `Y-27632`/`Fasudil`/`H1152` 같은 이름은 lowercase 룰(`a`/`u`/`d`)이나 dash-only
      조건에 걸려 False.
    """
    if len(token) < min_len:
        return False
    if not _passes_smiles_basics(token):
        return False
    if any(c in _DEFINITE_SMILES_CHARS for c in token):
        return True
    # structural char 없음 → 알파벳-only 대문자 시작만 SMILES (CCO/CN/O 등)
    return token.isalpha() and token[0].isupper()


def _normalize_single_line_blob(text: str) -> str:
    """한 줄로 붙여넣은 FASTA+SMILES+지시문 텍스트를 멀티라인으로 재구성.

    조건: text 에 '>' 가 있고 newline 이 3 개 미만일 때만 정규화.
    그 외는 원본 반환 (기존 멀티라인 경로 유지).

    재구성 결과:
        >HEADER (헤더 토큰들)
        <AA-stretch들 concat>

        <SMILES1> <name1>
        <SMILES2> <name2>
        ...

        <남은 지시문 텍스트>
    """
    if ">" not in text:
        return text
    if text.count("\n") >= 3:
        return text

    tokens = text.split()
    if not tokens:
        return text

    # 첫 '>' 토큰 찾기
    header_idx = None
    for i, tok in enumerate(tokens):
        if tok.startswith(">"):
            header_idx = i
            break
    if header_idx is None:
        return text

    # 헤더 토큰들: '>' 부터 첫 AA-stretch 직전까지
    aa_start = None
    for i in range(header_idx, len(tokens)):
        if _looks_like_aa_stretch(tokens[i]):
            aa_start = i
            break
    if aa_start is None:
        # AA-stretch 없으면 정규화 의미 없음
        return text

    header_tokens = tokens[header_idx:aa_start]
    header_line = " ".join(header_tokens)  # '>' 포함

    # AA-stretch 연속 흡수
    aa_end = aa_start
    aa_chunks: list[str] = []
    while aa_end < len(tokens) and _looks_like_aa_stretch(tokens[aa_end]):
        aa_chunks.append(tokens[aa_end].upper())
        aa_end += 1
    aa_concat = "".join(aa_chunks)

    # 나머지 토큰 = SMILES + name 페어 + 지시문
    rest = tokens[aa_end:]
    ligand_lines: list[str] = []
    instruction_tokens: list[str] = []
    i = 0
    seen_non_ligand = False
    while i < len(rest):
        tok = rest[i]
        # SMILES 후보 인지 검사
        if not seen_non_ligand and _looks_like_smiles(tok):
            # 다음 토큰이 이름 형식이면 페어
            name = None
            if i + 1 < len(rest) and _NAME_TOKEN_RE.match(rest[i + 1]):
                # 다음 토큰이 또 SMILES (괄호 포함) 이면 이름이 아님
                nxt = rest[i + 1]
                if not _looks_like_smiles(nxt):
                    name = nxt
            if name is not None:
                ligand_lines.append(f"{tok} {name}")
                i += 2
            else:
                ligand_lines.append(tok)
                i += 1
        else:
            # SMILES 가 아닌 토큰을 만나면, 이후는 모두 지시문으로 간주.
            seen_non_ligand = True
            instruction_tokens.append(tok)
            i += 1

    parts: list[str] = []
    parts.append(header_line)
    parts.append(aa_concat)
    parts.append("")  # blank
    if ligand_lines:
        parts.extend(ligand_lines)
        parts.append("")
    if instruction_tokens:
        parts.append(" ".join(instruction_tokens))

    return "\n".join(parts)


def _extract_target_sequence(text: str) -> tuple[str | None, str | None]:
    """첫 FASTA 블록 또는 inline AA 시퀀스 추출.

    1) '>' 토큰을 라인 시작이 아닌 *임의 위치* 에서도 찾는다
       (사용자가 SMILES 라인 끝에 ``>HEADER`` 를 붙인 경우 대응).
    2) 헤더 다음 줄부터 AA-only 라인을 연속 흡수.
       비-ASCII 문자(한글) 또는 따옴표가 섞인 경우 *접두 ASCII-알파벳 부분만* 채택하고,
       그 라인 이후로 종료.
    3) 헤더가 없으면, 텍스트 전체에서 *연속된 AA-only 라인 블록* 을 모아 가장 긴 것을 반환.
    """
    # 1) '>' 토큰 위치 탐색 (line start OR whitespace 직후)
    m = re.search(r"(?:^|\s)>\s*([^\n]+)", text)
    if m:
        header = m.group(1).strip().rstrip("'\"")
        # 헤더 라인의 끝(개행) 위치
        nl = text.find("\n", m.start())
        tail = text[nl + 1 :] if nl != -1 else ""
        seq_lines: list[str] = []
        for ln in tail.splitlines():
            cand = ln.strip()
            if not cand:
                break
            # 라인 내 첫 비-ASCII 또는 따옴표 이전까지만 채택
            ascii_part_chars: list[str] = []
            for c in cand:
                if ord(c) > 127 or c in "'\"":
                    break
                ascii_part_chars.append(c)
            ascii_part = "".join(ascii_part_chars).strip()
            cleaned = "".join(c for c in ascii_part if c.isalpha())
            if len(cleaned) < 5:
                # 거의 모든 문자가 비-AA → 블록 종료
                break
            seq_lines.append(cleaned.upper())
            # ascii_part 가 잘린 라인이면 (뒤에 한글/따옴표) 마지막 라인으로 처리하고 종료
            if len(ascii_part) < len(cand):
                break
        seq = "".join(seq_lines)
        if len(seq) >= 30:
            return seq, header

    # 2) 헤더 없음 → 연속 AA-only 라인 블록을 모아 가장 긴 것
    blocks: list[str] = []
    current: list[str] = []
    aa_re = re.compile(r"^[A-IK-NP-Z]+$")
    for ln in text.splitlines():
        cand = ln.strip()
        if cand and cand.isalpha() and aa_re.match(cand.upper()):
            current.append(cand.upper())
        else:
            if current:
                blocks.append("".join(current))
                current = []
    if current:
        blocks.append("".join(current))
    if blocks:
        best = max(blocks, key=len)
        if len(best) >= 30:
            return best, None

    # 3) 최후 fallback: 단일 라인 inline 매칭
    inline_matches = _AA_INLINE.findall(text)
    if inline_matches:
        best = max(inline_matches, key=len)
        if len(best) >= 30:
            return best.upper(), None

    return None, None


def _passes_smiles_basics(token: str) -> bool:
    """SMILES 후보 기본 검증 (charset, lowercase atom 룰, 길이).

    structural char 유무는 보지 않는다 — 호출자가 별도로 판정.
    """
    if len(token) < 2:
        return False
    if any(ord(c) > 127 for c in token):
        return False
    if not _SMILES_CHARSET_FULL_RE.match(token):
        return False
    # 알파벳-only 라면 첫 문자 대문자 (CCO ok, hello no)
    if token.isalpha() and not token[0].isupper():
        return False
    # 비-bracket 영역 lowercase 는 aromatic atom + Cl/Br/Si 접미사만 허용
    valid_lower = set("bcnopslri")
    in_bracket = False
    for c in token:
        if c == "[":
            in_bracket = True
            continue
        if c == "]":
            in_bracket = False
            continue
        if in_bracket:
            continue
        if c.islower() and c not in valid_lower:
            return False
    return True


def _is_definite_smiles(token: str) -> bool:
    """토큰이 "확실히" SMILES 인지 — structural char 1 개 이상 + 기본 검증 통과.

    structural char = `()[]=#/\\.%@` (이름 토큰에는 거의 등장하지 않는 문자).
    `Y-27632` 같은 이름은 `-` 만 있고 structural char 가 없어 False.
    `CC(N)C1CCC(CC1)C(=O)Nc1ccncc1` 는 `(`, `)`, `=` 가 있어 True.
    """
    if not any(c in _DEFINITE_SMILES_CHARS for c in token):
        return False
    return _passes_smiles_basics(token)


def _looks_like_name_token(token: str) -> bool:
    """토큰이 화합물 이름 후보인지 — name_re 통과 + structural char 없음.

    structural char 가 있으면 SMILES 일 가능성이 높으므로 이름으로 간주하지 않음.
    """
    if not _NAME_TOKEN_RE.match(token):
        return False
    if any(c in _DEFINITE_SMILES_CHARS for c in token):
        return False
    return True


def _extract_ligands(text: str) -> list[tuple[str, str]]:
    """SMILES + name 라인 추출.

    한 라인에 여러 (SMILES, name) 페어가 공백 구분으로 나열된 경우도 모두 추출.
    예: `SMI1 name1 SMI2 name2 SMI3 name3` → 3 개 모두 추출.

    토큰 분류:
      - "definite SMILES": structural char (`()[]=#/\\.%@`) 포함 + 기본 검증 통과.
      - "name token": `[A-Za-z0-9_\\-\\.]+` 매칭 + structural char 없음.
      - "ambiguous": 위 둘 다 아님 — 보통 영문 단어 / 한국어 / 지시문 단편.

    수락 규칙:
      - definite SMILES 발견 시 → 다음 토큰이 name 이면 페어, 아니면 단독.
      - 단독 alpha-only 토큰(`CCO`, `O`)은 다음 토큰이 name 일 때만 수락
        (일반 영문 단어와 구분 불가).
      - name-만 있는 토큰(`Y-27632`, `Fasudil`)은 앞 SMILES 없으면 무시.
    """
    ligands: list[tuple[str, str]] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        # 라인 내에서 '>' 또는 비-ASCII (한글) 또는 따옴표 *이전 부분만* 사용:
        # 사용자가 SMILES 끝에 한글/FASTA 헤더를 붙인 경우 대응.
        cut = len(line)
        for i, c in enumerate(line):
            if c == ">" or c in "'\"" or ord(c) > 127:
                cut = i
                break
        head = line[:cut].strip()
        if len(head) < 2:
            continue

        parts = head.split()
        if not parts:
            continue

        i = 0
        while i < len(parts):
            tok = parts[i]
            nxt = parts[i + 1] if i + 1 < len(parts) else None
            nxt_is_name = nxt is not None and _looks_like_name_token(nxt)

            accept_smiles = False
            if _is_definite_smiles(tok):
                accept_smiles = True
            elif nxt_is_name and _passes_smiles_basics(tok):
                # ambiguous SMILES (CCO/CN/O) 가 명시적 name 토큰을 동반 → 수락
                accept_smiles = True

            if not accept_smiles:
                i += 1
                continue

            smiles = tok
            if nxt_is_name:
                name = nxt
                i += 2
            else:
                name = f"ligand_{len(ligands) + 1}"
                i += 1

            if smiles in seen:
                continue
            seen.add(smiles)
            ligands.append((smiles, name))

    return ligands


def build_smi_blob(ligands: list[tuple[str, str]]) -> bytes:
    """LibraryService.parse 가 받을 수 있는 .smi 파일 바이트 생성."""
    lines = [f"{smi} {name}" for smi, name in ligands]
    return ("\n".join(lines) + "\n").encode("utf-8")
