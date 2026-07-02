# AF3 셋업 — AlphaFold3와 AF3 MCP 브리지 연결하기

kgaf3_chatbot의 🧪 **가상 스크리닝** 모드는 단백질–리간드 cofolding을
**외부 AlphaFold3(AF3)** 에 위임한다. 이 저장소는 AF3를 **직접 실행하지
않으며**, AF3 가중치나 서열 DB를 **담고 있지도 않다.** kgaf3_chatbot은
사용자가 직접 띄운 **AF3 MCP 서버**에 `AF3_MCP_URL`로 HTTP 요청을 보낼 뿐이다.

```
kgaf3-chat 컨테이너 ──HTTP──► AF3 MCP 브리지(:8002/mcp/) ──docker run──► alphafold3 (GPU · 가중치 · 서열 DB)
   (AF3_MCP_URL)               (외부 af3_chatbot 백엔드가 운영)
```

이 문서는 위 파이프라인을 처음부터 세우는 4단계를 설명한다:
① AlphaFold3 준비/실행 → ② AF3 MCP 브리지 서버 실행 → ③ 호스트 바인딩 패치
적용 → ④ `AF3_MCP_URL` 설정 → 검증. 도달성(reachability)이 막히는 구조적
지점과 우회법은 **[`docs/BRIDGE.md`](BRIDGE.md)** 에 따로 정리돼 있으니 함께
본다.

> [!IMPORTANT]
> **AF3 가중치와 서열 DB는 Google DeepMind가 라이선스한 자산이며, 이
> 저장소에 재배포되지 않는다.** 사용자가 AF3의 **자체 라이선스 조건에 따라**
> 직접 가중치·DB를 발급받아, **자신의 GPU 머신에서** 실행해야 한다.
> kgaf3_chatbot은 "이미 가중치/DB를 갖춘 AF3에 연결"만 책임진다. 이 저장소
> 코드는 MIT(`LICENSE`)이지만, AF3와 그 데이터의 라이선스 준수는 전적으로
> 사용자 책임이다.

---

## 사전 요구사항

- **NVIDIA GPU** + 드라이버 + `nvidia-container-toolkit` (AF3 추론용).
- **AlphaFold3 Docker 이미지** (`alphafold3:latest` 등) — 사용자가 직접 빌드.
- **AF3 모델 가중치** — DeepMind로부터 라이선스 발급 후 획득.
- **AF3 서열 데이터베이스** — MSA/템플릿 검색용 (수백 GB 규모).
- 위 자산을 두는 호스트 경로 (예: 가중치 디렉터리, DB 디렉터리, 입력/출력
  작업 디렉터리).

이 모든 것은 **AF3를 돌리는 머신**에 있어야 하며, kgaf3_chatbot이 도는
머신과 같을 수도 다를 수도 있다(원격 구성은 `docs/BRIDGE.md` 참고).

---

## 1단계 — AlphaFold3 준비 및 실행

가중치·DB·라이선스 발급 절차는 전부 **공식 AlphaFold3 저장소**를 따른다.
이 문서는 그 절차를 대체하지 않는다.

- 공식 저장소 및 라이선스: <https://github.com/google-deepmind/alphafold3>
- 모델 파라미터(가중치)는 별도 신청·승인 절차를 거쳐 DeepMind로부터
  받는다. **kgaf3_chatbot 저장소에는 포함돼 있지 않다.**
- 공식 안내에 따라 `alphafold3` Docker 이미지를 빌드하고, 가중치·서열 DB를
  내려받아 배치한다.

이 단계가 끝나면, 사용자는 GPU에서 `alphafold3` Docker 이미지를 실행해
입력 JSON으로부터 예측 CIF를 생성할 수 있는 상태여야 한다. 다음 단계의 MCP
브리지가 바로 이 Docker 이미지를 구동한다.

---

## 2단계 — AF3 MCP 브리지 서버 실행

가상 스크리닝이 AF3에 말을 걸려면, AF3를 감싸는 **MCP 서버**가 필요하다.
이 저장소는 그 브리지의 **참조 구현**을 `af3-bridge/`에 동봉한다.

- **`af3-bridge/af3_mcp_http.py`** — HTTP MCP 트랜스포트. 이 동봉 사본은
  `AF3_MCP_HOST`(기본 `0.0.0.0`) / `AF3_MCP_PORT`(기본 `8002`) 환경변수를 읽어
  바인딩하므로 기본 엔드포인트는 `http://<AF3 호스트>:8002/mcp`이다(즉 호스트
  패치가 이미 반영된 형태 — [`af3-bridge/README.md`](../af3-bridge/README.md)
  및 [`docs/BRIDGE.md`](BRIDGE.md) 참고). kgaf3-chat이 실제로 붙는 대상이다.
