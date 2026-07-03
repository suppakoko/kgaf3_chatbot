# kgaf3_chatbot

**[English](README.md)** | 한국어

**kgaf3_chatbot**은 두 가지 모드를 한 화면에서 제공하는 로컬 우선(local-first) 웹 챗봇이다:

1. 🧬 **GraphRAG** — 천연물 지식 그래프(NPASS 3.0 + Open Targets 25.12)에 대한
   자연어 질의응답.
2. 🧪 **가상 스크리닝(Virtual screening)** — 외부 AlphaFold3 MCP와 동봉된
   `smina --minimize` 재채점(rescoring)을 통한 단백질–화합물 cofolding, 결과
   웹 UI 포함.

질문을 던지면 지식 그래프에서 근거를 찾아 답하고, 단백질 서열 + 리간드 SMILES를
주면 AF3로 복합체를 예측한 뒤 `smina --minimize`로 정밀화·재채점해 **후보 랭킹
테이블**과 인터랙티브 3D 뷰를 돌려준다.

## 주요 기능

- 🧬 **GraphRAG 질의응답 (기본 활성)** — NPASS 3.0 + Open Targets 25.12 지식
  그래프(**273,519 노드 / 1,493,463 관계**)에 자연어로 질의한다.
- 🧪 **AF3 cofolding** — **외부** AlphaFold3 MCP 서버를 호출해 holo 복합체를 예측한다.
- ⚗️ **동봉 rescoring** — 자체 smina MCP(`smina --minimize`)를 포함하므로 별도
  설치가 필요 없다.
- 📊 **랭킹 출력** — 다운로드 가능한 후보 테이블, Mol* 3D 시각화, 신뢰도 차트.
- 🔌 **OpenRouter LLM** — 채팅 모델(기본 `anthropic/claude-sonnet-4-6`)을 UI에서
  선택한다.
- 🕸️ **MCP 기반** — GraphRAG MCP 도구 3종(`graphrag_query`, `get_kg_stats`,
  `run_cypher`)과 외부 AF3 MCP 브리지로 연동한다.
- 🐳 **원클릭 설치** — `configure.md` + `install.sh` / `install.bat`, 전부 Docker
  Compose 위에서.

## 아키텍처

```
[브라우저]  Vanilla JS + Mol* + Plotly
   │ REST / WebSocket
   ▼
┌────────────────────────────────────────────────┐
│ kgaf3-chat  (FastAPI :5013)                     │
│   ├── OpenRouter   (LLM, 채팅)                   │──► api.openrouter.ai   (외부)
│   ├── HTTP MCP ─► smina-mcp    (:8001 내부)      │  동봉 — 도킹 / minimize + rescore
│   ├── MCP SSE  ─► graphrag-mcp (:8893)          │──► neo4j (지식 그래프 내장)
│   └── HTTP MCP ─► AF3 MCP      (:8002)          │──► 외부 AF3 서버 (사용자 운영)
└────────────────────────────────────────────────┘
        Docker Compose 네트워크
```

kgaf3-chat은 **오케스트레이션 계층**이다. `smina-mcp`, `graphrag-mcp`, `neo4j`는
모두 Compose 스택 안에서 함께 돈다. 지식 그래프는 `neo4j` 이미지에 내장돼 첫 기동
시 자동 로드된다(약 1분). **AlphaFold3만 이 패키지 밖**에서 돌고, 가중치·DB는
라이선스 대상이라 사용자 환경에 남는다. kgaf3-chat은 GraphRAG 컨테이너에 강한
의존이 없어, KG가 내려가 있어도 앱은 무리 없이 뜨고 GraphRAG 기능만 우아하게
비활성화된다(로그에 `service.graphrag.mcp_unreachable` 기록).

호스트 포트: `5013`(kgaf3-chat 웹 UI), `8893`(graphrag-mcp SSE, 다른 로컬 MCP
클라이언트도 사용 가능), `8002`(외부 AF3 MCP, 사용자 운영). `smina-mcp`(8001)와
`neo4j`(7474/7687)는 내부 전용이다.

## 요구 사항

- **Docker** + Docker Compose (Linux, macOS, 또는 Windows/WSL2).
- **도달 가능한 외부 AF3 MCP 서버** — 스크리닝 모드에 필요.
  [docs/AF3_SETUP.md](docs/AF3_SETUP.md), [docs/BRIDGE.md](docs/BRIDGE.md) 참고.
- **OpenRouter API 키** — <https://openrouter.ai/keys>. 채팅과 `graphrag_query`에
  공통으로 쓰인다.
