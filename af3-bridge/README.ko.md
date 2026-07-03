[English](README.md) | **한국어**

# af3-bridge/ — AF3↔MCP 브리지 (참조용, 단독 실행 불가)

이 디렉터리는 kgaf3_chatbot의 **스크리닝 모드가 연결하는 외부 AlphaFold3(AF3) MCP
서버**의 **참조(reference) 구현**을 담고 있다. kgaf3_chatbot 본체(`kgaf3-chat`
서비스)는 AF3를 **직접 실행하지 않으며**, `configure.md`의 `AF3_MCP_URL`이 가리키는
이 AF3 MCP 서버에 HTTP로 요청을 보낼 뿐이다.

```
kgaf3-chat 컨테이너 ──HTTP──► AF3 MCP 서버(:8002/mcp) ──docker run──► alphafold3 (GPU, 가중치·DB)
   (AF3_MCP_URL)              (여기 파일들이 그 참조 구현)     (사용자 소유·라이선스)
```

> **핵심: 이 파일들은 단독으로 돌아가지 않는다.** 아래 코드는 별도의
> **`af3_chatbot` 백엔드 저장소**에서 잘라온 사본이며, 그 저장소의
> `app.services.*` 모듈에 강하게 결합(coupled)되어 있다. 그리고 실제 구조 예측은
> **`alphafold3` Docker 이미지 + 사용자가 직접 취득한 라이선스 가중치·DB**가
> 수행한다. 따라서 이 디렉터리만 떼어서 실행할 수 없다. kgaf3_chatbot 배포판은
> 이 코드를 **어떻게 배치·구동하는지 보여 주기 위한 참조**로만 포함한다.

> **AF3 가중치·MSA DB는 배포 대상이 아니다(라이선스).** Google DeepMind가
> 라이선스하는 AF3 모델 가중치와 서열 DB는 이 저장소에 **포함되어 있지 않다.**
> 사용자가 AF3 라이선스 조건에 따라 직접 취득하고, 자신의 GPU 머신에서 운영해야
> 한다. 취득·설치 절차는 [`../docs/AF3_SETUP.ko.md`](../docs/AF3_SETUP.ko.md) 참조.

---

## 1. 포함된 파일

| 파일 | 역할 |
|------|------|
| `af3_mcp_http.py` | **HTTP MCP transport 서버.** kgaf3-chat이 연결하는 실제 엔드포인트(`:8002/mcp`). Streamable HTTP transport로 MCP 도구를 노출한다. |
| `af3_mcp_server.py` | **stdio MCP transport 변형.** 동일 도구의 일부를 표준입출력 기반으로 노출하는 별도 진입점(예: 로컬 MCP 클라이언트 직결용). HTTP 서버와 완전히 독립. |
| `af3-mcp.service` | 위 HTTP 서버를 systemd 서비스로 상시 구동하는 유닛 파일(예시). |

이들은 실제로는 `af3_chatbot` 저장소의 `app/mcp/af3_mcp_http.py`,
`app/mcp/af3_mcp_server.py`에 해당한다. 즉 **AF3를 운영할 머신의 `af3_chatbot`
체크아웃 안**에서 실행되어야 하며, 여기 사본은 내용 확인·비교용이다.

---

## 2. af3_chatbot 백엔드에 대한 결합(왜 단독 실행이 안 되나)

`af3_mcp_http.py`와 `af3_mcp_server.py`는 첫 도구 호출 시 `af3_chatbot`의 서비스
객체를 lazy import 한다:

- `app.services.af3_service.AF3Service` — AF3 작업 제출·상태·결과 조회, 그리고
  `alphafold3` Docker 컨테이너 실행 자체를 담당.
- `app.services.json_builder.JsonBuilder` — 단백질 서열 + 리간드로부터 AF3
  input JSON(`version: 2`) 생성·검증.
- `app.services.batch_dock_service.BatchDockService` — 단일 단백질 + N개 리간드
  배치 도킹(동일 서열 MSA 재사용) 오케스트레이션.
- `app.services.result_service.ResultService` — 완료 작업의 신뢰도 요약
  (ipTM, ranking_score, PAE, pLDDT) 및 결과 CIF 경로 수집.

이 모듈들이 `PYTHONPATH`에 없으면 서버는 도구 실행 단계에서 실패한다. 또한
`AF3Service`는 실행 시 `alphafold3` Docker 이미지와 마운트된 가중치·DB 경로를
필요로 한다. 그래서 이 파일들은 **① af3_chatbot 앱 코드 + ② alphafold3 이미지 +
③ 사용자 가중치·DB** 세 가지가 모두 갖춰진 환경에서만 동작한다.

---

## 3. HTTP MCP 엔드포인트와 도구

`af3_mcp_http.py`는 `AF3_MCP_HOST`/`AF3_MCP_PORT`(기본 `0.0.0.0:8002`)에 바인딩하고
`/mcp` 경로에 MCP transport를 마운트한다. 따라서 엔드포인트는:

```
http://<AF3 호스트>:8002/mcp
```

kgaf3_chatbot의 `AF3_MCP_URL`은 여기를 가리킨다(컨테이너 기본값
`http://host.docker.internal:8002/mcp/`).

**핵심 도구 6종:**

