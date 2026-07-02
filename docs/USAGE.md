# USAGE — kgaf3_chatbot 사용 가이드

kgaf3_chatbot은 **하나의 웹 UI 안에 두 개의 모드**를 담고 있다:

- 🧬 **GraphRAG** — 천연물 지식그래프(NPASS 3.0 + Open Targets 25.12,
  **273,519 노드 / 1,493,463 관계**)에 **자연어로 질의**하는 모드.
- 🧪 **가상 스크리닝(AlphaFold)** — 단백질–리간드 **cofolding**(외부 AF3) +
  번들 `smina --minimize` 재점수화로 **후보를 순위화**하는 모드.

설치는 `docs/INSTALL.md`, GraphRAG 스택 자체의 배포·검증은 `docs/GRAPHRAG.md`,
외부 AF3 준비는 `docs/AF3_SETUP.md`(및 `docs/BRIDGE.md`)를 참고하라. 이 문서는
설치가 끝난 뒤 **브라우저에서 어떻게 쓰는가**를 다룬다.

---

## 0. 접속과 화면 구성

설치가 성공하면 브라우저에서 웹 UI를 연다(포트는 `configure.md`의 `APP_PORT`,
기본 `5013`).

```text
http://localhost:5013
```

화면 상단 헤더에는 **모드 토글**이 있다. 두 개의 탭 버튼으로 되어 있다:

- **🧪 AlphaFold** — 가상 스크리닝 모드. **기본 선택**(앱 첫 진입 시 활성).
- **🧬 GraphRAG** — 지식그래프 질의 모드.

모드 선택은 브라우저 `localStorage`에 저장되므로, 다음 방문 때 마지막으로
쓰던 모드가 그대로 열린다. 헤더 오른쪽에는 **LLM 모델 선택 드롭다운**
(기본 `anthropic/claude-sonnet-4-6`, `configure.md`의 `LLM_DEFAULT_MODEL`)과
연결 상태 표시가 있다.

화면 맨 아래 **시스템 바**에는 각 백엔드의 상태 칩이 있다:
`AF3`(외부 AF3 MCP), `Smina`(번들 도킹 MCP), `GraphRAG`(Neo4j/MCP), `LLM`,
`jobs`(진행 중 작업 수), GPU 사용률. 모드를 쓰기 전에 이 칩들로 필요한
백엔드가 붙어 있는지 먼저 확인하면 좋다.

> 모드에 따라 입력창 안내문(placeholder)과 환영 패널의 예시가 바뀐다.
> 왼쪽 사이드바의 **+ 새 대화**로 대화를 새로 시작할 수 있고, 최근 대화
> 목록에서 이전 세션을 다시 열 수 있다.

---

## 1. 🧬 GraphRAG 모드 — 지식그래프 자연어 질의

### 1-1. 무엇을 물어볼 수 있나

GraphRAG는 천연물–타겟–질병 지식그래프에 대해 **Text2Cypher**로 답한다.
질문을 자연어로 입력하면, 백엔드가 그 질문을 **Cypher 쿼리로 변환 → Neo4j에서
실행 → 결과를 Markdown 답변으로 합성**한다.

> **질병·단백질 이름은 영어를 권장한다.** 지식그래프의 노드 라벨과 식별자가
> 영어(질병명, UniProt/Ensembl ID, 유전자 심볼 등)로 되어 있어, 영어 질의가
> 매칭 정확도가 높다. 한국어 질의도 동작한다.

환영 패널에 실린 예시 질문 그대로 시작해도 된다:

```text
• Tell me the targets for glaucoma treatment
• Return the top 20 natural products active against ROCK1 with IC50 < 100 nM
• Compare ROCK1 and ROCK2 head-to-head: list UniProt/Ensembl IDs,
  associated glaucoma subtypes, NP counts, top compounds, and approved drugs.
• 알츠하이머 관련 단백질 타겟에 활성이 있는 천연물 상위 10개를 보여줘
```

입력창 안내문에도 짧은 예시가 뜬다(예: `ROCK1 에 활성 있는 천연물 상위 20개`).
Enter로 전송, Shift+Enter로 줄바꿈이다.

### 1-2. 진행 카드 — 네 단계

