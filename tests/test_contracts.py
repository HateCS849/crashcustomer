import copy

from crashcustomer.checks import check_contract
from crashcustomer.models import Action, RunRequest
from crashcustomer.store import Store
import pytest
from pydantic import ValidationError


def snapshot():
    return {"step": 3, "screenshot": "/before.png", "network": [], "statuses": [], "busy": False,
            "fields": [{"label": "이메일", "type": "email", "value": "qa@example.com", "checked": False}],
            "buttons": [{"label": "제출", "type": "submit", "disabled": False}]}


def test_transient_server_failure_is_not_itself_a_bug():
    before = snapshot()
    after = copy.deepcopy(before)
    after["network"] = [{"method": "POST", "status": 503}]
    assert check_contract(before, after, {"kind": "click"}) == []


def test_loading_button_is_not_misclassified_as_locked():
    before = snapshot()
    after = copy.deepcopy(before)
    after.update(network=[{"method": "POST", "status": 503}], busy=True)
    after["buttons"][0]["disabled"] = True
    assert check_contract(before, after, {"kind": "click"}) == []


@pytest.mark.parametrize("change,expected", [("reset", "input_loss"), ("lock", "retry_locked"), ("success", "false_success")])
def test_contract_violations_require_observed_evidence(change, expected):
    before = snapshot()
    after = copy.deepcopy(before)
    after["network"] = [{"method": "POST", "status": 503}]
    if change == "reset":
        after["fields"][0]["value"] = ""
    elif change == "lock":
        after["buttons"][0]["disabled"] = True
    else:
        after["statuses"] = ["신청이 완료되었습니다."]
    assert [f["code"] for f in check_contract(before, after, {"kind": "click"})] == [expected]


def test_sessions_are_isolated_and_replay_resets_server_state(tmp_path):
    store = Store(tmp_path)
    original, replay = store.create_session("reset"), store.create_session("reset")
    applicant = {"name": "테스터", "email": "qa@example.com", "consent": True}
    assert store.submit(original, applicant)[0] == 503
    assert store.submit(original, applicant)[0] == 201
    assert store.submit(replay, applicant)[0] == 503
    assert store.get_session(replay)["submission"] is None


def test_browser_action_cannot_contain_executable_code():
    with pytest.raises(ValidationError):
        Action(kind="evaluate", target="body", value="document.cookie", reason="test")
    with pytest.raises(ValidationError):
        Action(kind="click", target="제출", reason="test", javascript="alert(1)")


def test_run_budget_and_fault_are_validated():
    with pytest.raises(ValidationError):
        RunRequest(max_steps=999)
    with pytest.raises(ValidationError):
        RunRequest(fault="unknown")
