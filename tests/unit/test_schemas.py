from common.schemas import ChatRequest, TurnCompletedEvent


def test_chat_request_trims_message() -> None:
    payload = ChatRequest(message="  hello  ")
    assert payload.message == "hello"


def test_turn_completed_event_accepts_minimal_payload() -> None:
    event = TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message="hello",
        assistant_message="hi",
    )
    assert event.agent_id == "maxima"

