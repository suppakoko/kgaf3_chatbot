"""afmm_chat - uvicorn 엔트리.

실행:
  uv run python run.py                    # 기본 (.env 의 APP_PORT 사용)
  uv run python run.py --port 5013        # 포트 override
  uv run python run.py --reload           # 개발 모드 (hot reload)
"""

import argparse

import uvicorn

from app.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="afmm_chat server")
    parser.add_argument("--host", default=settings.app_host)
    parser.add_argument("--port", type=int, default=settings.app_port)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,  # structlog 가 stdout 으로 직접 출력
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
