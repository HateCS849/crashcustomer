"""Safety gate for registered applications. Only local or explicitly allowed test hosts are run.

Fault injection and repeated submissions must never reach a production service, so the gate is
an allowlist: loopback, *.localhost / *.test, and hosts named in CRASHCUSTOMER_ALLOWED_HOSTS.
"""
import os
import time
from urllib.parse import urlsplit

import httpx

from .config import BASE_URL

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class TargetError(ValueError):
    pass


def allowed_hosts() -> set[str]:
    extra = os.getenv("CRASHCUSTOMER_ALLOWED_HOSTS", "")
    return LOCAL_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()}


def check_target(url: str) -> str:
    if not url:
        raise TargetError("프로젝트에 브라우저 테스트 주소가 없습니다. 프로젝트 정보에서 주소를 등록해주세요.")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        raise TargetError("http(s) 주소만 실행할 수 있습니다.")
    if not (host in allowed_hosts() or host.endswith(".localhost") or host.endswith(".test")):
        raise TargetError(f"{host}은(는) 허용된 테스트 호스트가 아닙니다. 운영 서비스 보호를 위해 로컬 주소만 실행하며, "
                          "스테이징 서버는 .env의 CRASHCUSTOMER_ALLOWED_HOSTS에 직접 추가해야 합니다.")
    own = urlsplit(BASE_URL)
    if (parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)) == (own.hostname, own.port or 80):
        raise TargetError("CrashCustomer 자신의 주소는 테스트 대상으로 등록할 수 없습니다.")
    return url


async def health_check(url: str, client: httpx.AsyncClient | None = None) -> dict:
    check_target(url)
    started = time.perf_counter()
    owns = client is None
    client = client or httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False)
    try:
        response = await client.get(url)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        ok = response.status_code < 400 or response.status_code in {401, 403}
        return {"ok": ok, "status": response.status_code, "latency_ms": elapsed,
                "message": "응답을 확인했습니다." if ok else f"HTTP {response.status_code} 응답입니다. 앱이 정상 기동했는지 확인해주세요."}
    except httpx.HTTPError as error:
        return {"ok": False, "status": None, "latency_ms": None,
                "message": f"연결하지 못했습니다 ({type(error).__name__}). 앱을 먼저 실행해주세요."}
    finally:
        if owns:
            await client.aclose()
