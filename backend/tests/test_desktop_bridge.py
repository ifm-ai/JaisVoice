"""No subprocesses: exercise preauthorized native execution through real tool wrappers."""
import asyncio
import html
import json
import shlex
import sys
import threading
import uuid
from collections import deque
from types import SimpleNamespace

import pytest
from livekit.agents import AgentSession, function_tool, llm
from livekit.agents.llm.chat_context import AgentConfigUpdate
from livekit.agents.llm.tool_context import ToolContext

from horizon_voice.agent import AGENT_INSTRUCTIONS, HorizonAgent, RoomPipeline
from horizon_voice.browser import BrowserServer
from horizon_voice.desktop import DesktopBridge, MAX_OUTPUT_BYTES, NOTE_SCRIPT, NOTE_SCRIPT_END, note_command
from horizon_voice.context import compact_context
from horizon_voice.pipeline import Turn, TurnCoordinator


def make_bridge(*, timeout=1):
    events = []
    async def emit(event): events.append(event)
    async def unused(*args): pass
    coordinator = TurnCoordinator(None, emit, unused)
    turn = Turn(str(uuid.uuid4()))
    coordinator.active = turn
    bridge = DesktopBridge(coordinator, lambda speech: turn if speech == "speech" else None, emit, request_timeout=timeout)
    ctx = SimpleNamespace(speech_handle=SimpleNamespace(id="speech", interrupted=False), function_call=SimpleNamespace(call_id="shell-1"))
    return bridge, coordinator, turn, ctx, events


def reply(turn, **updates):
    return {"type": "shell_result", "turn_id": turn.id, "tool_id": "shell-1", "approved": True, "exit_code": 0, "output": "explicit test result", **updates}


async def wait_for_request(events):
    async with asyncio.timeout(2):
        while not any(event["type"] == "shell_request" for event in events):
            await asyncio.sleep(0)


def make_packet(control, *, identity="client", topic="horizon.control"):
    return SimpleNamespace(topic=topic, participant=SimpleNamespace(identity=identity), data=json.dumps(control).encode())


def attach_pipeline(bridge, coordinator):
    pipeline = RoomPipeline.__new__(RoomPipeline)
    pipeline.client_identity = "client"
    pipeline.desktop = bridge
    pipeline.coordinator = coordinator
    pipeline.incoming_turn = None
    pipeline.cancelled_ids = deque(maxlen=256)
    pipeline.received_ids = deque(maxlen=256)
    pipeline.tasks = set()
    return pipeline


async def test_pending_registered_before_emit_and_success_has_real_result():
    bridge, coordinator, turn, ctx, events = make_bridge()
    original_emit = coordinator._emit
    async def synchronous_native_double(event):
        await original_emit(event)
        if event["type"] == "shell_request":
            assert bridge.receive_result(reply(turn))
    coordinator._emit = synchronous_native_double
    text = await bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Explicit test double only"})
    assert json.loads(text)["exit_code"] == 0
    assert [event["type"] for event in events] == ["tool_start", "shell_request", "tool_end"]
    assert "error_code" not in events[-1]
    assert turn.actions == 1
    assert bridge.pending == {}


async def test_room_authentication_wrong_ids_malformed_and_duplicate_results_ignored():
    bridge, coordinator, turn, ctx, events = make_bridge()
    pipeline = attach_pipeline(bridge, coordinator)
    task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
    await wait_for_request(events)
    pipeline.on_data(make_packet(reply(turn), identity="impostor"))
    pipeline.on_data(make_packet(reply(turn), topic="wrong-topic"))
    assert not bridge.receive_result(reply(turn, turn_id="wrong-turn"))
    assert not bridge.receive_result(reply(turn, tool_id="wrong-tool"))
    for malformed in ({"approved": "true"}, {"exit_code": True}, {"output": []}, {"error": {}}, {"timed_out": "false"}, {"exit_code": None}, {"approved": False}):
        assert not bridge.receive_result(reply(turn, **malformed))
    assert not task.done()
    pipeline.on_data(make_packet(reply(turn)))
    assert not bridge.receive_result(reply(turn))
    await task
    assert not bridge.receive_result(reply(turn))


