from fastapi.testclient import TestClient
import pytest

from k2_runtime import gateway
from k2_runtime.cleanup_guard import cleanup_problem
from k2_runtime.prompts import cleaner_messages
from model_fixtures import loaded


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


def done(content, finish="stop"):
    return {"finish_reason": finish, "content": content, "reasoning_content": "", "tool_calls": [],
            "usage": {}, "metrics": {}}


@pytest.fixture
def gateway_client(monkeypatch):
    """The real /v1/clean endpoint with an explicitly substituted generation worker."""
    worker = gateway.Worker()
    worker.status = "ready"
    worker.models = loaded(["4B"])
    worker.outputs, worker.requests = [], []
    def fake_run(body, cancelled, emit):
        worker.requests.append(body)
        emit(("ready", {"prompt_tokens": 1}))
        emit(("done", worker.outputs.pop(0)) if worker.outputs else ("error", "no fixture output"))
        emit(("end", None))
    monkeypatch.setattr(worker, "load", lambda: None)
    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(gateway, "worker", worker)
    with TestClient(gateway.app) as client:
        yield client, worker


def test_uncertain_correction_is_refused_and_never_returned(gateway_client):
    http, worker = gateway_client
    transcript = "ابحث عن أبو ظبي عفوا أقصد في Paris وأفتح الموقع الرسمي"
    worker.outputs.append(done("ابحث عن أبو ظبي وأفتح الموقع الرسمي"))
    response = http.post("/v1/clean", json={"text": transcript})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "cleaner_uncertain"
    assert "text" not in response.json()
    assert worker.requests[0].model == "k2-cleaner" and worker.requests[0].tools is None
    assert worker.requests[0].messages == cleaner_messages(transcript)


def test_cleanup_guard_is_per_request_and_enabled_by_default(gateway_client):
    http, worker = gateway_client
    raw = "Can you check the result of biggest K2 Horizon model on artifical analysis ? From there write the benchmar score in a text file"
    corrected = "Can you check the result of the biggest K2 Horizon model on artificial analysis? From there, write the benchmark score in a text file."
    worker.outputs.extend([done(corrected)] * 3)
    off = http.post("/v1/clean", json={"text": raw, "guard": False})
    assert off.status_code == 200 and off.json()["text"] == corrected
    assert http.post("/v1/clean", json={"text": raw}).json()["detail"]["code"] == "cleaner_uncertain"
    assert http.post("/v1/clean", json={"text": raw, "guard": True}).json()["detail"]["code"] == "cleaner_uncertain"


@pytest.mark.parametrize("finish,content,code", [("length", "Text", "cleaner_incomplete"), ("stop", "  ", "cleaner_empty")])
def test_guard_disabled_still_rejects_incomplete_or_empty_output(gateway_client, finish, content, code):
    http, worker = gateway_client
    worker.outputs.append(done(content, finish))
    response = http.post("/v1/clean", json={"text": "Text", "guard": False})
    assert response.status_code == 422 and response.json()["detail"]["code"] == code


@pytest.mark.parametrize("value", ["False", "0", "", 0, 1, [], {}])
def test_invalid_guard_option_does_not_silently_disable(gateway_client, value):
    http, worker = gateway_client
    assert http.post("/v1/clean", json={"text": "Text", "guard": value}).status_code == 422
    assert worker.requests == []
