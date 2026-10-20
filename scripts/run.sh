#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo '가상환경이 없습니다. README의 설치 절차를 먼저 실행해주세요.'
  exit 1
fi
exec .venv/bin/python -m uvicorn crashcustomer.app:app --host 127.0.0.1 --port 8710
