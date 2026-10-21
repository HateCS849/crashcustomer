"""Observable service-contract checks. These functions do not accept a fault identifier."""
import re
from collections import Counter
from urllib.parse import urlsplit


def successful(snapshot: dict) -> bool:
    return "신청이 완료되었습니다." in snapshot.get("statuses", [])


def check_contract(previous: dict | None, current: dict, action: dict | None) -> list[dict]:
    if not previous or not action or action["kind"] != "click":
        return []
    failures = [r for r in current.get("network", []) if r["method"] == "POST" and r["status"] >= 500]
    if not failures:
        return []
    common = {"step": current["step"], "before": previous["screenshot"],
              "after": current["screenshot"], "network": failures, "severity": "high"}
    if successful(current):
        return [{**common, "code": "false_success", "title": "저장 요청 실패를 성공으로 표시",
                 "expected": "서버가 요청을 수락한 경우에만 신청 완료를 표시해야 합니다.",
                 "actual": f"제출 응답이 HTTP {failures[-1]['status']}인데 신청 완료 메시지가 표시되었습니다."}]
    findings = []
    current_fields = {f["label"]: f for f in current["fields"]}
    lost = []
    for field in previous["fields"]:
        after = current_fields.get(field["label"])
        if not after:
            continue
        if field["type"] == "checkbox":
            if field["checked"] and not after["checked"]:
                lost.append(field["label"])
        elif field["type"] in {"text", "email", "tel", "textarea"} and field["value"] and after["value"] != field["value"]:
            lost.append(field["label"])
    if lost:
        findings.append({**common, "code": "input_loss", "title": "서버 오류 후 입력 내용 소실",
                         "expected": "일시적 오류 이후에도 입력값과 동의 상태를 유지해야 합니다.",
                         "actual": "유지되지 않은 항목: " + ", ".join(lost)})
    submit_buttons = [b for b in current["buttons"] if b["type"] == "submit"]
    if submit_buttons and all(b["disabled"] for b in submit_buttons) and not current["busy"]:
        findings.append({**common, "code": "retry_locked", "title": "오류 처리 후 재시도 버튼 잠김",
                         "expected": "오류 처리가 끝나면 다시 제출할 수 있어야 합니다.",
                         "actual": "로딩이 종료되었지만 제출 버튼이 비활성화되어 재시도할 수 없습니다."})
    return findings


# Registered applications: generic resilience contract, evaluated around injected failures.
SUCCESS_PATTERN = re.compile(r"완료|성공|저장되었|등록되었|접수되었|감사합니다|success|saved|thank you|created|confirmed", re.I)
ERROR_PATTERN = re.compile(r"오류|실패|에러|다시 시도|일시적|문제가 발생|못했|불러오지|확인할 수 없|만료|잠시 후|error|failed|failure|try again|unavailable|expired", re.I)
AUTH_PATTERN = re.compile(r"로그인|인증|세션|만료|login|log in|sign in|expired|unauthori", re.I)
BROKEN_PATTERN = re.compile(r"\bundefined\b|\bNaN\b|\[object Object\]|\bnull\b")
MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}
RETRY_STORM_LIMIT = 5
GENERIC_CONTRACT = [
    "사용자는 화면의 정상 경로로 목표 작업을 완료할 수 있어야 한다.",
    "요청이 실패하면 사용자에게 오류를 알려야 한다(조용한 실패 금지). 인증 만료는 다시 로그인하도록 안내해야 한다.",
    "요청이 실패해도 입력값은 유지되고, 처리 종료 후 다시 시도할 수 있어야 한다.",
    "요청이 실패했을 때 성공 메시지를 표시하면 안 된다.",
    "응답을 기다리는 동안 같은 요청을 중복 제출할 수 없어야 하며, 서버가 같은 요청을 두 번 처리하면 안 된다.",
    "응답 형식이 바뀌어도 undefined·NaN 같은 깨진 값을 표시하면 안 된다.",
    "실패한 요청을 지연 없이 반복 재시도하면 안 된다.",
    "화면 파일을 불러오지 못하면 사용자에게 알려야 한다.",
    "실패 처리 중 처리되지 않은 스크립트 예외나 끝나지 않는 로딩이 없어야 한다.",
]


def new_lines(previous: dict | None, current: dict) -> list[str]:
    before = set((previous or {}).get("text", "").splitlines())
    return [line.strip() for line in current.get("text", "").splitlines() if line.strip() and line not in before]


