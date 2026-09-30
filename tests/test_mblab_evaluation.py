"""Evaluation plumbing with explicit HTTP doubles; no inference claims."""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("evaluate_mblab", ROOT / "scripts/evaluate_mblab.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)

FROZEN_SHA256 = "b4c61a0ac90b3f55dbbb59204961df748ce22343abe36ac290a761b407fd32e6"
FIXTURE_SCHEMA = {"type": "function", "function": {"name": "browser_navigate", "parameters": {
    "type": "object", "properties": {"url": {"type": "string"}},
    "required": ["url"], "additionalProperties": False}}}


def fake_completion(content, finish="stop", calls=None):
    return {"choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": content, "tool_calls": calls or [],
        "reasoning_content": "PRIVATE_REASONING_MUST_NOT_APPEAR"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        "metrics": {"elapsed_ms": 12, "generation_tps": 80.0, "peak_memory_gb": 8.0}}


def test_cases_are_frozen_and_new_inputs_not_in_production_examples():
    from k2_runtime.prompts import EXAMPLES
    assert evaluation.sha256(evaluation.DEFAULT_CASES) == FROZEN_SHA256
    cases = json.loads(evaluation.DEFAULT_CASES.read_text())["cases"]
    assert len(cases) == len({case["id"] for case in cases}) == 16
    assert sum(case["cohort"] == "regression" for case in cases) == 3
    assert sum(case["cohort"] == "new" for case in cases) == 13
    development_inputs = {source for source, expected in EXAMPLES}
    for case in cases:
        if case["cohort"] == "new":
            assert case["input"] not in development_inputs
        for expected in case["acceptable"]:
            assert evaluation.cleanup_problem(case["input"], expected) is None


def test_normalization_is_not_semantically_permissive():
    normalize = evaluation.normalized_exact
    assert normalize(" Paris,   puis Lyon. ") == normalize("Paris puis Lyon")
    assert normalize("المستقبل، في دبي.") == normalize("المستقبل في دبي")
    assert normalize("Paris") != normalize("باريس")
    assert normalize("Paris") != normalize("paris")
    assert normalize("أبغى") != normalize("ابغى")
    assert normalize("réserve") != normalize("reserve")
    assert normalize("25") != normalize("٢٥")
    assert normalize("12") != normalize("15")
    assert normalize("-25") != normalize("25")
    assert normalize("1.5") != normalize("1 5")


def test_guard_acceptance_is_independent_of_exact_correctness():
    case = {"input": "Cherche Lyon je rectifie Paris", "acceptable": ["Cherche Paris"]}
    # A no-op incorrectly retains the repair, but the limited lexical guard
    # cannot reject it. This false acceptance must remain visible in the report.
    scored = evaluation.score_cleanup(case, fake_completion(case["input"]))
    assert scored["raw_llm_correct"] is False
    assert scored["gate_accepted"] is True
    assert scored["wrong_but_gate_accepted"] is True
    assert scored["usable_correct"] is False
    correct = evaluation.score_cleanup(case, fake_completion("Cherche Paris"))
    assert correct["raw_llm_correct"] is correct["gate_accepted"] is correct["usable_correct"] is True
    truncated = evaluation.score_cleanup(case, fake_completion("Cherche Paris", "length"))
    assert truncated["raw_llm_correct"] is True
    assert truncated["gate_accepted"] is False
    assert truncated["gate_error_code"] == "cleaner_incomplete"


def test_cleanup_http_uses_production_prompt_without_exposing_reasoning():
    case = {"id": "fixture", "cohort": "new", "input": "Ne paie rien", "acceptable": ["Ne paie rien"]}
    received = []
    def handle(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json=fake_completion("Ne paie rien"))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            return await evaluation.evaluate_cleanup(http, "http://127.0.0.1:9876", case, "low")
    row = asyncio.run(run())
    assert received[0]["messages"] == evaluation.cleaner_messages(case["input"])
    assert received[0]["model"] == "k2-cleaner"
    assert row["usable_correct"] is True
    assert row["model_metrics"]["elapsed_ms"] == 12
    assert "PRIVATE_REASONING_MUST_NOT_APPEAR" not in json.dumps(row)
    assert "reasoning_content" not in json.dumps(row)


def test_http_failure_report_does_not_dump_private_response_body():
    case = {"id": "fixture", "cohort": "new", "input": "Bonjour", "acceptable": ["Bonjour"]}
    def handle(request):
        return httpx.Response(422, json={"detail": "PRIVATE_REASONING_MUST_NOT_APPEAR"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            return await evaluation.evaluate_cleanup(http, "http://127.0.0.1:9876", case, "low")
    row = asyncio.run(run())
    assert row["http_status"] == 422
    assert row["gate_accepted"] is False
    assert row["raw_llm_correct"] is False
    assert "PRIVATE_REASONING_MUST_NOT_APPEAR" not in json.dumps(row)


@pytest.mark.parametrize("name,args,finish,expected", [
    ("browser_navigate", {"url": "https://example.com/"}, "tool_calls", True),
    ("browser_navigate", {"url": "https://wrong.example/"}, "tool_calls", False),
    ("browser_navigate", {"url": "https://example.com/", "invented": True}, "tool_calls", False),
    ("browser_navigate", {}, "tool_calls", False),
    ("invented_tool", {"url": "https://example.com/"}, "tool_calls", False),
    ("browser_navigate", {"url": "https://example.com/"}, "stop", False),
])
def test_navigate_proposal_uses_exact_schema_and_target(name, args, finish, expected):
    calls = [{"id": "fixture-call", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
    score = evaluation.score_agent(fake_completion("", finish, calls), "single_browser_navigate", FIXTURE_SCHEMA)
    assert score["passed"] is expected
    assert "PRIVATE_REASONING_MUST_NOT_APPEAR" not in json.dumps(score)


def test_agent_plain_answer_must_not_use_tools():
    result = fake_completion("25.")
    assert evaluation.score_agent(result, "arithmetic_no_tool", FIXTURE_SCHEMA)["passed"]
    assert not evaluation.score_agent(fake_completion("26"), "arithmetic_no_tool", FIXTURE_SCHEMA)["passed"]


def test_summary_separates_regression_and_new():
    rows = [
        {"cohort": "regression", "raw_llm_correct": True, "gate_accepted": True, "usable_correct": True, "wrong_but_gate_accepted": False},
        {"cohort": "new", "raw_llm_correct": False, "gate_accepted": True, "usable_correct": False, "wrong_but_gate_accepted": True},
    ]
    summary = evaluation.summarize(rows)
    assert summary["all"]["total"] == 2
    assert summary["all"]["usable_correct"] == 1
    assert summary["regression"]["usable_correct"] == 1
    assert summary["new"]["usable_correct"] == 0
    assert summary["new"]["wrong_but_gate_accepted"] == 1
