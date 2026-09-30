#!/usr/bin/env python3
"""Frozen local-checkpoint evaluation, not prompt tuning or an end-to-end demo.

Calls an already-resident gateway; never loads an extra MLX model, sends audio,
contacts the public Internet, or executes the agent's proposed browser action.
The installed Playwright MCP is initialized only to obtain its actual schema.
Internal reasoning and complete gateway responses are never written to reports.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import unicodedata
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

import httpx
import jsonschema

from horizon_voice.cleanup_guard import cleanup_problem
from horizon_voice.config import Settings
from k2_runtime.prompts import cleaner_messages

DEFAULT_CASES = ROOT / "tests/data/mblab_cleanup_cases.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalized_exact(text: str) -> str:
    """Formatting only: no case folding, accent/diacritic stripping or synonyms."""
    text = unicodedata.normalize("NFC", text)
    normalized = []
    for index, char in enumerate(text):
        # A numeric sign or separator is content, not harmless punctuation.
        next_numeric = index + 1 < len(text) and text[index + 1].isdigit()
        numeric_separator = index > 0 and text[index - 1].isdigit() and next_numeric
        numeric_sign = char in "-−" and next_numeric
        normalized.append(" " if unicodedata.category(char).startswith("P")
                          and not (numeric_separator or numeric_sign) else char)
    return " ".join("".join(normalized).split())


def score_cleanup(case: dict, completion: dict) -> dict:
    """Score correctness independently from the production rejection gate."""
    choice = completion.get("choices", [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content")
    finish = choice.get("finish_reason")
    nonempty = isinstance(text, str) and bool(text.strip())
    raw_correct = nonempty and any(normalized_exact(text) == normalized_exact(expected)
                                   for expected in case["acceptable"])
    lexical_problem = cleanup_problem(case["input"], text) if nonempty else None
    if finish != "stop":
        rejection = "cleaner_incomplete"
    elif not nonempty:
        rejection = "cleaner_empty"
    elif lexical_problem:
        rejection = "cleaner_uncertain"
    else:
        rejection = None
    return {
        "actual": text if isinstance(text, str) else None,
        "finish_reason": finish,
        "raw_llm_correct": bool(raw_correct),
        "gate_accepted": rejection is None,
        "gate_error_code": rejection,
        "lexical_guard_reason": lexical_problem,
        "usable_correct": bool(raw_correct and rejection is None),
        "wrong_but_gate_accepted": bool(not raw_correct and rejection is None),
    }


async def completion(http: httpx.AsyncClient, base_url: str, body: dict) -> tuple[dict, dict]:
    started = time.monotonic()
    response = await http.post(base_url + "/v1/chat/completions", json=body)
    response.raise_for_status()
    result = response.json()
    # The raw message can contain reasoning_content: retain only explicit metrics.
    measurement = {
        "wall_elapsed_ms": round((time.monotonic() - started) * 1000),
        "usage": result.get("usage"),
        "model_metrics": result.get("metrics"),
    }
    return result, measurement


async def evaluate_cleanup(http, base_url, case, effort, max_tokens=2048):
    messages = cleaner_messages(case["input"])
    row = {**case, "prompt_sha256": object_sha(messages)}
    try:
        result, measurement = await completion(http, base_url, {
            "model": "k2-cleaner", "messages": messages, "temperature": 0,
            "max_tokens": max_tokens, "reasoning_effort": effort, "stream": False,
        })
        row.update(score_cleanup(case, result), **measurement)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        row.update(raw_llm_correct=False, gate_accepted=False, usable_correct=False,
                   wrong_but_gate_accepted=False, request_error=type(exc).__name__)
        if isinstance(exc, httpx.HTTPStatusError):
            row["http_status"] = exc.response.status_code
    return row


async def installed_navigate_schema() -> tuple[dict, dict]:
    """Read the actual installed MCP tool; no browser tool is executed."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    settings = Settings()
    cli = settings.playwright_cli
    node = shutil.which(settings.node) or settings.node
    params = StdioServerParameters(command=node, args=[str(cli), "--browser", "chromium",
        "--isolated", "--image-responses", "omit", "--codegen", "none"],
        env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(settings.runtime_dir / "browsers")},
        cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            tool = next(item for item in listing.tools if item.name == "browser_navigate")
            schema = {"type": "function", "function": {"name": tool.name,
                "description": tool.description or "", "parameters": tool.inputSchema}}
    package_path = cli.parent / "package.json"
    package = json.loads(package_path.read_text())
    return schema, {"source": "actual installed Playwright MCP list_tools",
        "package": package.get("name"), "version": package.get("version"),
        "cli_sha256": sha256(cli), "schema_sha256": object_sha(schema),
        "browser_launched": False, "browser_action_executed": False}