| 도구 | 설명 |
|------|------|
| `af3_create_job` | 단백질 서열 + 리간드(SMILES/CCD)로 AF3 input JSON 생성. |
| `af3_run_job` | input JSON으로 AF3 작업 제출(`full`/`data_pipeline_only`/`inference_only`). |
| `af3_get_status` | 작업 상태 조회. |
| `af3_get_results` | 완료 작업의 신뢰도 요약(ipTM, ranking_score, mean_pae, mean_plddt) 조회. |
| `af3_get_input_json` | 저장된 input JSON 스펙 반환(`version: 2` 계약 검증용). |
| `af3_list_jobs` | 작업 목록 조회. |

**배치 도킹 도구 4종**(스크리닝 모드의 다중 리간드 파이프라인용, MSA 1회 + N회
inference):

| 도구 | 설명 |
|------|------|
| `af3_create_batch_job` | 단일 단백질 + N개 리간드 배치 생성(동일 서열 MSA 자동 재사용). |
| `af3_get_batch_status` | 배치 진행 상태(`msa_reused`, 리간드별 카운트) 조회. |
| `af3_get_batch_results` | 완료 배치의 리간드별 결과 일괄 반환. |
| `af3_get_batch_ligand_result` | 배치 내 단일 리간드 상세(cif_path, summary_confidences). |

> `af3_mcp_server.py`(stdio 변형)는 이 중 핵심 5종(`af3_create_job`,
> `af3_run_job`, `af3_get_status`, `af3_get_results`, `af3_list_jobs`)만 노출한다.
> kgaf3-chat이 실제로 쓰는 것은 HTTP 서버(`af3_mcp_http.py`)다.

---

## 4. systemd로 구동하기 (`af3-mcp.service`)

이 유닛은 AF3 호스트의 `af3_chatbot` 체크아웃 안에서 `python -m app.mcp.af3_mcp_http`를
uv 가상환경으로 실행한다. `WorkingDirectory`, `User`/`Group`, `AF3_*` 경로, GPU 디바이스,
Docker 이미지 태그는 **예시값**이므로 반드시 사용자 환경에 맞게 고쳐야 한다.

```bash
# 유닛 설치 (경로·환경변수 수정 후)
sudo cp af3-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now af3-mcp

# 관리
sudo systemctl status af3-mcp     # 상태
sudo systemctl restart af3-mcp    # 재시작
journalctl -u af3-mcp -f          # 로그 실시간
```

유닛은 `alphafold3` 컨테이너를 실행하기 위해 `SupplementaryGroups=docker`로 Docker
소켓 접근 권한을 얻는다. `AF3_MODELS_DIR`/`AF3_DB_DIR` 등이 사용자의 실제 가중치·DB
위치를 가리키는지 확인한다.

---

## 5. 컨테이너·원격에서 도달 가능하게 만들기 (호스트 패치)

원본 `af3_chatbot`의 `main()`은 바인딩을 `127.0.0.1:8002`로 하드코딩한다(루프백
전용). 이 경우 kgaf3-chat **컨테이너**(호스트를 `host.docker.internal`/docker0로 봄)나
**원격 머신**에서 도달할 수 없다(reachability 문제 "B3").

이를 풀려면 [`../patches/af3_mcp_host_env.patch`](../patches/af3_mcp_host_env.patch)를
**외부 `af3_chatbot` 저장소**에 적용해 바인딩을 `AF3_MCP_HOST`/`AF3_MCP_PORT`
환경변수로 전환한다. (여기 담긴 `af3_mcp_http.py` 사본은 이미 이 패치가 반영된
형태다.)

```bash
# af3_chatbot 저장소 루트에서
git apply /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
# git 체크아웃이 아니면:
patch -p1 < /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
```

패치 적용 후 `af3-mcp.service`에 바인딩을 지정한다:

```ini
[Service]
Environment=AF3_MCP_HOST=0.0.0.0      # 또는 docker0 브리지 IP (예: 172.17.0.1)
Environment=AF3_MCP_PORT=8002
```

`0.0.0.0`은 모든 인터페이스에 열리므로 방화벽으로 신뢰 대상(docker0/특정 원격 IP)만
허용하라. 패치 없이 푸는 방법(host network, socat 포워더, SSH 터널)과 방화벽 설정,
검증 절차는 [`../docs/BRIDGE.ko.md`](../docs/BRIDGE.ko.md)에 정리되어 있다.

---

## 6. 사용자 책임 요약

- **AF3 가중치·서열 DB 취득·설치·운영**: 사용자 책임. 이 저장소는 재배포하지 않는다.
- **`alphafold3` Docker 이미지 빌드·GPU 준비**: 사용자 책임.
- **`af3_chatbot` 백엔드 실행 환경**(위 `app.services.*` 모듈): 사용자 책임.
- kgaf3_chatbot은 "이미 가동 중인 AF3 MCP에 `AF3_MCP_URL`로 연결"만 담당한다.

전체 AF3 준비·구동·검증 흐름: [`../docs/AF3_SETUP.ko.md`](../docs/AF3_SETUP.ko.md) ·
브리지 도달성 문제와 대책: [`../docs/BRIDGE.ko.md`](../docs/BRIDGE.ko.md).