async def test_denial_and_failed_execution_never_return_success():
    for updates, expected in [
        ({"approved": False, "exit_code": None, "error": "Native request rejected"}, "shell_denied"),
        ({"exit_code": 1, "error": "Automation denied by macOS"}, "shell_failed"),
        ({"exit_code": None, "timed_out": True, "error": "30 second limit"}, "shell_timeout"),
    ]:
        bridge, coordinator, turn, ctx, events = make_bridge()
        task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
        await wait_for_request(events)
        assert bridge.receive_result(reply(turn, **updates))
        with pytest.raises(llm.ToolError, match="blocked|Do not claim success"):
            await task
        assert events[-1]["error_code"] == expected
        assert not any(event["type"] == "shell_cancel" for event in events)


async def test_denial_blocks_another_command_even_with_a_fresh_tool_call_id():
    bridge, coordinator, turn, ctx, events = make_bridge()
    task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
    await wait_for_request(events)
    bridge.receive_result(reply(turn, approved=False, exit_code=None, error="No"))
    with pytest.raises(llm.ToolError, match="blocked"):
        await task
    ctx.function_call.call_id = "different-command-id"
    with pytest.raises(llm.ToolError, match="blocked"):
        await bridge.tools[0](ctx, {"command": "/bin/echo retry", "reason": "Different wording"})
    assert len([event for event in events if event["type"] == "shell_request"]) == 1
    assert turn.actions == 1


async def test_completed_call_identifier_cannot_be_reused_for_late_result_confusion():
    bridge, coordinator, turn, ctx, events = make_bridge()
    task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
    await wait_for_request(events)
    bridge.receive_result(reply(turn))
    await task
    with pytest.raises(llm.ToolError, match="already used"):
        await bridge.tools[0](ctx, {"command": "/bin/echo different", "reason": "Test"})
    assert not bridge.receive_result(reply(turn))
    assert turn.actions == 1


async def test_timeout_sends_native_cancel_and_cleans_pending_future():
    bridge, coordinator, turn, ctx, events = make_bridge(timeout=0.01)
    with pytest.raises(llm.ToolError, match="timed out"):
        await bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"})
    assert [event["type"] for event in events] == ["tool_start", "shell_request", "shell_cancel", "tool_end"]
    assert events[-1]["error_code"] == "shell_timeout"
    assert bridge.pending == {}
    assert not bridge.receive_result(reply(turn))


async def test_new_turn_suppresses_old_result_but_still_cancels_native_executor():
    bridge, coordinator, turn, ctx, events = make_bridge()
    task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
    await wait_for_request(events)
    turn.cancelled = True
    coordinator.active = Turn(str(uuid.uuid4()))
    assert not bridge.receive_result(reply(turn))
    await bridge.cancel_turn(turn)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [event["type"] for event in events] == ["tool_start", "shell_request", "shell_cancel"]
    assert events[-1]["turn_id"] == turn.id
    assert bridge.pending == {}


async def test_room_cancel_packet_cancels_shell_wait_and_new_text_does_not_revive_it():
    bridge, coordinator, turn, ctx, events = make_bridge()
    pipeline = attach_pipeline(bridge, coordinator)
    task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
    coordinator._task = task
    await wait_for_request(events)
    pipeline.on_data(make_packet({"type": "cancel", "turn_id": turn.id}))
    await asyncio.gather(*list(pipeline.tasks))
    assert task.cancelled()
    assert not bridge.receive_result(reply(turn))
    pipeline.on_data(make_packet({"type": "text_request", "turn_id": turn.id, "text": "Do not revive cancelled input"}))
    assert pipeline.incoming_turn is None
    assert any(event["type"] == "shell_cancel" for event in events)


async def test_cancelled_before_tool_or_waiting_on_lock_never_sends_request():
    bridge, coordinator, turn, ctx, events = make_bridge()
    async with bridge.action_lock:
        task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
        await asyncio.sleep(0)
        turn.cancelled = True
    with pytest.raises(llm.ToolError, match="cancelled"):
        await task
    assert events == []
    assert turn.actions == 0