질문을 보내면 답변 자리에 **"GraphRAG 진행 상황"** 카드가 뜨고, 아래 네 단계가
순서대로 채워진다:

| 단계 키 | 카드 표시 | 의미 |
|---|---|---|
| `cypher_gen` | 🧠 Cypher 생성 | 자연어 질문을 Cypher 쿼리로 변환 |
| `neo4j_exec` | 🔍 Neo4j 실행 | 생성된 Cypher를 지식그래프에서 실행 |
| `answer_synth` | 📝 답변 합성 | 결과 행을 Markdown 답변으로 정리 |
| `complete` | (카드 헤더에 요약) | 총 소요 시간·상태 요약 확정 |

카드에는 **실제로 실행된 Cypher 쿼리**와 **반환된 행 수(row count)**, 토큰
사용량이 함께 표시된다. 최종 **답변은 Markdown**으로 렌더링된다(표·목록·굵은
글씨 등).

> 내부적으로 이 네 단계는 MCP `graphrag_query` **한 번의 호출**이 돌려준
> 메타데이터로 사후에 채워진다(프론트엔드가 보여주는 모양은 그대로다). 자세한
> 내부 동작은 `docs/GRAPHRAG.md`의 "한 방 질의와 진행 카드" 절을 보라.

### 1-3. 사용 가능한 MCP 도구 세 개

GraphRAG MCP 서버(`http://graphrag-mcp:8893/sse`, 호스트 루프백
`127.0.0.1:8893`에도 공개)가 노출하는 도구는 정확히 셋이다. 채팅 UI의 🧬
GraphRAG 질의는 이 중 첫 번째를 쓴다.

- **`graphrag_query`** — 자연어 → Cypher → 실행 → Markdown 답변을 **한 방에**
  처리. `answer`, `cypher`, `row_count`, `token_usage` 등을 돌려준다.
  LLM 단계를 돌리므로 **`OPENROUTER_API_KEY`가 필요하다**(채팅과 같은 키).
- **`get_kg_stats`** — 노드/관계 개수 반환. **LLM 키 불필요.**
- **`run_cypher`** — 읽기 전용 직접 Cypher 실행. **LLM 키 불필요.**

로컬의 다른 MCP 클라이언트(예: Claude Desktop)도 `127.0.0.1:8893`으로 같은
서버에 붙어 이 도구들을 쓸 수 있다.

### 1-4. GraphRAG 워크드 예시

1. 헤더에서 **🧬 GraphRAG** 탭을 누른다.
2. 입력창에 다음을 붙여넣고 Enter:

   ```text
   Return the top 20 natural products active against ROCK1 with IC50 < 100 nM
   ```

3. 진행 카드가 `🧠 Cypher 생성` → `🔍 Neo4j 실행` → `📝 답변 합성` →
   완료 순으로 채워진다. 카드에는 생성된 Cypher(예: `MATCH (np:NaturalProduct)
   -[a:HAS_ACTIVITY]->(t:Target {symbol:'ROCK1'}) WHERE a.ic50 < 100 ...`)와
   반환 행 수가 뜬다.
4. 답변 자리에 **Markdown 표**로 천연물 목록(이름·IC50 등)이 정리되어 나온다.

> **키가 없을 때:** `OPENROUTER_API_KEY`가 비어 있으면 `graphrag_query`(자연어
> 질의)는 실패한다. 다만 KG가 살아 있는지는 키 없이도 `get_kg_stats` /
> `run_cypher`로 확인할 수 있다(방법은 `docs/GRAPHRAG.md`). 카드에
> `service.graphrag.mcp_unreachable` 관련 오류가 뜨면 GraphRAG 스택이 떠 있는지
> 부터 점검하라.

---

## 2. 🧪 가상 스크리닝 모드 — cofolding + 재점수화

### 2-1. 파이프라인 개요

스크리닝 모드는 **단백질–리간드 복합체를 예측하고 순위를 매긴다**:

```text
입력(단백질 FASTA + 리간드 SMILES) 또는 라이브러리 파일
      │
      ▼  AF3 cofolding  (외부 AlphaFold3 MCP — 사용자가 직접 운영)
      ▼  smina --minimize  재점수화  (번들 smina-mcp, 설정 불필요)
      ▼
순위화된 후보 표  +  Mol* 인터랙티브 3D 뷰  +  복합체 CIF(엔드포인트로 제공)
```

