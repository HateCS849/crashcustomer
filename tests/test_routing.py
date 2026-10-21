import pytest

from crashcustomer.routing import route_goal


@pytest.mark.parametrize("message", [
    "안녕", "안녕, 너는 누구야?", "안녕하세요!", "너는 누구야?", "넌 누구야", "반가워요~ 누구세요?",
    "hello", "Hi, who are you?", "뭐 하는 에이전트야?", "사용법 알려줘",
])
def test_small_talk_and_usage_questions_get_guidance(message):
    assert route_goal(message)["intent"] == "help"


@pytest.mark.parametrize("message", [
    "안녕, 주문 폼 작성해서 주문 완료해줘",
    "원두 정기배송 주문 양식을 작성해 주문을 완료해줘.",
    "화면의 주요 입력 양식을 작성해 제출하고 결과를 확인해줘.",
    "this checkout is broken, hi",
])
def test_messages_with_a_task_start_a_campaign(message):
    assert route_goal(message)["intent"] == "campaign"


def test_lab_requests_are_routed_to_the_lab():
    assert route_goal("커넥션 풀 장애 전파 실험하고 싶어")["intent"] == "lab"