- **`af3-bridge/af3_mcp_server.py`** — stdio MCP 변형(참조용).
- **`af3-bridge/af3-mcp.service`** — 브리지를 상시 구동하는 systemd 유닛 예시.
- 자세한 설명·도구 목록·실행법은 **[`af3-bridge/README.md`](../af3-bridge/README.md)** 참고.

> [!WARNING]
> **이 브리지 코드는 단독(standalone) 실행이 불가능하다.** 외부
> **`af3_chatbot` 백엔드**에 강하게 결합돼 있어서 `app.services.af3_service.AF3Service`,
> `JsonBuilder`, `BatchDockService`를 import하고, 실제 추론은 `alphafold3`
> Docker 이미지를 `docker run`으로 돌려 수행한다. 따라서 브리지는 반드시
> **af3_chatbot 앱 + alphafold3 이미지 + 사용자의 가중치/DB**가 갖춰진 환경
> 안에서(또는 그와 함께) 실행해야 한다. `af3-bridge/`의 파일은 그 환경에서
> 쓸 참조 브리지 서버 + systemd 유닛으로 제공되는 것이다.

브리지는 아래 **6개 핵심 도구**를 HTTP MCP(`:8002`)로 노출한다(배치용 도구는
추가):

| 도구 | 하는 일 |
|------|---------|
| `af3_create_job` | 단백질 서열 + 리간드 SMILES로 AF3 입력 JSON 생성 |
| `af3_run_job` | 생성된 작업을 GPU에서 cofolding 실행 |
| `af3_get_status` | 작업 상태 조회 |
| `af3_get_results` | 완료된 작업의 결과(CIF 등) 조회 |
| `af3_get_input_json` | 작업에 쓰인 입력 JSON 조회 |
| `af3_list_jobs` | 작업 목록 나열 |

(그 밖에 `af3_create_batch_job` / `af3_get_batch_status` /
`af3_get_batch_results` / `af3_get_batch_ligand_result` 등 라이브러리 배치용
도구가 함께 노출된다. 설치기는 마지막 검증에서 배치 도구까지 확인한다.)

systemd로 상시 구동하는 예(유닛 내 경로·환경변수는 사용자 환경에 맞게 수정):

```bash
# af3-bridge/af3-mcp.service 를 사용자 환경에 맞게 수정한 뒤 설치
sudo cp af3-bridge/af3-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now af3-mcp
sudo systemctl status af3-mcp        # active (running) 확인
journalctl -u af3-mcp -f             # 로그 실시간 확인
```

---

## 3단계 — 호스트 바인딩 패치 적용 (B3 도달성 문제)

외부 **`af3_chatbot` 저장소**의 원본 `af3_mcp_http.py`는 `main()`에서 바인딩을
`127.0.0.1:8002`로 **하드코딩**한다(이것이 `patches/af3_mcp_host_env.patch`가
겨냥하는 대상이다). 참고로 이 저장소에 **동봉된**
`af3-bridge/af3_mcp_http.py` 사본은 이미 이 패치가 반영돼
`AF3_MCP_HOST`(기본 `0.0.0.0`) / `AF3_MCP_PORT`(기본 `8002`)를 읽는다
([`af3-bridge/README.md`](../af3-bridge/README.md) · [`docs/BRIDGE.md`](BRIDGE.md)
참고). 문제는 사용자가 실제로 운영하는 **외부 af3_chatbot** 쪽 원본이며,
그 루프백 전용 바인딩은 **같은 호스트의 다른 프로세스만** 닿을 수 있어 다음 두
상황에서는 도달 불가다:

- kgaf3-chat **컨테이너 안**에서는 호스트를 `host.docker.internal`(또는
  docker0 브리지 IP)로 보므로 호스트 루프백에 닿지 못한다.
- **원격 머신**(예: 한쪽에서 kgaf3_chatbot, 다른 Linux 박스에서 AF3)에서도
  당연히 닿지 못한다.

이것이 **B3 도달성 문제**다. 이 저장소는 바인딩을 환경변수로 바꾸는
1줄 패치를 동봉한다.

- **`patches/af3_mcp_host_env.patch`** — `main()`이 `AF3_MCP_HOST`(기본
  `0.0.0.0`) / `AF3_MCP_PORT`(기본 `8002`) 환경변수를 읽도록 바꾼다.

> 이 패치는 **외부 `af3_chatbot` 저장소**에 적용한다(kgaf3_chatbot이 아니다).
> 설치기는 이 패치를 **자동으로 적용하지 않는다.**

```bash
# af3_chatbot 저장소 루트에서 (git 체크아웃이면)
git apply /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
# git 체크아웃이 아니면
patch -p1 < /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
```

