from services.agent_gateway_v3.app.services.request_context import build_request_context


def test_request_context_uses_server_turn_id_when_client_turn_id_is_supplied() -> None:
    context = build_request_context("maxima", client_turn_id="client-controlled")

    assert context.turn_id.startswith("turn-")
    assert context.turn_id != "client-controlled"
