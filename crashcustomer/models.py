from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

Fault = Literal["healthy", "reset", "locked", "false_success"]
Backend = Literal["rule", "ollama"]

FAULTS = {
    "healthy": {"label": "정상 버전", "description": "일시적 오류 후 입력을 유지하고 재시도합니다."},
    "reset": {"label": "입력값 소실", "description": "서버 오류가 발생하면 입력 내용이 사라집니다."},
    "locked": {"label": "재시도 불가", "description": "오류 이후 제출 버튼이 잠긴 상태로 남습니다."},
    "false_success": {"label": "거짓 성공", "description": "저장에 실패했는데 완료 메시지를 표시합니다."},
}

CONTRACT = [
    "유효한 이름과 이메일, 필수 동의를 입력하면 행사 참가 신청을 완료할 수 있다.",
    "일시적인 서버 오류가 발생해도 입력값과 필수 동의는 유지되어야 한다.",
    "오류 처리 종료 후 제출 버튼을 다시 사용할 수 있어야 한다.",
    "신청 요청이 실패했을 때 '신청이 완료되었습니다.'라는 성공 상태를 표시하면 안 된다.",
]
DEFAULT_GOAL = "행사 참가 신청을 완료하고, 오류가 발생하면 입력 보존과 재시도 가능 여부를 확인해줘."


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fault: Fault = "reset"
    backend: Backend = "rule"
    goal: str = Field(default=DEFAULT_GOAL, min_length=5, max_length=1500)
    max_steps: int = Field(default=16, ge=3, le=30)
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class ProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1500)
    base_url: str = Field(default="", max_length=2048)
    browser_url: str = Field(default="", max_length=2048)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("프로젝트 이름을 입력해주세요.")
        return value

    @field_validator("base_url", "browser_url")
    @classmethod
    def validate_url(cls, value):
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
                raise ValueError("인증정보·쿼리·프래그먼트가 없는 http(s) 주소를 입력해주세요.")
            try:
                parsed.port
            except ValueError as error:
                raise ValueError("올바른 포트 번호를 입력해주세요.") from error
        return value


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["fill", "click", "check", "select", "finish"]
    target: str = Field(default="", max_length=160)
    value: str = Field(default="", max_length=400)
    reason: str = Field(min_length=1, max_length=800)


class Applicant(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=200)
    consent: bool
