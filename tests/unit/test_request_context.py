from services.agent_gateway_v3.app.services.request_context import build_request_context


def test_request_context_uses_server_turn_id_when_client_turn_id_is_supplied() -> None:
    context = build_request_context("maxima", client_turn_id="client-controlled")

    assert context.turn_id.startswith("turn-")
    assert context.turn_id != "client-controlled"


def test_request_context_generates_idempotent_turn_id_for_same_client_turn_id() -> None:
    context1 = build_request_context("maxima", client_turn_id="client-turn-42", user_id="user-A")
    context2 = build_request_context("maxima", client_turn_id="client-turn-42", user_id="user-A")
    context3 = build_request_context("maxima", client_turn_id="client-turn-42", user_id="user-B")
    assert context1.turn_id == context2.turn_id
    assert context1.turn_id != context3.turn_id
    assert context1.request_id != context2.request_id