패치 적용 후 바인딩을 설정한다(예: `af3-mcp.service`의 `[Service]` 블록):

```ini
[Service]
Environment=AF3_MCP_HOST=0.0.0.0      # 또는 docker0 브리지 IP (예: 172.17.0.1)
Environment=AF3_MCP_PORT=8002
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart af3-mcp
```

`0.0.0.0`은 모든 인터페이스에 여는 것이므로, **방화벽으로 신뢰 대상에만**
범위를 좁혀야 한다. 패치 없이 푸는 대안(host network 모드, socat/nginx 포워더,
SSH 터널)과 방화벽 설정 예시는 **[`docs/BRIDGE.md`](BRIDGE.md)** 에 전부
정리돼 있다. 이 3단계와 B3 관련 내용은 전적으로 **외부 AF3 MCP**에 적용되며,
GraphRAG 스택(내부 compose)과는 무관하다.

---

## 4단계 — `AF3_MCP_URL` 설정 (configure.md)

브리지가 도달 가능해졌으면, kgaf3_chatbot이 그 주소를 알도록
[`configure.md`](../configure.md)의 ```ini 블록에서 `AF3_MCP_URL`을 맞춘다.

```ini
AF3_MCP_URL     = http://host.docker.internal:8002/mcp/   # 같은 호스트의 AF3
AF3_OUTPUT_ROOT = /data/af3_output                        # AF3 결과 절대경로(이미 존재해야 함)
AF3_MCP_AUTH_TOKEN =                                      # AF3 MCP가 인증을 요구할 때만
```

컨테이너 관점에서 올바른 호스트를 가리키는지가 핵심이다:

- **같은 호스트의 AF3** → `http://host.docker.internal:8002/mcp/`.
  Linux compose에서 `host.docker.internal`이 안 풀리면 `extra_hosts`로
  `host-gateway`를 매핑하거나 docker0 IP(예: `172.17.0.1`)를 직접 적는다.
- **원격 AF3 호스트** → `http://<원격호스트IP>:8002/mcp/`.

`AF3_OUTPUT_ROOT`는 AF3가 결과를 쓰는 **호스트 절대경로**이며, kgaf3-chat이
**읽기 전용**으로 마운트해 예측 CIF를 읽는다. 경로는 설치 전에 이미 존재해야
한다. 인증 뒤에 브리지를 둔 경우에만 `AF3_MCP_AUTH_TOKEN`에 Bearer 토큰을
넣는다. 나머지 설치 흐름은 [`docs/INSTALL.md`](INSTALL.md)를 따른다.

---

## 5단계 — 검증

설치기(`install.sh` / `install.bat`)는 마지막 단계에서 `AF3_MCP_URL`로 MCP
`initialize` + `tools/list`를 호출해 **연결과 도구 목록(배치 도구 포함)** 을
확인하고, 실패 시 무엇을 고쳐야 하는지 정확히 출력한다. 성공하면 보고서에
`External AF3: <URL> [connected, batch tools verified]`가 찍힌다.

수동으로 엔드포인트가 살아있는지만 빠르게 보려면:

```bash
# 200 응답이면 엔드포인트는 살아있음 (MCP 핸드셰이크는 설치기가 수행)
curl -i http://host.docker.internal:8002/mcp/
```

연결이 실패하면 다음 순서로 원인을 좁힌다(이 중 하나가 거의 항상 원인이다):

1. 브리지가 떠 있나 — `systemctl status af3-mcp` / `journalctl -u af3-mcp -f`.
2. 바인딩이 루프백 전용인가 — **B3**, 3단계의 패치가 필요하다.
3. `AF3_MCP_URL`이 컨테이너 관점에서 올바른 호스트를 가리키나 — 4단계.

가상 스크리닝은 도달 가능한 AF3 MCP가 **반드시** 필요하다. AF3가 연결되지
않으면 앱은 뜨더라도 cofolding 단계(Stage 3)에서 매번 실패한다. 자세한 진단과
우회법은 **[`docs/BRIDGE.md`](BRIDGE.md)** 를, 사용법은
[`docs/USAGE.md`](USAGE.md)를 참고한다.

---

관련 문서: [`docs/BRIDGE.md`](BRIDGE.md) (B3 도달성·우회법),
[`af3-bridge/README.md`](../af3-bridge/README.md) (브리지 파일 상세),
[`configure.md`](../configure.md) (`AF3_MCP_URL` · `AF3_OUTPUT_ROOT` ·
`AF3_MCP_AUTH_TOKEN`), [`docs/INSTALL.md`](INSTALL.md) (전체 설치).
GraphRAG는 이와 완전히 별개의 내부 스택이다 → [`docs/GRAPHRAG.md`](GRAPHRAG.md).
