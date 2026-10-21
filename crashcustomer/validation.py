"""Evidence and coverage checks independent of model opinions."""


def coverage(candidates, scenarios, limit):
    available = list(dict.fromkeys(c["fault"] for c in candidates))
    planned = list(dict.fromkeys(c["fault"] for c in scenarios))
    required = available[:limit]
    by_id = {c["candidate_id"]: c for c in candidates}
    misplaced = [fault for fault in required
                 if any(c["fault"] == fault and c.get("method") in {"POST", "PUT", "PATCH", "DELETE"} for c in candidates)
                 and any(s["fault"] == fault for s in scenarios)
                 and not any(s["fault"] == fault and by_id.get(s.get("candidate_id"), s).get("method") in {"POST", "PUT", "PATCH", "DELETE"} for s in scenarios)]
    return {"available": available, "required": required, "planned": planned,
            "missing": [f for f in required if f not in planned or f in misplaced], "misplaced": misplaced,
            "out_of_budget": available[limit:]}


def complete_plan(candidates, scenarios, limit):
    """Preserve valid model choices; reserve space for each required fault type."""
    by_id = {c["candidate_id"]: c for c in candidates}
    required = coverage(candidates, scenarios, limit)["required"]
    chosen = []
    for fault in required:
        options = [c for c in candidates if c["fault"] == fault]
        mutations = [c for c in options if c.get("method") in {"POST", "PUT", "PATCH", "DELETE"}]
        preferred = mutations or options
        selected = next((s for s in scenarios if s.get("candidate_id") in {c["candidate_id"] for c in preferred}), None)
        fallback = next(iter(preferred), None)
        if selected:
            chosen.append({**by_id[selected["candidate_id"]], "reason": selected.get("reason", "계획 선택")})
        elif fallback:
            chosen.append({**fallback, "reason": "누락 검사 후 필수 장애 유형·대표 변경 경로 보완", "source": "coverage_repair"})
    for s in scenarios:
        if len(chosen) >= limit:
            break
        if s.get("candidate_id") in by_id and not any(c["candidate_id"] == s["candidate_id"] for c in chosen):
            chosen.append({**by_id[s["candidate_id"]], "reason": s.get("reason", "계획 선택")})
    return chosen[:limit]


def validate_campaign(campaign, runs):
    issues = []
    if not runs or runs[0].get("result") != "goal_completed":
        issues.append("장애 없는 기준 경로의 목표 완료를 확인하지 못했습니다.")
    planned = campaign.get("coverage", {})
    if planned.get("missing") or planned.get("out_of_budget"):
        issues.append("예산 내 계획 또는 전체 장애 유형 중 미점검 범위가 있습니다.")
    if not campaign.get("scenarios"):
        issues.append("실행 가능한 장애 시나리오가 없습니다.")
    for sc in campaign.get("scenarios", []):
        if sc.get("status") != "completed" or not sc.get("injected"):
            issues.append(f"{sc['label']}: 실행 완료 또는 실제 장애 적용을 확인하지 못했습니다.")
        elif sc.get("result") not in {"goal_completed", "findings_confirmed"}:
            issues.append(f"{sc['label']}: 목표 완료 또는 결함 재현을 확인하지 못했습니다.")
        for f in sc.get("findings", []):
            if f.get("verification") != "confirmed" or not all(f.get(k) for k in ("before", "after", "actual")):
                issues.append(f"{sc['label']}: 재현 또는 화면 근거가 부족합니다.")
    probes = campaign.get("probes") or {}
    for name in ("load", "validation"):
        p = probes.get(name)
        if p and p.get("valid") is False:
            issues.append(f"{name}: 인증·응답 선행 조건을 충족하지 못해 측정이 무효입니다.")
    inputs = probes.get("validation") or {}
    if inputs.get("accepted_invalid") or inputs.get("unverified_5xx"):
        issues.append("입력 검증에서 수락된 잘못된 요청 또는 재확인하지 못한 5xx가 있어 추가 확인이 필요합니다.")
    if campaign.get("error"):
        issues.append("실행 오류가 있습니다.")
    summary = campaign.get("summary")
    if summary:
        findings = [f for r in runs for f in r.get("findings", [])] + probes.get("findings", [])
        expected = {"scenarios": max(0, len(runs) - 1), "findings": len(findings),
                    "confirmed": sum(f.get("verification") in {"confirmed", "measured"} for f in findings)}
        if any(summary.get(key) != value for key, value in expected.items()):
            issues.append("보고서 요약 수치가 원본 관찰·측정 기록과 일치하지 않습니다.")
    return {"sufficient": not issues, "issues": list(dict.fromkeys(issues)),
            "verdict": "inconclusive" if not runs or runs[0].get("result") != "goal_completed" else "partial" if issues else "complete",
            "scope": "이번 목표 경로와 선택한 장애 조건에 대한 검증이며 전체 서비스의 무결성 보증은 아닙니다."}
