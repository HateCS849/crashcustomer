"""API-level probes for a registered app, built from requests the browser actually made.

validation  invalid variants of each observed JSON body (empty, missing field, wrong type, oversize);
            a 5xx answer is re-sent once and reported only if it repeats.
load        staged concurrent GETs on observed read endpoints and the page itself; reports the first
            stage whose error rate or P95 latency breaks the target.

Only GET is sent under load. Validation sends only invalid bodies, but a lenient server may still
accept one (for example a number where a string was expected), so it runs only on allowed test hosts.
"""
import asyncio
import math
import time
from urllib.parse import urlsplit

import httpx

MAX_VALIDATION_REQUESTS = 30
OVERSIZE = "x" * 10_000


def percentile(values: list[float], fraction: float) -> float | None:
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 1) if values else None


def invalid_variants(body: dict) -> list[tuple[str, dict]]:
    variants = [("빈 본문", {})]
    for key, value in body.items():
        variants.append((f"{key} 누락", {k: v for k, v in body.items() if k != key}))
        if isinstance(value, bool):
            wrong = "not-a-boolean"
        elif isinstance(value, (int, float)):
            wrong = "not-a-number"
        elif isinstance(value, str):
            variants.append((f"{key} 1만 자", {**body, key: OVERSIZE}))
            wrong = {"unexpected": "object"}
        else:
            wrong = "unexpected-string"
        variants.append((f"{key} 형식 오류", {**body, key: wrong}))
    return variants


async def validation_probe(client: httpx.AsyncClient, requests: list[dict]) -> dict:
    results, failing, sent = [], {}, 0
    auth_failed = False
    for request in requests:
        if request["method"] not in {"POST", "PUT", "PATCH"} or not isinstance(request.get("body"), dict):
            continue
        for label, body in invalid_variants(request["body"]):
            if sent >= MAX_VALIDATION_REQUESTS:
                break
            sent += 1
            try:
                status = (await client.request(request["method"], request["path"], json=body)).status_code
            except httpx.HTTPError as error:
                status, label = 0, f"{label} ({type(error).__name__})"
            auth_failed |= status in {401, 403}
            confirmed = None
            if status >= 500 and sent < MAX_VALIDATION_REQUESTS:
                sent += 1
                try:
                    confirmed = (await client.request(request["method"], request["path"], json=body)).status_code >= 500
                except httpx.HTTPError:
                    confirmed = True
            results.append({"method": request["method"], "path": request["path"], "variant": label, "status": status, "confirmed": confirmed})
            if confirmed:
                failing.setdefault((request["method"], request["path"]), []).append(f"{label} → {status}")
    findings = [{"code": "input_validation_500", "severity": "medium", "verification": "confirmed",
                 "title": "잘못된 입력에 서버 오류(5xx) 응답",
                 "expected": "잘못된 요청은 4xx와 이유를 돌려주고 서버 오류로 끝나면 안 됩니다.",
                 "actual": f"{method} {path}: 잘못된 입력 {len(cases)}종이 재전송에서도 5xx ({', '.join(cases[:4])}{' 외' if len(cases) > 4 else ''})"}
                for (method, path), cases in failing.items()]
    invalid = auth_failed or any(r["status"] == 0 or 300 <= r["status"] < 400 for r in results)
    return {"requests": sent, "results": results, "findings": findings, "valid": not invalid,
            "reason": "인증·연결·리디렉션 때문에 입력 검증이 무효입니다." if invalid else None,
            "accepted_invalid": sum(200 <= r["status"] < 300 for r in results),
            "unverified_5xx": sum(r["status"] >= 500 and r["confirmed"] is None for r in results)}


async def load_probe(client: httpx.AsyncClient, paths: list[str], stages: list[int], slo_ms: int,
                     requests_per_user: int = 3, expect_json_paths: list[str] | None = None) -> dict:
    measured, breach = [], None
    preflight = []
    expect_json_paths = expect_json_paths or []
    def good(response, path):
        if not 200 <= response.status_code < 300:
            return False
        if path in expect_json_paths:
            try:
                return "json" in response.headers.get("content-type", "") and isinstance(response.json(), (dict, list))
            except ValueError:
                return False
        return True
    for path in paths:
        try:
            response = await client.get(path)
            preflight.append({"path": path, "status": response.status_code, "valid": good(response, path)})
        except httpx.HTTPError:
            preflight.append({"path": path, "status": 0, "valid": False})
    if not all(p["valid"] for p in preflight):
        return {"valid": False, "reason": "사전 응답 검증 실패: 인증 또는 기대 응답을 확인해주세요.",
                "preflight": preflight, "paths": paths, "stages": [], "slo_ms": slo_ms, "saturation_users": None, "findings": []}
    valid = True
    for users in stages:
        durations, errors = [], 0

        async def user():
            nonlocal errors, valid
            for index in range(requests_per_user):
                path = paths[index % len(paths)]
                started = time.perf_counter()
                try:
                    response = await client.get(path)
                    status = response.status_code
                    invalid = not good(response, path)
                except httpx.HTTPError:
                    status = 0
                    invalid = True
                durations.append((time.perf_counter() - started) * 1000)
                errors += invalid
                if status in {401, 403} or (200 <= status < 300 and invalid):
                    valid = False

        started = time.perf_counter()
        await asyncio.gather(*(user() for _ in range(users)))
        elapsed = time.perf_counter() - started
        total = users * requests_per_user
        stage = {"users": users, "requests": total, "error_rate": round(errors / total, 3),
                 "p50_ms": percentile(durations, .5), "p95_ms": percentile(durations, .95), "max_ms": round(max(durations), 1),
                 "throughput_rps": round(total / elapsed, 1) if elapsed else None}
        measured.append(stage)
        if breach is None and (stage["error_rate"] > .05 or stage["p95_ms"] > slo_ms):
            breach = stage
    findings = []
    if breach and valid:
        findings.append({"code": "load_slo_breach", "severity": "medium", "verification": "measured",
                         "title": "동시 사용자 증가 시 응답 목표 초과",
                         "expected": f"동시 사용자 {stages[-1]}명까지 오류율 5% 이하, P95 {slo_ms}ms 이하여야 합니다.",
                         "actual": f"동시 {breach['users']}명에서 P95 {breach['p95_ms']}ms, 오류율 {breach['error_rate'] * 100:.1f}%"})
    return {"valid": valid, "reason": None if valid else "측정 중 인증 또는 응답 형식 검증 실패", "preflight": preflight, "paths": paths, "slo_ms": slo_ms, "stages": measured, "saturation_users": breach["users"] if breach else None, "findings": findings}


def load_targets(target_url: str, requests: list[dict]) -> list[str]:
    page = urlsplit(target_url).path or "/"
    reads = [r["path"] + ("?" + r["query"] if r.get("query") else "") for r in requests if r["method"] == "GET"]
    return list(dict.fromkeys(reads + [page]))


def stages_for(max_users: int) -> list[int]:
    return [users for users in (1, 5, 10, 20, 50) if users < max_users] + [max_users]
