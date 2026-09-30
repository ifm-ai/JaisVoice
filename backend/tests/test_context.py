from livekit.agents import llm

from horizon_voice.context import compact_context


def test_compaction_preserves_current_goal_pairs_and_original_audit():
    context = llm.ChatContext()
    context.add_message(role="system", content="system instruction")
    context.add_message(role="user", content="old forgotten request")
    context.add_message(role="assistant", content="old reply")
    context.add_message(role="user", content="previous request")
    context.add_message(role="assistant", content="previous reply")
    context.add_message(role="user", content="current corrected request in Arabic العربية")
    for index in range(12):
        context.items.extend([
            llm.FunctionCall(call_id=f"call-{index}", name="browser_snapshot", arguments="{}"),
            llm.FunctionCallOutput(call_id=f"call-{index}", name="browser_snapshot", output=f"Page {index}: " + "content " * 4000, is_error=False),
        ])
    original = context.to_dict()
    compacted = compact_context(context)
    assert context.to_dict() == original
    users = [item.text_content for item in compacted.items if item.type == "message" and item.role == "user"]
    assert users == ["old forgotten request", "previous request", "current corrected request in Arabic العربية"]
    calls = [item.call_id for item in compacted.items if item.type == "function_call"]
    outputs = [item for item in compacted.items if item.type == "function_call_output"]
    assert calls == [item.call_id for item in outputs]
    assert outputs[-1].output.startswith("Page 11: ")
    assert 10000 <= len(outputs[-1].output) < 10200
    assert sum(len(item.output) for item in outputs[:-1]) < 7500


def test_long_conversation_keeps_eight_recent_prompts_and_system():
    context = llm.ChatContext()
    context.add_message(role='system', content='Instructions')
    for index in range(12):
        context.add_message(role='user', content=str(index))
        context.add_message(role='assistant', content='Response ' + str(index))
    compacted = compact_context(context)
    assert [i.text_content for i in compacted.items if getattr(i, 'role', '') == 'user'] == list(map(str, range(4, 12)))
    assert compacted.items[0].text_content == 'Instructions'
