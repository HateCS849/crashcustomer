"""Generate a technical readiness report from measured artifacts, without filling missing evidence."""
import json
import statistics
from pathlib import Path

DOCS = Path("docs")


def read(name):
    path = DOCS / name
    return json.loads(path.read_text()) if path.exists() else None


def main():
    context = read("benchmark-context-20261021.json")
    planning = read("benchmark-planning-20261021.json")
    rule = read("evaluation-submission-rule-20261021.json")
    ai = read("evaluation-submission-ai-20261021.json")
    validation = read("validation-submission-20261021.json")
    cloud = read("verification-langsmith-campaign-20261021.json") or {"status": "not_verified"}
    ready = bool(validation and validation["returncode"] == 0 and context and context.get("complete")
                 and planning and planning.get("complete") and rule and ai)
    checks = bool(ready and rule["passed"] == rule["runs"]
                  and context["summary"]["full"]["errors"] == context["summary"]["compact"]["errors"] == 0
                  and all(not r["coverage"]["missing"] for r in planning["results"] if r["mode"] == "validated")
                  and ai["recall"] == 1 and ai["false_positives"] == 0 and all(not r.get("error") for r in ai["results"]))
    status = {"local_evidence_complete": ready, "langsmith_status": cloud["status"],
              "controlled_checks_passed": checks, "all_technical_evidence_complete": checks and cloud["status"] == "verified",
              "presentation_excluded": True, "external_submission_performed": False}
    (DOCS / "submission-status-20261021.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 제출 전 기술 검증 결과", "", "기준일: 2026-10-21 (Asia/Seoul). PPT 작성·발표 준비는 범위에서 제외했다.", "",
             "이 보고서는 원본 PDF의 기능 30·기술 요소 30·한계 설명 20·발표 20·실측 보너스 10 기준에 대응하는 기술 증거다. 점수를 자체 확정하는 문서는 아니다.", "",
             "## 상태", "", f"- 로컬 증거 정리: {'완료' if ready else '진행 중 또는 누락'}",
             f"- 통제된 검증 조건: {'통과' if checks else '진행 중 또는 추가 검토 필요'}",
             f"- LangSmith 실제 캠페인 수신: `{cloud['status']}`", "- PPT·발표: 요청에 따라 미작성", "- 외부 Git/Google Sheet 등록·공개 업로드: 수행하지 않음", "",
             "## PDF 평가 항목 대응", "", "| 평가 영역 | 구현·증거 | 범위·남은 사항 |", "|---|---|---|",
             "| 기능 구현 | 브라우저 탐색, 장애 주입, 계약 판정, 독립 재현, 입력·부하 검사, 충분성 검증 | 합성 앱에서 검증. 실제 서비스 전체의 무결성 보증 아님 |",
             "| 기술 요소 | FastAPI, LangGraph 역할 분리, 실제 MCP Prompt/Resource/Tool, RAG, 의존성 주입, SSE, 모듈화, 자동 테스트 | 병렬 LLM 토론·stateless Worker를 구현했다는 주장은 하지 않음 |",
             f"| 관측·평가 | 로컬 Trace·실행 ID·JSON 실측·회귀 테스트·LangSmith 검증 도구 | LangSmith 실제 수신 상태: {cloud['status']} |",
             "| 한계·트레이드오프 | ARCHITECTURE.md, ADR.md, 아래 실측 해석 | 대상 범위·비용·재현 한계를 명시 |",
             "| Before/After 보너스 근거 | 같은 관찰·목표·모델의 컨텍스트 축소와 계획 검증 비교 | 반복 횟수·순서·원본 데이터를 공개. 통계적 일반화 아님 |", ""]
    if validation:
        lines += ["## 자동 검증", "", f"- 단위·통합 테스트: {validation['passed']}개 통과, 종료 코드 {validation['returncode']}",
                  f"- JavaScript 구문 검사: {sum(c['passed'] for c in validation['javascript_syntax'])}/{len(validation['javascript_syntax'])}",
                  "- 환경·패키지 버전: [검증 메타데이터](validation-submission-20261021.json)", "- 원본 로그: [테스트 출력](tests-submission-20261021.txt)", ""]
    if context and context.get("complete"):
        s = context["summary"]
        lines += ["## 같은 입력: 컨텍스트 축소 전후", "", "실제 샘플 앱의 저장된 화면 상태 3개 × 3반복 × 2조건, 측정 18호출. 예열 1호출은 제외했다. 같은 상태·목표·모델·허용 행동·JSON 스키마를 사용하고 조건 순서를 교대로 배치했다.", "",
                  "| 항목 | Full 관찰·이력 | Compact |", "|---|---:|---:|"]
        for label, key in (("허용 행동 선택", "valid_decisions"), ("평균 입력 토큰", "mean_input_tokens"), ("평균 출력 토큰", "mean_output_tokens"), ("평균 지연(ms)", "mean_ms"), ("중앙 지연(ms)", "median_ms"), ("호출 오류", "errors")):
            lines.append(f"| {label} | {s['full'][key]} | {s['compact'][key]} |")
        lines += ["", f"- 평균 입력 토큰 감소: {s['input_token_reduction_percent']}%", f"- 같은 행동 종류·대상 선택: {s['same_kind_and_target_pairs']}/{s['pairs']}쌍",
                  "- 허용 행동 선택은 스키마·선택지 검증이며, 입력값의 모든 비즈니스 조건이나 전체 작업 성공률을 뜻하지 않는다.",
                  "- 평균 토큰은 성공한 호출 기준이다. 로컬 추론이므로 금전 비용을 추정하지 않았다. 지연은 OS·캐시·모델 출력 길이의 영향을 받으며 독립 머신 통제 실험이 아니다.",
                  "- [원본 결과](benchmark-context-20261021.json) · [고정 입력](context-fixtures-20261021.json)", ""]
    if planning and planning.get("complete"):
        lines += ["## 같은 입력: 계획 검증 전후", "", "같은 관찰 API·첨부 검색 근거·목표·모델로 조건별 3회 비교했다. 기존은 단발 계획이며, 개선 조건은 MCP 계획 Prompt·제한 재계획·유형 및 대표 변경 경로 검증·규칙 보완의 묶음이다.", "",
                  "| 조건 | 반복별 최종 유형 수 | 평균 LLM 호출 | 평균 입력 토큰 | 평균 지연(ms) |", "|---|---|---:|---:|---:|"]
        for mode, name in (("one_pass", "기존 단발"), ("validated", "검증·재계획")):
            rows = [r for r in planning["results"] if r["mode"] == mode]
            types = ", ".join(str(len(r["coverage"]["planned"])) + "/" + str(len(r["coverage"]["required"])) for r in rows)
            lines.append(f"| {name} | {types} | {statistics.mean(r['usage']['calls'] for r in rows):.2f} | {statistics.mean(r['usage']['input_tokens'] for r in rows):.2f} | {statistics.mean(r['ms'] for r in rows):.2f} |")
        lines += ["", "유형이 존재해도 변경 요청 후보가 있는데 조회 요청만 선택하면 보완 대상으로 처리한다. 규칙 보완이 개입한 경우 이를 LLM의 독자적인 발견이라고 표시하지 않는다. 계획 범위 충족은 실제 결함 탐지율과 별개다.",
                  "개선 조건은 호출·토큰·지연이 늘 수 있다. 이 비교는 묶음의 효과이며 각 구성 요소의 독립 효과를 분리하지 않았다. 최초 유형 전용 검증 기록도 보존했다.",
                  "[최종 원본](benchmark-planning-20261021.json) · [유형만 검사하던 중간 실험](benchmark-planning-types-only-20261021.json)", ""]
    if rule:
        lines += ["## 브라우저 실제 동작", "", f"- 내장 폼 규칙 기준선: 정상·개별 결함 4조건 × 3반복, {rule['passed']}/{rule['runs']} 통과. 탐지율 {rule['defect_recall']}, 오탐 {rule['false_positives']}, 실행 오류 {rule['execution_errors']}.", "- [규칙 기준선 원본](evaluation-submission-rule-20261021.json)"]
    if ai:
        lines += [f"- 현재 AI 캠페인: 탐지율 {ai['recall']}, 오탐 {ai['false_positives']}, 모델 호출 {ai['llm_calls']}회.",
                  "- 상태와 미점검 범위도 함께 확인해야 한다. 결함을 찾지 않았다는 이유만으로 완료라고 표시하지 않는다.", "- [현재 AI 캠페인 원본](evaluation-submission-ai-20261021.json)"]
    lines += ["", "이 수치는 결함을 심어둔 작은 샘플에 대한 결과다. 실제 서비스 일반화, 실제 Hangari의 내부 장애 원인, 과거 베타 운영 사실을 증명하지 않는다.", "",
              "## 설계·운영·재현 자료", "", "- [실제 구조와 데이터 흐름](ARCHITECTURE.md)", "- [설계 결정·대안·트레이드오프](ADR.md)", "- [설치·실행·기능·한계](../README.md)",
              "- [인증·SSE 검증](verification-upgrade-20261021.json)", "- [LangSmith 실제 수신 상태](verification-langsmith-campaign-20261021.json)",
              "- LangSmith 조회 구현 참고: [공식 SDK 조회 문서](https://docs.langchain.com/langsmith/export-traces), [필터·trace_id 문서](https://docs.langchain.com/langsmith/trace-query-syntax)", "",
              "## LangSmith 실제 수신을 마무리하는 조건", "", "예제 주문 앱의 합성 관찰·예제 운영 문서·모델 출력 전송에 대한 명시적 승인과 로컬 API 키 설정이 필요하다. 승인 전에는 외부 전송을 실행하지 않는다. 키는 채팅·제출 ZIP에 넣지 않는다.",
              "승인·설정 후 서버를 재시작하고 `python scripts/verify_langsmith.py --allow-synthetic-external-trace`를 실행한다. 합성 Run 생성·종료·조회와 실제 AI 캠페인 루트·LLM·MCP 하위 추적까지 확인해야 verified다.",
              "실행 후 이 보고서와 제출 ZIP을 다시 생성하면 수신 증거가 포함된다. API 키 미설정 또는 승인 대기 상태를 수신 성공으로 바꾸지 않는다."]
    (DOCS / "SUBMISSION_READINESS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__": main()