async def test_browser_and_shell_share_serialization_and_fifty_action_budget():
    bridge, coordinator, turn, ctx, events = make_bridge()
    browser = BrowserServer.__new__(BrowserServer)
    browser.coordinator = coordinator
    browser.speech_turn = bridge.speech_turn
    browser.max_actions = 50
    browser._action_lock = bridge.action_lock
    browser._calls = {}
    executed = []
    @function_tool(raw_schema={"name": "browser_snapshot", "parameters": {"type": "object", "properties": {}}})
    async def browser_double(raw_arguments: dict):
        executed.append(True)
        return "Test snapshot"
    browser_tool = browser._wrap(browser_double)
    turn.actions = 48
    shell_task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
    await wait_for_request(events)
    browser_task = asyncio.create_task(browser_tool(ctx, {}))
    await asyncio.sleep(0)
    assert executed == []
    assert turn.actions == 49
    bridge.receive_result(reply(turn))
    await shell_task
    await browser_task
    assert executed == [True]
    assert turn.actions == 50
    with pytest.raises(llm.ToolError, match="50-action"):
        await bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"})
    with pytest.raises(llm.ToolError, match="50-action"):
        await browser_tool(ctx, {})


@pytest.mark.parametrize("arguments", [
    {"command": "", "reason": "Test"}, {"command": "true", "reason": ""},
    {"command": "true", "reason": "Test", "extra": "ignored?"},
    {"command": "x\0y", "reason": "Test"}, {"command": "ع" * 4001, "reason": "Test"},
    {"command": "true", "reason": "x" * 1001}, {"command": "\n" * 7900 + "true", "reason": "Test"},
])
async def test_invalid_or_oversize_commands_cannot_reach_native_app(arguments):
    bridge, coordinator, turn, ctx, events = make_bridge()
    with pytest.raises(llm.ToolError, match="non-empty command"):
        await bridge.tools[0](ctx, arguments)
    assert events == []
    assert turn.actions == 0


async def test_close_cancels_pending_and_bounds_unicode_output():
    bridge, coordinator, turn, ctx, events = make_bridge()
    task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
    await wait_for_request(events)
    assert bridge.receive_result(reply(turn, output="ع" * MAX_OUTPUT_BYTES))
    result = json.loads(await task)
    assert len(result["output"].encode()) <= MAX_OUTPUT_BYTES + 3
    ctx.function_call.call_id = "second"
    task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
    while not bridge.pending:
        await asyncio.sleep(0)
    await bridge.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge.pending == {}


async def test_agent_exposes_livekit_raw_shell_schema():
    bridge, coordinator, turn, ctx, events = make_bridge()
    agent = HorizonAgent(coordinator, desktop=bridge)
    assert any(tool.id == "shell_execute" for tool in agent.tools)
    schema = bridge.tools[0].info.raw_schema
    assert schema["parameters"]["required"] == ["command", "reason"]
    note_tool = next(tool for tool in agent.tools if tool.id == "macos_create_note")
    assert note_tool.info.raw_schema["parameters"]["required"] == ["title", "body"]


def test_preauthorized_mode_has_execution_deadline_and_no_horizon_consent_gate():
    async def unused(*args): pass
    coordinator = TurnCoordinator(None, unused, unused)
    bridge = DesktopBridge(coordinator, lambda _: None, unused)
    assert bridge.request_timeout == 45
    assert "preauthorized" in AGENT_INSTRUCTIONS
    assert "Do not ask\nagain for JAIS Voice permission" in AGENT_INSTRUCTIONS
    assert "macOS system permissions still apply" in AGENT_INSTRUCTIONS
    descriptions = " ".join(tool.info.raw_schema["description"] for tool in bridge.tools)
    assert "executes the exact command immediately" in descriptions
    assert "requires approval" not in descriptions
    assert "after native user approval" not in descriptions


