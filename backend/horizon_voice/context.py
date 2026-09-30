from livekit.agents import llm


def compact_context(source: llm.ChatContext) -> llm.ChatContext:
    """Keep current task and last dialogue, shorten stale page snapshots in a copy.

    The MLX gateway remains the authority for the exact 16k token budget. This
    inexpensive text bound prevents typical browser loops from filling that
    budget with repeated snapshots; the session's original history is intact.
    """
    user_indices = [index for index, item in enumerate(source.items) if item.type == "message" and item.role == "user"]
    start = user_indices[-8] if len(user_indices) >= 8 else 0
    retained = [item for index, item in enumerate(source.items) if index >= start or (item.type == "message" and item.role in {"system", "developer"})]
    items = [item.model_copy(deep=True) for item in retained]
    outputs = [item for item in items if item.type == "function_call_output"]
    prior_budget = min(650, 6000 // max(1, len(outputs) - 1))
    for index, output in enumerate(outputs):
        limit = 10000 if index == len(outputs) - 1 else prior_budget
        if len(output.output) > limit:
            output.output = output.output[:limit] + "\n[Earlier page content abbreviated; use browser tools for current page state.]"
    # Completed historical answers add context but should not dominate the next
    # request. User messages and tool call arguments are kept verbatim.
    for item in items:
        if item.type == "message" and item.role == "assistant" and len(item.text_content or "") > 1600:
            item.content = [(item.text_content or "")[:1600] + "…"]
    return llm.ChatContext(items=items)