def generic_success(previous: dict | None, current: dict) -> bool:
    shown = current.get("statuses", []) + new_lines(previous, current)
    return any(SUCCESS_PATTERN.search(text) and not ERROR_PATTERN.search(text) for text in shown)


def lost_inputs(previous: dict, current: dict) -> list[str]:
    current_fields = {f["label"]: f for f in current["fields"]}
    lost = []
    for field in previous["fields"]:
        after = current_fields.get(field["label"])
        if not after:
            continue
        if field["type"] == "checkbox":
            if field["checked"] and not after["checked"]:
                lost.append(field["label"])
        elif field["type"] in {"text", "email", "tel", "textarea", "number", "url"} and field["value"] and after["value"] != field["value"]:
            lost.append(field["label"])
    return lost


def finding(common: dict, code: str, severity: str, title: str, expected: str, actual: str) -> dict:
    return {**common, "code": code, "severity": severity, "title": title, "expected": expected, "actual": actual}


def path_of(url: str) -> str:
    return urlsplit(url).path if "://" in url else url


def unrecovered(network: list[dict]) -> list[dict]:
    """Failures not followed by a success on the same endpoint (a quiet successful retry is correct behavior)."""
    failed = []
    for index, response in enumerate(network):
        if not (response["status"] >= 500 or response["status"] in {0, 401, 429}):
            continue
        key = (response["method"], path_of(response.get("url", "")))
        if not any(200 <= later["status"] < 300 and (later["method"], path_of(later.get("url", ""))) == key for later in network[index + 1:]):
            failed.append(response)
    return failed


def fault_effects(previous: dict | None, current: dict, common: dict) -> list[dict]:
    """Checks shared by the first screen and every click: what injected faults did to the page."""
    injected = current.get("injected", [])
    failures = [r for r in current.get("network", []) if r["status"] >= 500 or r["status"] in {0, 401, 429}]
    fresh = new_lines(previous, current)
    found = []
    if (failures or injected) and current.get("error_count", 0) > (previous or {}).get("error_count", 0):
        found.append(finding(common, "unhandled_error", "medium", "실패 처리 중 처리되지 않은 예외", "요청 실패와 응답 이상은 화면 코드에서 처리되어야 합니다.",
                             "스크립트 예외: " + "; ".join(current.get("console_errors", [])[-2:])[:400]))
    if injected and any(BROKEN_PATTERN.search(line) for line in fresh + current.get("statuses", [])):
        sample = next(line for line in fresh + current.get("statuses", []) if BROKEN_PATTERN.search(line))
        found.append(finding(common, "broken_render", "high", "응답 형식 변경 시 깨진 값 표시", "예상과 다른 응답은 오류로 처리하고 깨진 값을 보여주면 안 됩니다.",
                             f"화면에 표시된 문구: {sample[:160]}"))
    failed_paths = {path_of(r.get("url", "")) for r in failures}
    counts = Counter((r["method"], r["path"]) for r in current.get("requests_window", []) if r["path"] in failed_paths)
    storm = [(key, n) for key, n in counts.items() if n > RETRY_STORM_LIMIT]
    if storm:
        (method, path), n = storm[0]
        found.append(finding(common, "retry_storm", "medium", "실패 요청의 즉시 반복 재시도", "실패한 요청은 간격을 두고 제한된 횟수만 재시도해야 합니다.",
                             f"{method} {path} 요청이 한 화면 갱신 사이에 {n}회 반복되었습니다."))
    visible = current.get("alerts", []) + current.get("text", "").splitlines()
    if any(i["mode"] == "asset_block" for i in injected) and not any(ERROR_PATTERN.search(line) for line in visible):
        found.append(finding(common, "asset_failure_silent", "medium", "화면 파일 로딩 실패를 알리지 않음", "스크립트·스타일을 불러오지 못하면 새로고침 등 안내를 표시해야 합니다.",
                             "차단된 파일: " + ", ".join(sorted({i['path'] for i in injected if i['mode'] == 'asset_block'}))[:300]))
    return found


def feedback_lines(previous: dict | None, current: dict) -> list[str]:
    return current.get("alerts", []) + [line for line in new_lines(previous, current) if ERROR_PATTERN.search(line)]