> **스크리닝 모드에는 닿을 수 있는 외부 AF3 MCP가 반드시 필요하다.** AF3가
> 연결돼 있지 않으면 파이프라인이 **cofolding 단계에서 실패**한다. AF3 모델
> 가중치·서열 DB는 이 저장소에 **재배포되지 않으며**(구글 딥마인드 라이선스),
> 사용자가 직접 AF3와 가중치를 확보해 자신의 GPU 머신에서 돌려야 한다. 준비·
> 연결 방법은 `docs/AF3_SETUP.md`와 `docs/BRIDGE.md`를 보라. 화면 하단 시스템
> 바의 `AF3` 칩으로 연결 상태를 먼저 확인하라. 번들 `smina`는 별도 설정이
> 필요 없다(내부 Docker 네트워크에서 자동 연결).

### 2-2. 입력 방법 1 — 채팅에 직접 붙여넣기

단백질 **FASTA 서열**과 **하나 이상의 리간드 SMILES**(한 줄에 하나, 뒤에
선택적으로 이름)를 함께 붙여넣고, **도킹 요청 문구**(예: "도킹해서 순위
매겨줘")를 덧붙여 전송한다. 환영 패널의 예시 형식이 그대로 유효하다:

```text
>ROCK1_HUMAN_1-415
MSTGDSFETRFEKMDNLLRDPKSEVNSDCLLDGLDALVYDLDFPALRKNKNIDNFLSRYK
... (전체 415aa 시퀀스) ...

CN(C)CCOC1=C(C=CC(=C1)C2=CNN=C2)NC(=O)[C@H]3CC4=C(C=CC(=C4)OC)OC3 Chroman_1
C[C@H]1CNCCCN1S(=O)(=O)C2=CC=CC3=C2C(=CN=C3)C H1152
C[C@H](C1CCC(CC1)C(=O)NC2=CC=NC=C2)N Y-27632

위 단백질에 화합물 3개를 docking해서 순위를 매겨주세요
```

- 각 SMILES 줄은 `SMILES [이름]` 형식이다. 이름은 생략 가능하며, 넣으면 결과
  표의 `Ligand` 열에 그대로 쓰인다.
- 도킹을 시작시키려면 **도킹 키워드**(예: "docking", "도킹", "순위")가 담긴
  요청 문구가 필요하다. 단백질/리간드만 붙여넣으면 챗봇은 일반 대화로 받는다.

### 2-3. 입력 방법 2 — 라이브러리 파일 업로드

여러 화합물을 한 번에 스크리닝하려면 입력창의 **라이브러리 업로드** 버튼으로
파일을 올린다. 허용 확장자는 **`.sdf`, `.smi`, `.csv`, `.txt`**다. 업로드하면
미리보기 영역에 파일명과 파싱된 화합물 정보가 뜬다. 이후 단백질 FASTA와 도킹
요청 문구를 함께 보내면 라이브러리 전체가 스크리닝 대상이 된다.

### 2-4. 진행과 결과

전송 후에는 **작업 진행 상황** 패널이 뜨고, cofolding→재점수화가 끝나면 결과
카드가 채워진다. 결과 카드에는 다음이 포함된다.

**① 순위화된 후보 표.** 컬럼은 다음과 같다(헤더에 단위·방향 표기):

| 컬럼 | 단위/방향 | 의미 |
|---|---|---|
| `#` | — | 현재 정렬 기준의 순위 |
| `Ligand` | — | 리간드 이름/ID |
| `ipTM` | 0~1 ↑ | AF3 인터페이스 신뢰도(높을수록 좋음) |
| `PAE` | Å ↓ | 예측 정렬 오차(낮을수록 좋음) |
| `Smina` | kcal/mol ↓ | `smina --minimize` 재점수(낮을수록 강한 결합) |
| `Score` | composite ↑ | 종합 점수(높을수록 좋음) |

표는 **정렬 가능**하다. 결과 카드의 **정렬 드롭다운**에서 `Smina` /
`Composite` / `AF3 Score`(ranking_score) / `ipTM`을 고르거나, 정렬 가능한 숫자
컬럼 헤더(`ipTM` / `Smina` / `Score`)를 클릭해 정렬한다(`PAE`·`#` 헤더는 정렬
버튼이 아니다). 백엔드 결과 API는 정렬 기준으로
**`smina` / `composite` / `iptm` / `ranking_score` / `e_inter`**(상호작용
에너지)를 받는다. `상위 N개` 입력으로 표시 개수를 제한(최대 100)하고,
**새로고침**·**CSV**·**TSV** 버튼으로 현재 정렬·상위 N 기준의 표를 내려받을 수
있다.

**② 인터랙티브 Mol* 3D 뷰.** 후보를 열면 **Mol*** 기반 3D 복합체 뷰어가
오버레이로 뜬다. 단백질–리간드 복합체를 회전·확대하며 보고, 툴바의
**스크린샷** 버튼으로 현재 화면을 이미지로 저장한다.

**③ 복합체 CIF.** 각 후보의 예측 복합체 구조는 **CIF**로 제공된다. 전용
다운로드 버튼은 없지만, Mol* 뷰어가 로드하는 것과 동일한 구조를
`/api/results/{job}/{ligand}/cif` 엔드포인트에서 직접 받아 외부 도구(PyMOL 등)로
열 수 있다.

**④ 분포 차트.** 결과 카드의 **차트** 버튼으로 분포 패널을 열면 **Smina 분포**와
**ipTM vs Smina** 산점도를 볼 수 있다.

### 2-5. 스크리닝 워크드 예시

1. 헤더에서 **🧪 AlphaFold** 탭이 선택돼 있는지 확인한다(기본값).
2. 시스템 바의 `AF3` 칩이 연결됨을 확인한다. `-`거나 끊겨 있으면
   `docs/AF3_SETUP.md`로 먼저 AF3 MCP를 연결한다.
3. 위 2-2의 ROCK1 FASTA + 3개 SMILES(Chroman_1, H1152, Y-27632) +
   "docking해서 순위를 매겨주세요"를 붙여넣고 Enter.
4. 진행 패널에서 AF3 cofolding → smina 재점수화가 진행된다(대형 작업은 시간이
   걸린다).
5. 결과 카드에서 **Smina** 기준 정렬로 상위 후보를 확인하고, 관심 후보를 열어
   **Mol* 3D 뷰**로 결합 포즈를 살펴본다. 필요하면 위 CIF 엔드포인트로 구조를
   받는다. 표 전체는 **CSV/TSV**로 저장한다.

---

## 3. 자주 막히는 곳

- **스크리닝이 cofolding에서 실패** — 외부 AF3 MCP에 닿지 못하는 경우다. 원격
  호스트이거나 AF3 MCP가 `127.0.0.1`에만 바인딩된 상황이 흔하다. 호스트 바인딩
  패치·SSH 터널 등 해결법은 `docs/AF3_SETUP.md` / `docs/BRIDGE.md`, 연결값
  `AF3_MCP_URL`은 `configure.md`를 보라.
- **GraphRAG 질의 실패** — `OPENROUTER_API_KEY` 누락(자연어 질의는 LLM 필요),
  또는 GraphRAG 스택 미기동(`service.graphrag.mcp_unreachable`). 스택 기동·검증은
  `docs/GRAPHRAG.md`. GraphRAG가 꺼져 있어도 앱과 스크리닝 모드는 정상 동작한다.
- **화면에 모드 토글이 그대로인데 반응 없음** — 브라우저 새로고침 후 헤더의
  탭(🧪/🧬)을 다시 눌러 보라. 선택은 `localStorage`에 저장된다.

---

## 관련 문서

- [docs/INSTALL.md](INSTALL.md) — 설치·헬스체크·nginx·트러블슈팅.
- [docs/GRAPHRAG.md](GRAPHRAG.md) — GraphRAG 스택 배포·검증·트러블슈팅(기본 ON).
- [docs/AF3_SETUP.md](AF3_SETUP.md) — 외부 AF3 라이선스·가중치·MCP 브리지 준비.
- [docs/BRIDGE.md](BRIDGE.md) — 외부 AF3 MCP 도달성(호스트 바인딩/터널).
- [configure.md](../configure.md) — 모든 설정 키와 기본값.
