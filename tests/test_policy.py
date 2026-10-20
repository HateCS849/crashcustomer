import json

import httpx
import pytest
from pydantic import ValidationError

from crashcustomer.policy import OllamaPolicy, RulePolicy, available_actions


def observation():
    return {"fields": [{"label": "이름", "type": "text", "value": "", "checked": False,
                        "required": True, "disabled": False}], "buttons": [],
            "screenshot": "/private-screenshot.png", "aria": "textbox 이름", "network": []}


async def test_ollama_receives_observation_and_schema_without_fault_answer(monkeypatch):
    original = httpx.AsyncClient

    def handler(request):
        payload = json.loads(request.content)
        prompt = json.loads(payload["messages"][1]["content"])
        assert set(prompt) == {"goal", "service_contract", "observation", "choices", "previous_actions"}
        assert "screenshot" not in prompt["observation"]
        assert payload["stream"] is False
        assert payload["format"]["properties"]["action_id"]["enum"] == ["a0"]
        return httpx.Response(200, json={"message": {"content": json.dumps({
            "action_id": "a0", "value": "테스터", "reason": "이름 입력"})},
            "prompt_eval_count": 120, "eval_count": 30})

    monkeypatch.setattr("crashcustomer.policy.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    action, usage = await OllamaPolicy().decide("신청을 완료해줘", observation(), [])
    assert action.target == "이름"
    assert usage == {"calls": 1, "input_tokens": 120, "output_tokens": 30}


async def test_invalid_model_output_does_not_silently_fall_back_to_rules(monkeypatch):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"message": {"content": '{"kind":"execute_code"}'}}))
    monkeypatch.setattr("crashcustomer.policy.httpx.AsyncClient", lambda **kw: original(transport=transport, **kw))
    with pytest.raises(ValidationError):
        await OllamaPolicy().decide("신청을 완료해줘", observation(), [])


async def test_rule_baseline_uses_rendered_fields():
    action, usage = await RulePolicy().decide("신청", observation(), [])
    assert action.kind == "fill" and action.target == "이름"
    assert usage == {}


def test_model_cannot_submit_an_incomplete_form():
    snapshot = observation()
    snapshot["buttons"] = [{"label": "제출", "disabled": False}]
    assert all(c["kind"] != "click" for c in available_actions(snapshot))
    snapshot["fields"][0].update(value="테스터", valid=True)
    assert [c["kind"] for c in available_actions(snapshot)] == ["click"]