- kgaf3-chat 자체에는 GPU가 필요 없다(AF3의 GPU는 외부 호스트에 있다).

## 빠른 시작

```bash
# 1. 코드 받기
git clone https://github.com/suppakoko/kgaf3_chatbot.git
cd kgaf3_chatbot

# 2. 설정: 단일 설정 파일만 편집
#    OPENROUTER_API_KEY, AF3_MCP_URL, AF3_OUTPUT_ROOT 입력
$EDITOR configure.md          # 모든 옵션은 configure.md 참고

# 3. 설치 (Docker Compose 스택 빌드 + 기동)
./install.sh                  # Linux / macOS
# install.bat                 # Windows / WSL2 (더블클릭 또는 터미널 실행)
```

설치기 단계: preflight → `configure.md` 파싱 → `.env.docker` 생성 → 호스트
디렉터리 부트스트랩 → best-effort SELinux/firewalld 처리 → `docker compose build`
→ 스택 기동(기본은 GraphRAG 포함 네 개 서비스, 옵트아웃 시 kgaf3-chat+smina-mcp만)
→ `http://localhost:5013/health/ready` 헬스 폴링 → 외부 AF3 검증 → 결과 보고.
성공하면 `http://localhost:5013`을 열면 된다.

모든 키는 **[configure.md](configure.md)**, 단계별 상세 설치는
**[docs/INSTALL.md](docs/INSTALL.md)**, AF3 연결 검증이 실패하면
**[docs/BRIDGE.md](docs/BRIDGE.md)**를 본다.

## 사전 빌드 이미지 사용 (빌드 건너뛰기)

kgaf3-chat과 smina-mcp 이미지는 릴리스마다 GitHub Container Registry에 게시되므로
로컬 빌드 대신 pull로 받을 수 있다(GraphRAG 이미지는 언제나 Docker Hub의
`yoonjuho94/*`에서 pull):

```bash
docker pull ghcr.io/suppakoko/kgaf3-chat:latest
docker pull ghcr.io/suppakoko/afmm-smina-mcp:latest
docker pull yoonjuho94/graphrag-neo4j:1.0
docker pull yoonjuho94/graphrag-mcp-server:1.1
```

Compose 스택이 빌드 대신 이 이미지를 쓰게 하려면 override를 추가한다(재현성을
위해 버전 태그를 고정하는 편이 좋다):

```yaml
# docker-compose.override.yml
services:
  kgaf3-chat:
    image: ghcr.io/suppakoko/kgaf3-chat:latest
    build: null        # 로컬 빌드 비활성화
  smina-mcp:
    image: ghcr.io/suppakoko/afmm-smina-mcp:latest
    build: null
```

## 문서

- [configure.md](configure.md) — 단일 설치 설정 파일.
- [docs/INSTALL.md](docs/INSTALL.md) — 상세 로컬 설치(사전 요구 사항, 단계별 절차,
  헬스 체크, nginx, 문제 해결).
- [docs/USAGE.md](docs/USAGE.md) — 두 채팅 모드와 결과 웹 UI 상세 사용법·예시.
- [docs/GRAPHRAG.md](docs/GRAPHRAG.md) — GraphRAG 지식 그래프 스택 배포·사용·문제 해결.
- [docs/AF3_SETUP.md](docs/AF3_SETUP.md) — AF3 라이선스/가중치/DB 확보 + AF3 MCP
  브리지 실행 + 호스트 패치 + 검증.
- [docs/BRIDGE.md](docs/BRIDGE.md) — 외부 AF3 MCP 도달성 확보(블로커 B3).
- [af3-bridge/README.md](af3-bridge/README.md) — 동봉 AF3 브리지 파일 설명(독립 실행 불가).
- [sharing_chtbot.md](sharing_chtbot.md) — 이 리포를 공개하는 관리자용 가이드(GitHub, ghcr.io, Zenodo DOI).

## 라이선스

이 리포의 코드는 [MIT](LICENSE) — © 2026 kgaf3_chatbot contributors.
인용 방법은 [CITATION.cff](CITATION.cff) 참고.

> **AlphaFold3 주의:** AF3 모델 가중치와 서열 DB는 이 프로젝트에 **포함되지 않으며**,
> 각자의 라이선스 아래 별도로 받아 실행해야 한다. kgaf3_chatbot은 사용자가 제공하는
> AF3 서버에 연결만 한다.
>
> **데이터셋 주의:** 지식 그래프의 원천 데이터셋(NPASS, Open Targets)도 각자의
> 라이선스 아래 있으며 사용자의 책임이다. 이 리포는 라이선스 대상 자산을 그대로
> 재배포하지 않는다.
