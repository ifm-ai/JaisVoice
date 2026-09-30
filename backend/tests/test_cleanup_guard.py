import httpx
import pytest

from horizon_voice.cleanup_guard import cleanup_problem
from horizon_voice.clients import InferenceClients, PipelineError
from horizon_voice.config import Settings
from horizon_voice.pipeline import TurnCoordinator


@pytest.mark.parametrize("source,result", [
    ("Je veux je veux aller à Lyon je rectifie à Paris demain", "Je veux aller à Paris demain."),
    ("ابحث عن أبو ظبي عفوا أقصد في Paris وأفتح الموقع الرسمي", "ابحث في Paris وأفتح الموقع الرسمي"),
    ("أرسل الملف إلى أحمد أقصد سارة غدا", "أرسل الملف إلى سارة غدا"),
    ("Send three tickets I mean four tickets to Alice I mean Bob", "Send four tickets to Bob"),
    ("أَرْسِلْ الملفَ إلى سارة", "ارسل الملف الى سارة."),
    ("L’ÉTÉ commence, à Paris !", "L'été commence à paris."),
    ("Je veux trois billets, pas quatre", "Je veux trois billets, pas quatre."),
    ("لا ترسل الرسالة الآن", "لا ترسل الرسالة الآن"),
    ("لا لا ترسل الرسالة الآن", "لا ترسل الرسالة الآن"),
    ("Je ne veux pas pas envoyer ce message", "Je ne veux pas envoyer ce message"),
    ("Don’t send this message", "Don't send this message."),
])
def test_preservation_repairs_and_harmless_formatting_pass(source, result):
    assert cleanup_problem(source, result) is None


@pytest.mark.parametrize("source,result,reason", [
    ("ابحث عن أبو ظبي عفوا أقصد في Paris وأفتح الموقع الرسمي", "ابحث عن أبو ظبي وأفتح الموقع الرسمي", "missing_final_repair"),
    ("ابحث عن الرياض أقصد جدة وافتح الموقع الرسمي", "ابحث عن جدة", "missing_final_repair"),
    ("Je vais à Lyon je rectifie Paris demain", "Je vais à Lyon demain", "missing_final_repair"),
    ("Send three tickets I mean four tickets to Bob", "Send three tickets to Bob", "missing_final_repair"),
    ("افتح موقع المتحف", "افتح موقع المتحف في لندن", "added_content"),
    ("Send three tickets", "Send four tickets", "added_content"),
    ("Open the museum", "Do not open the museum", "added_content"),
    ("لا ترسل التقرير", "ترسل التقرير", "missing_negation"),
    ("ما أبغى أرسل الرسالة", "أبغى أرسل الرسالة", "missing_negation"),
    ("لن أرسل الرسالة", "أرسل الرسالة", "missing_negation"),
    ("Do not send the report", "Do send the report", "missing_negation"),
    ("Never send the report", "Send the report", "missing_negation"),
    ("Don't send the report", "Send the report", "missing_negation"),
    ("Je ne veux pas envoyer ce rapport", "Je veux envoyer ce rapport", "missing_negation"),
    ("Ne fais jamais cela", "Fais cela", "missing_negation"),
    ("Search Rome I mean", "Search Rome", "unfinished_repair"),
])
def test_obvious_additions_or_lost_final_choices_are_rejected(source, result, reason):
    assert cleanup_problem(source, result) == reason


def test_bare_negations_do_not_create_repair_markers():
    # It must preserve bare negation, not treat it as an instruction to choose
    # either option. This does not determine which option the speaker intended.
    assert cleanup_problem("Paris non Lyon", "Paris") == "missing_negation"
    assert cleanup_problem("ثلاث تذاكر لا أربع", "ثلاث تذاكر") == "missing_negation"


async def test_uncertain_real_client_output_blocks_the_agent():
    transcript = "ابحث عن أبو ظبي عفوا أقصد في Paris وأفتح الموقع الرسمي"
    wrong = "ابحث عن أبو ظبي وأفتح الموقع الرسمي"
    async def respond(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": wrong}}]})
    events = []
    async def emit(event): events.append(event)
    async def forbidden_reply(turn, clean): pytest.fail("Uncertain cleanup must never reach the agent")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        clients = InferenceClients(Settings(), http)
        async def explicit_asr_double(audio, *, language=None): return transcript
        clients.transcribe = explicit_asr_double
        coordinator = TurnCoordinator(clients, emit, forbidden_reply)
        await (await coordinator.start("uncertain-cleanup", b"explicit-asr-double"))
    assert events[-1]["type"] == "error"
    assert events[-1]["error_code"] == "cleaner_uncertain"
    assert not any(event["type"] in {"correction", "tool_start", "complete"} for event in events)


@pytest.mark.parametrize("audio", [False, True])
async def test_cleanup_toggle_is_per_turn_and_defaults_back_to_enabled(audio):
    raw = "Can you check the result of biggest K2 Horizon model on artifical analysis ? From there write the benchmar score in a text file"
    corrected = "Can you check the result of the biggest K2 Horizon model on artificial analysis? From there, write the benchmark score in a text file."
    async def respond(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": corrected}}]})
    events, replies = [], []
    async def emit(event): events.append(event)
    async def reply(turn, clean):
        replies.append(clean)
        return "done"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        clients = InferenceClients(Settings(), http)
        async def transcribe(data, *, language=None): return raw
        clients.transcribe = transcribe
        coordinator = TurnCoordinator(clients, emit, reply)
        start = coordinator.start if audio else coordinator.start_text
        data = b"audio-double" if audio else raw
        await (await start("off", data, cleanup_guard_enabled="false"))
        assert events[-1]["type"] == "complete"
        assert replies == [corrected]
        await (await start("default", data))
        assert events[-1]["error_code"] == "cleaner_uncertain"
        await (await start("on", data, cleanup_guard_enabled="true"))
        assert events[-1]["error_code"] == "cleaner_uncertain"
        assert replies == [corrected]


@pytest.mark.parametrize("finish,content,code", [("length", "Text", "cleaner_incomplete"), ("stop", "", "cleaner_empty")])
async def test_guard_disabled_still_rejects_incomplete_or_empty_output(finish, content, code):
    async def respond(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": content}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(PipelineError) as error:
            await InferenceClients(Settings(), http).clean("Text", cleanup_guard_enabled=False)
        assert error.value.code == code


@pytest.mark.parametrize("value", ["False", "0", "", 0, 1, [], {}])
def test_invalid_guard_option_does_not_silently_disable(value):
    from horizon_voice.clients import validate_cleanup_guard
    with pytest.raises(PipelineError):
        validate_cleanup_guard(value)
