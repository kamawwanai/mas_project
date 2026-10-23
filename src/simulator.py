"""Deterministic private preference service. Returns one answer at a time."""
from copy import deepcopy

TOPICS = {"interests", "pace", "comfort", "economy", "quiet", "location"}


class UserSimulator:
    def __init__(self, profile):
        self.__answers = deepcopy(profile.get("answers", {}))

    def answer(self, question):
        topic = question.get("topic")
        if topic not in TOPICS:
            return {"status": "unsupported_topic"}
        answer = self.__answers.get(topic)
        return {"status": "answered" if answer is not None else "unknown", "topic": topic, "answer": deepcopy(answer)}