@pytest.mark.parametrize("title,body", [
    ("Demonstration", "bonjour, je tourne en local !"),
    ("ملاحظة جديدة", "مرحبا، أنا أعمل محليًا!\nسطر ثان"),
    ("A <b>title</b> & 'quotes'", 'Literal <script>alert("bad")</script> & <img src="https://example.com/a">'),
    ("'; /usr/bin/false; #", '$(touch /tmp/SHOULD_NOT_RUN); `bad`\nHORIZON_NOTES_APPLESCRIPT\nend run\ntell application "Finder" to quit'),
    ("Lines\nAnd \'quotes", "a\r\nb\rc\nd"),
])
def test_note_text_stays_in_quoted_argv_and_never_enters_static_script(title, body):
    command = note_command(title, body)
    prefix, script = command.rsplit(" <<'" + NOTE_SCRIPT_END + "'\n", 1)
    assert script == NOTE_SCRIPT + "\n" + NOTE_SCRIPT_END
    arguments = shlex.split(prefix)
    assert arguments[:2] == ["/usr/bin/osascript", "-"]
    assert len(arguments) == 4
    assert arguments[2] == title
    expected_body = html.escape(body, quote=True).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    assert arguments[3] == "<div>" + expected_body + "</div>"
    assert "return plaintext of createdNote" in NOTE_SCRIPT
    assert "make new note" in NOTE_SCRIPT
    assert "name:noteTitle, body:noteHTML" in NOTE_SCRIPT
    assert "every note" not in NOTE_SCRIPT


def test_note_title_is_supplied_only_by_name_not_duplicated_in_body_html():
    command = note_command("Unique title marker", "Exact requested body")
    prefix, _ = command.rsplit(" <<'" + NOTE_SCRIPT_END + "'\n", 1)
    arguments = shlex.split(prefix)
    assert arguments[2] == "Unique title marker"
    assert arguments[3] == "<div>Exact requested body</div>"
    assert "Unique title marker" not in arguments[3]


@pytest.mark.parametrize("title,body", [(None, "x"), (" ", "x"), ("a" * 301, "x"), ("x\0", "y"), ("Title", None), ("Title", ""), ("Title", "\0"), ("Title", "ع" * 2001), ("Title", '"' * 2000)])
def test_invalid_or_expansion_oversized_note_is_rejected_without_truncation(title, body):
    with pytest.raises(llm.ToolError):
        note_command(title, body)


async def test_note_helper_uses_same_execution_events_budget_and_native_block_boundary():
    bridge, coordinator, turn, ctx, events = make_bridge()
    note_tool = next(tool for tool in bridge.tools if tool.id == "macos_create_note")
    task = asyncio.create_task(note_tool(ctx, {"title": "Test title", "body": "Exact literal text"}))
    await wait_for_request(events)
    assert events[-1]["command"] == note_command("Test title", "Exact literal text")
    assert turn.actions == 1
    bridge.receive_result(reply(turn, approved=False, exit_code=None, error="Native request rejected"))
    with pytest.raises(llm.ToolError, match="blocked"):
        await task
    ctx.function_call.call_id = "new-note-retry"
    with pytest.raises(llm.ToolError, match="blocked"):
        await note_tool(ctx, {"title": "Different title", "body": "Different text"})
    assert len([event for event in events if event["type"] == "shell_request"]) == 1


async def test_radio_helpers_are_fixed_commands_dispatched_to_native_execution(tmp_path):
    bridge, coordinator, turn, ctx, events = make_bridge()
    bluetooth = tmp_path / "blueutil"
    script = tmp_path / "script dir" / "macos_radios.py"
    script.parent.mkdir()
    bluetooth.write_text("not executed")
    script.write_text("not executed")
    bridge = DesktopBridge(coordinator, bridge.speech_turn, coordinator._emit, bluetooth_tool=bluetooth, radio_script=script)
    functions = {tool.id: tool for tool in bridge.tools}
    assert {"macos_radio_status", "macos_set_airplane_mode"} <= functions.keys()
    for index, (name, arguments, expected_suffix) in enumerate([
        ("macos_radio_status", {}, ["status"]),
        ("macos_set_airplane_mode", {"enabled": True}, ["airplane", "on", "--confirmed"]),
        ("macos_set_airplane_mode", {"enabled": False}, ["airplane", "off", "--confirmed"]),
    ]):
        ctx.function_call.call_id = "radio-" + str(index)
        before = len(events)
        task = asyncio.create_task(functions[name](ctx, arguments))
        while len(events) < before + 2:
            await asyncio.sleep(0)
        request = events[-1]
        assert request["type"] == "shell_request"
        assert shlex.split(request["command"]) == [sys.executable, str(script), *expected_suffix]
        if arguments.get("enabled"):
            assert "Wi-Fi ET Bluetooth" in request["reason"]
            assert "clavier/souris" in request["reason"]
            assert "transcription" in request["reason"]
        bridge.receive_result(reply(turn, tool_id=ctx.function_call.call_id))
        await task
    assert turn.actions == 3
    for invalid in ({"enabled": "true"}, {"enabled": 1}, {"enabled": True, "extra": "injected"}, {}):
        with pytest.raises(llm.ToolError, match="boolean"):
            await functions["macos_set_airplane_mode"](ctx, invalid)
    with pytest.raises(llm.ToolError, match="no arguments"):
        await functions["macos_radio_status"](ctx, {"extra": "injected"})


