from services.agent_gateway_v3.app.services.turn_assembler import TurnAssembler


def test_turn_assembler_collects_text_and_usage() -> None:
    assembler = TurnAssembler()
    assembler.add_event(
        {
            "usage_metadata": {
                "prompt_token_count": 10,
                "candidates_token_count": 8,
                "thoughts_token_count": 3,
                "total_token_count": 21,
            }
        }
    )
    assembler.add_text("hello")
    assembler.add_text(" world")

    assert assembler.reply_text() == "hello world"
    assert assembler.usage["total_token_count"] == 21
    assert assembler.usage["token_counts"]["total_token_count"] == 21
    assert assembler.usage["billable_tokens"] == {
        "input_text_image_video": 10,
        "input_audio": 0,
        "output_including_thinking": 11,
    }
    assert assembler.usage["estimated_cost_usd"] == 0.00004875
    assert assembler.raw_events == [
        {
            "usage_metadata": {
                "prompt_token_count": 10,
                "candidates_token_count": 8,
                "thoughts_token_count": 3,
                "total_token_count": 21,
            }
        }
    ]