def check_initial(current: dict) -> list[dict]:
    """First screen of a registered app: faults on page-load requests and assets."""
    if not current.get("injected"):
        return []
    common = {"step": current["step"], "before": current["screenshot"], "after": current["screenshot"],
              "network": current.get("network", []), "injected": current["injected"]}
    found = fault_effects(None, current, common)
    failures = unrecovered(current.get("network", []))
    if failures and not feedback_lines(None, current) and not any(f["code"] == "asset_failure_silent" for f in found):
        found.append(finding(common, "silent_failure", "medium", "실패를 알리지 않는 화면", "화면을 여는 중 요청이 실패하면 사용자에게 알려야 합니다.",
                             f"{failures[-1]['method']} {path_of(failures[-1].get('url', ''))} 요청이 실패했지만 안내가 없습니다."))
    return found


def check_resilience(previous: dict | None, current: dict, action: dict | None) -> list[dict]:
    if not previous or not action or action["kind"] != "click":
        return []
    injected = current.get("injected", [])
    failures = [r for r in current.get("network", []) if r["status"] >= 500 or r["status"] in {0, 401, 429}]
    common = {"step": current["step"], "before": previous["screenshot"], "after": current["screenshot"],
              "network": failures, "injected": injected}
    findings = fault_effects(previous, current, common)
    failures = unrecovered(current.get("network", []))
    for flight in current.get("mid_flight", []):
        if flight["enabled"] and not flight["busy"]:
            findings.append(finding(common, "double_submit", "high", "응답 대기 중 중복 제출 가능",
                                    "요청 처리 중에는 같은 버튼을 다시 누를 수 없거나 진행 상태를 표시해야 합니다.",
                                    f"요청이 끝나기 전에도 '{flight['label']}' 버튼을 다시 누를 수 있었습니다."))
            break
    accepted = Counter((r["method"], path_of(r.get("url", ""))) for r in current.get("network", [])
                       if r["method"] in MUTATIONS and 200 <= r["status"] < 300)
    duplicated = [(key, n) for key, n in accepted.items() if n > 1]
    if duplicated:
        (method, path), n = duplicated[0]
        findings.append(finding(common, "duplicate_processing", "high", "같은 변경 요청의 중복 성공 응답",
                                "같은 사용자 동작은 서버에서 한 번만 처리되어야 합니다(멱등성).",
                                f"한 번의 제출 동작에서 {method} {path} 요청이 {n}번 2xx 응답을 받았습니다. 실제 데이터 중복 생성은 별도 검증이 필요합니다."))
    if current.get("busy_timeout"):
        findings.append(finding(common, "stuck_loading", "medium", "끝나지 않는 로딩 상태",
                                "요청이 실패하거나 지연되어도 로딩 표시는 종료되어야 합니다.",
                                "8초가 지나도 aria-busy 로딩 상태가 해제되지 않았습니다."))
    if not failures:
        return findings
    if generic_success(previous, current):
        findings.append(finding(common, "false_success", "high", "실패한 요청을 성공으로 표시",
                                "서버가 요청을 수락한 경우에만 성공 상태를 표시해야 합니다.",
                                f"요청이 실패했는데({failures[-1]['method']} → {failures[-1]['status'] or '연결 끊김'}) 성공 문구가 표시되었습니다."))
        return findings
    feedback = feedback_lines(previous, current)
    if not feedback:
        findings.append(finding(common, "silent_failure", "medium", "실패를 알리지 않는 화면",
                                "요청이 실패하면 사용자에게 오류와 다음 행동을 알려야 합니다.",
                                "요청이 실패했지만 새 오류 메시지나 경고가 표시되지 않았습니다."))
    elif any(r["status"] == 401 for r in failures) and not any(AUTH_PATTERN.search(line) for line in feedback):
        findings.append(finding(common, "auth_expiry_unhandled", "medium", "인증 만료를 일반 오류로 처리",
                                "401 응답에는 다시 로그인하도록 안내해야 합니다.",
                                "인증 만료(401)에 표시된 안내: " + feedback[0][:160]))
    lost = lost_inputs(previous, current)
    if lost:
        findings.append(finding(common, "input_loss", "high", "요청 실패 후 입력 내용 소실",
                                "일시적 오류 이후에도 입력값과 선택 상태를 유지해야 합니다.",
                                "유지되지 않은 항목: " + ", ".join(lost)))
    submit_buttons = [b for b in current["buttons"] if b["type"] == "submit"]
    if submit_buttons and all(b["disabled"] for b in submit_buttons) and not current["busy"]:
        findings.append(finding(common, "retry_locked", "high", "실패 후 재시도 버튼 잠김",
                                "오류 처리가 끝나면 다시 제출할 수 있어야 합니다.",
                                "로딩이 끝났지만 제출 버튼이 비활성화되어 재시도할 수 없습니다."))
    return findings