def test_radio_helpers_not_advertised_without_both_local_dependencies(tmp_path):
    async def unused(*args): pass
    coordinator = TurnCoordinator(None, unused, unused)
    bluetooth = tmp_path / "blueutil"
    script = tmp_path / "macos_radios.py"
    script.write_text("not executed")
    bridge = DesktopBridge(coordinator, lambda _: None, unused, bluetooth_tool=bluetooth, radio_script=script)
    assert all(not tool.id.startswith("macos_radio") and tool.id != "macos_set_airplane_mode" for tool in bridge.tools)


def test_livekit_context_copy_and_openai_payload_do_not_deepcopy_bridge_resources():
    async def unused(*args): pass
    coordinator = TurnCoordinator(None, unused, unused)
    bridge = DesktopBridge(coordinator, lambda _: None, unused)
    # Real rooms own an httpx connection pool with non-pickleable locks. A
    # concrete RLock reproduces the same failure without opening a connection.
    bridge.explicit_noncopyable_test_resource = threading.RLock()
    configuration = AgentConfigUpdate(instructions="Test", tools_added=["shell_execute"])
    configuration._tools = bridge.tools
    context = llm.ChatContext(items=[configuration])
    copied = compact_context(context)
    assert copied.items[0] is not configuration
    payload = ToolContext(copied.items[0]._tools).parse_function_tools("openai", strict=False)
    assert json.loads(json.dumps(payload))[0]["function"]["name"] == "shell_execute"


async def test_real_agent_session_with_shell_tool_reaches_provider_without_copying_locks():
    class ExplicitPayloadLLM(llm.LLM):
        def __init__(self):
            super().__init__()
            self.payloads = []

        def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
            # Exercise the actual SDK's provider schema conversion as well as
            # HorizonAgent.llm_node's context copy before any network inference.
            self.payloads.append(ToolContext(tools or []).parse_function_tools("openai", strict=False))
            return ExplicitPayloadStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)

    class ExplicitPayloadStream(llm.LLMStream):
        async def _run(self):
            self._event_ch.send_nowait(llm.ChatChunk(id="test-only-payload", delta=llm.ChoiceDelta(content="Test complete.")))

    class ExplicitClients:
        async def clean(self, text): return text

    events = []
    async def emit(event): events.append(event)
    pipeline = RoomPipeline.__new__(RoomPipeline)
    model = ExplicitPayloadLLM()
    pipeline.session = AgentSession(llm=model, user_away_timeout=None)
    pipeline.session.output.set_audio_enabled(False)
    pipeline.speech_turns = {}
    pipeline.coordinator = TurnCoordinator(ExplicitClients(), emit, pipeline.reply)
    pipeline.desktop = DesktopBridge(pipeline.coordinator, pipeline.speech_turns.get, emit)
    pipeline.desktop.explicit_noncopyable_test_resource = threading.RLock()
    await pipeline.session.start(agent=HorizonAgent(pipeline.coordinator, desktop=pipeline.desktop))
    try:
        task = await pipeline.coordinator.start_text("actual-shell-payload-test", "Explicit test input")
        await asyncio.wait_for(task, 5)
        assert events[-1]["type"] == "complete", events
        assert events[-1]["text"] == "Test complete."
        assert model.payloads[0][0]["function"]["name"] == "shell_execute"
        json.dumps(model.payloads)
    finally:
        await pipeline.desktop.aclose()
        await pipeline.session.aclose()


