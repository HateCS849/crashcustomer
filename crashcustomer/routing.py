"""Bounded intent routing. Routing never starts a test or invents target URLs."""
import re

# Greeting / "who are you" phrases. A message is small talk only when nothing else remains after removing them.
SMALL_TALK = re.compile(
    r"안녕(하세요|하십니까|하신가요)?|반가(워요?|워|습니다)|"
    r"(너|넌|당신)(은|는)?\s*(누구|뭐)(야|니|예요|에요|세요|입니까|인가요)?|누구(야|세요|예요|신가요)|"
    r"(뭐|무엇을?)\s*(하는|할 수 있는)\s*(에이전트|거|것)(야|예요|이에요|인가요)?|"
    r"\b(hello|hi|hey|who are you)\b", re.I)
FILLER = re.compile(r"[\s,.!?~…·]*")
USAGE_WORDS = ("사용법", "어떻게 사용", "무슨 기능")
LAB_WORDS = ("실험실", "커넥션 풀", "장애 전파", "시뮬레이션")


def is_small_talk(text: str) -> bool:
    return bool(SMALL_TALK.search(text)) and FILLER.fullmatch(SMALL_TALK.sub("", text)) is not None


def route_goal(message: str) -> dict:
    text = message.strip()
    if is_small_talk(text) or any(w in text for w in USAGE_WORDS):
        return {"intent": "help", "answer": "프로젝트를 선택하고 목표를 입력하면 기준 경로 확인 → 자료 검색 → 장애 계획 → 재현 → API 측정 → 결과 검증을 진행합니다. 동시 로그인과 DB 풀의 구조 실험은 로컬 실험실을 선택하세요."}
    if any(w in text for w in LAB_WORDS):
        return {"intent": "lab", "answer": "로컬 실험실에서 동시 로그인 수·DB 풀·외부 지연을 설정하세요. Python 구조 재현 실험입니다."}
    return {"intent": "campaign", "answer": "등록 프로젝트에서 목표 경로와 장애 대응을 점검합니다."}