def score_agent(result: dict, criterion: str, tool_schema: dict) -> dict:
    choice = result.get("choices", [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content")
    calls = message.get("tool_calls") or []
    # Explicitly pick content/calls instead of serializing reasoning metadata.
    row = {"answer": content, "finish_reason": choice.get("finish_reason"), "tool_calls": calls}
    if criterion == "arithmetic_no_tool":
        row["passed"] = (isinstance(content, str) and normalized_exact(content) == "25"
                         and not calls and choice.get("finish_reason") == "stop")
    else:
        valid = False
        if len(calls) == 1:
            function = calls[0].get("function") or {}
            try:
                arguments = json.loads(function.get("arguments", ""))
                jsonschema.validate(arguments, tool_schema["function"]["parameters"])
                valid = function.get("name") == "browser_navigate" and arguments == {"url": "https://example.com/"}
            except (ValueError, TypeError, jsonschema.ValidationError):
                pass
        row["passed"] = bool(valid and choice.get("finish_reason") == "tool_calls")
        row["schema_valid_expected_call"] = valid
    return row


async def evaluate_agent(http, base_url, effort, tool_schema, max_tokens=2048):
    from horizon_voice.agent import AGENT_INSTRUCTIONS
    cases = [
        ("arithmetic_no_tool", "ما ناتج 17 + 8؟ أجب بالعدد فقط."),
        ("single_browser_navigate", "افتح https://example.com/ الآن."),
    ]
    rows = []
    for criterion, user_text in cases:
        messages = [{"role": "system", "content": AGENT_INSTRUCTIONS}, {"role": "user", "content": user_text}]
        row = {"id": criterion, "input": user_text, "prompt_sha256": object_sha(messages),
               "tool_execution": "not executed; model proposal only"}
        try:
            result, measurement = await completion(http, base_url, {
                "model": "k2-agent", "messages": messages, "tools": [tool_schema],
                "tool_choice": "auto", "temperature": 0, "max_tokens": max_tokens,
                "reasoning_effort": effort, "stream": False,
            })
            row.update(score_agent(result, criterion, tool_schema), **measurement)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            row.update(passed=False, request_error=type(exc).__name__)
            if isinstance(exc, httpx.HTTPStatusError):
                row["http_status"] = exc.response.status_code
        rows.append(row)
    return rows


def summarize(rows):
    return {cohort: {
        "total": len(selected := [row for row in rows if cohort == "all" or row["cohort"] == cohort]),
        "raw_llm_correct": sum(row["raw_llm_correct"] for row in selected),
        "gate_accepted": sum(row["gate_accepted"] for row in selected),
        "usable_correct": sum(row["usable_correct"] for row in selected),
        "wrong_but_gate_accepted": sum(row["wrong_but_gate_accepted"] for row in selected),
    } for cohort in ("all", "regression", "new")}


async def main(args):
    parsed = urlparse(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.path not in {"", "/"}:
        raise ValueError("Evaluation is restricted to an HTTP gateway on loopback")
    base_url = args.base_url.rstrip("/")
    suite = json.loads(args.cases.read_text())
    cases = suite["cases"]
    if args.case_id:
        unknown = set(args.case_id) - {case["id"] for case in cases}
        if unknown:
            raise ValueError("Unknown case IDs: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["id"] in args.case_id]
    prompts_module = importlib.import_module("k2_runtime.prompts")
    guard_module = importlib.import_module("horizon_voice.cleanup_guard")
    report = {"suite_version": suite["suite_version"], "suite_sha256": sha256(args.cases),
        "started_at": datetime.now(timezone.utc).isoformat(), "base_url": base_url,
        "real_inference": True, "asr_used": False, "browser_actions_executed": False,
        "fixture_model": False, "normalization": suite["normalization"], "provenance": suite["provenance"],
        "production_prompt_sha256": sha256(Path(prompts_module.__file__)),
        "production_guard_sha256": sha256(Path(guard_module.__file__)),
        "evaluation_script_sha256": sha256(Path(__file__)),
        "inference_settings": {"cleaner_reasoning_effort": args.cleaner_effort,
            "agent_reasoning_effort": args.agent_effort, "temperature": 0, "max_tokens": args.max_tokens},
        "notes": ["Direct local gateway check, not an ASR/LiveKit/native-interface test.",
            "Raw correctness and guard acceptance are independent: a wrong output may pass the lexical guard.",
            "New cases are withheld from this prompt-development cycle, not certified absent from model training.",
            "Agent temperature is zero for reproducibility; production defaults to 0.2.",
            "A structurally correct proposed tool call does not establish that a browser action succeeded."],
        "cleaner": [], "agent": []}
    async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as http:
        health_response = await http.get(base_url + "/health")
        health_response.raise_for_status()
        health = health_response.json()
        if health.get("status") != "ready":
            raise RuntimeError("The resident gateway is not ready")
        profile = str(health.get("model_manifest", ""))
        if args.expect_profile and args.expect_profile not in Path(profile).name:
            raise RuntimeError(f"Expected a model profile containing {args.expect_profile!r}; got {profile!r}")
        report["profile"] = {"manifest_path": profile, "models": health.get("models")}
        if profile and Path(profile).is_file():
            report["profile"]["manifest_sha256"] = sha256(Path(profile))
        for case in cases:
            row = await evaluate_cleanup(http, base_url, case, args.cleaner_effort, args.max_tokens)
            report["cleaner"].append(row)
            print(json.dumps({"id": row["id"], "raw_llm_correct": row["raw_llm_correct"],
                "gate_accepted": row["gate_accepted"], "elapsed_ms": row.get("wall_elapsed_ms")}), flush=True)
        if not args.cleaner_only:
            try:
                schema, provenance = await installed_navigate_schema()
                report["tool_schema"] = {"provenance": provenance, "schema": schema}
                report["agent"] = await evaluate_agent(http, base_url, args.agent_effort, schema, args.max_tokens)
            except Exception as exc:
                # A setup failure must not discard already measured cleaner
                # results, nor count zero attempted agent cases as success.
                report["agent"] = [{"id": "agent_schema_setup", "passed": False,
                    "request_error": type(exc).__name__, "tool_execution": "not executed"}]
    report["summary"] = summarize(report["cleaner"])
    report["agent_summary"] = {"total": len(report["agent"]), "passed": sum(row["passed"] for row in report["agent"])}
    report["passed"] = all(row["usable_correct"] for row in report["cleaner"]) and all(row["passed"] for row in report["agent"])
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(args.report), "summary": report["summary"],
        "agent_summary": report["agent_summary"], "passed": report["passed"]}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    settings = Settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8788")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case-id", action="append", help="Optional subset; still records the hash of the complete frozen suite")
    parser.add_argument("--expect-profile", default="mblab", help="Required manifest filename substring; use an empty string explicitly to compare another profile")
    parser.add_argument("--cleaner-effort", choices=["low", "medium", "high"], default=settings.cleaner_reasoning_effort)
    parser.add_argument("--agent-effort", choices=["low", "medium", "high"], default=settings.agent_reasoning_effort)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--cleaner-only", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / ".runtime/validation/mblab-frozen-v1.json")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
