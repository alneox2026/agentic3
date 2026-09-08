import httpx

from common.diagnostics import safe_http_response_details


def test_safe_http_response_details_omits_non_json_body() -> None:
    response = httpx.Response(
        500,
        text="secret user prompt and assistant response",
        headers={"content-type": "text/plain"},
    )

    details = safe_http_response_details(response)

    assert details["body_excerpt"] == "<non_json_body_omitted>"
    assert details["body_length"] > 0
    assert "secret user prompt" not in str(details)


def test_safe_http_response_details_redacts_sensitive_json_fields() -> None:
    response = httpx.Response(
        500,
        json={
            "detail": {
                "message": "secret user prompt",
                "text": "secret assistant response",
                "safe_reason": "upstream failure",
            }
        },
    )

    details = safe_http_response_details(response)
    body_excerpt = details["body_excerpt"]

    assert "upstream failure" in body_excerpt
    assert "secret user prompt" not in body_excerpt
    assert "secret assistant response" not in body_excerpt
    assert "<redacted>" in body_excerpt