def test_platform_instructions_only_offer_a_verified_existing_bluetooth_executable(tmp_path):
    async def unused(*args): pass
    coordinator = TurnCoordinator(None, unused, unused)
    path = tmp_path / "macos tools" / "blueutil"
    absent = DesktopBridge(coordinator, lambda _: None, unused, bluetooth_tool=path)
    assert "No Bluetooth command-line" in absent.instructions
    path.parent.mkdir()
    path.write_text("explicit test fixture, never executed")
    available = DesktopBridge(coordinator, lambda _: None, unused, bluetooth_tool=path)
    assert "'" + str(path) + "' --power 0" in available.instructions
    assert "private API" in available.instructions
    assert "read power back" in available.instructions


async def test_text_control_auth_fresh_ids_and_real_start_text_contract():
    bridge, coordinator, turn, ctx, events = make_bridge()
    pipeline = attach_pipeline(bridge, coordinator)
    pipeline.ready = asyncio.Event()
    pipeline.ready.set()
    pipeline.startup_error = None
    calls = []
    async def cancel(): calls.append("cancel")
    async def start_text(turn_id, text): calls.append((turn_id, text))
    pipeline.coordinator = SimpleNamespace(cancel=cancel, start_text=start_text)
    pipeline.emit = coordinator._emit
    turn_id = str(uuid.uuid4())
    control = {"type": "text_request", "turn_id": turn_id, "text": "افتح ملاحظة جديدة"}
    pipeline.on_data(make_packet(control, identity="wrong-client"))
    for invalid in ({"turn_id": "not-a-uuid"}, {"text": " "}, {"text": "ع" * 2001}, {"text": 1}):
        pipeline.on_data(make_packet({**control, **invalid}))
    assert calls == []
    pipeline.on_data(make_packet(control))
    pipeline.on_data(make_packet(control))
    await asyncio.gather(*list(pipeline.tasks))
    assert calls == ["cancel", (turn_id, control["text"])]
    assert len(pipeline.received_ids) == 1


async def test_new_text_turn_invalidates_pending_shell_before_starting_new_request():
    bridge, coordinator, turn, ctx, events = make_bridge()
    pipeline = attach_pipeline(bridge, coordinator)
    pipeline.ready = asyncio.Event()
    pipeline.ready.set()
    pipeline.startup_error = None
    pipeline.emit = coordinator._emit
    started = []
    async def start_text(turn_id, text):
        started.append((turn_id, text))
        coordinator.active = Turn(turn_id)
    coordinator.start_text = start_text
    task = asyncio.create_task(bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Test"}))
    coordinator._task = task
    await wait_for_request(events)
    new_id = str(uuid.uuid4())
    pipeline.on_data(make_packet({"type": "text_request", "turn_id": new_id, "text": "A new explicit test turn"}))
    await asyncio.gather(*list(pipeline.tasks))
    assert task.cancelled()
    assert started == [(new_id, "A new explicit test turn")]
    assert not bridge.receive_result(reply(turn))
    assert any(event["type"] == "shell_cancel" and event["turn_id"] == turn.id for event in events)


async def test_clarification_stops_generation_and_blocks_further_actions():
    bridge, coordinator, turn, ctx, events = make_bridge()
    tool = next(tool for tool in bridge.tools if tool.id == "ask_clarifying_question")
    with pytest.raises(llm.StopResponse):
        await tool(ctx, {"question": "  Which city?  "})
    assert turn.clarification_question == "Which city?"
    assert turn.actions == 0 and not bridge.pending and not events
    with pytest.raises(llm.StopResponse):
        await bridge.tools[0](ctx, {"command": "/usr/bin/true", "reason": "Must wait"})
    assert not events


@pytest.mark.parametrize("arguments", [{}, {"question": " "}, {"question": "ع" * 1001}, {"question": "x\0y"}, {"question": "City?", "extra": True}])
async def test_invalid_clarification_does_not_pause(arguments):
    bridge, coordinator, turn, ctx, events = make_bridge()
    tool = next(tool for tool in bridge.tools if tool.id == "ask_clarifying_question")
    with pytest.raises(llm.ToolError):
        await tool(ctx, arguments)
    assert turn.clarification_question is None


async def test_stale_clarification_is_rejected():
    bridge, coordinator, turn, ctx, events = make_bridge()
    turn.cancelled = True
    tool = next(tool for tool in bridge.tools if tool.id == "ask_clarifying_question")
    with pytest.raises(llm.ToolError):
        await tool(ctx, {"question": "Which city?"})
    assert turn.clarification_question is None and not events
