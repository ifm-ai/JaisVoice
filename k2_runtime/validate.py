"""Opt-in real model validation; writes explicit measured results to stdout."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time

from mlx_lm import stream_generate
from mlx_lm.sample_utils import make_sampler
from .model import load_model
from .protocol import normalize_messages
from .prompts import cleaner_messages
from .gateway import prepare_k2_prompt


def evaluate(path, cases, tools=None, effort="low"):
    model, tokenizer, config = load_model(path)
    provenance = path / "horizon_model.json"
    entry = json.loads(provenance.read_text()) if provenance.exists() else {}
    results = []
    for name, messages in cases:
        prompt, parser = prepare_k2_prompt(tokenizer, normalize_messages(messages), tools, effort, entry)
        started = time.perf_counter()
        last = None
        for last in stream_generate(model, tokenizer, prompt, max_tokens=1024, sampler=make_sampler(temp=0)):
            parser.feed(last.text)
        try:
            finish = parser.finish(last.finish_reason)
            error = None
        except ValueError as exc:
            finish, error = last.finish_reason, str(exc)
        result = {"case": name, "content": parser.content.strip(), "tool_calls": parser.calls,
            "finish_reason": finish, "error": error, "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "prompt_tokens": len(prompt), "completion_tokens": last.generation_tokens,
            "tokens_per_second": last.generation_tps, "peak_memory_gb": last.peak_memory}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--agent", action="store_true")
    p.add_argument("--report", type=Path)
    p.add_argument("--reasoning-effort", choices=["low", "medium", "high"], default="low")
    args = p.parse_args()
    if args.agent:
        tools = [{"type": "function", "function": {"name": "browser_navigate", "description": "Open an absolute URL in the user's browser.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}}]
        cases = [("tool-call", [{"role": "system", "content": "Use browser_navigate when asked to open a page. Reply concisely in the user's language."}, {"role": "user", "content": "Ouvre https://example.com dans le navigateur."}]),
            ("tool-result-synthetic-fixture", [{"role": "system", "content": "Reply concisely in the user's language and cite pages you read."}, {"role": "user", "content": "Ouvre https://example.com et donne le titre de la page."}, {"role": "assistant", "content": "", "tool_calls": [{"id": "call_example", "type": "function", "function": {"name": "browser_navigate", "arguments": '{"url":"https://example.com"}'}}]}, {"role": "tool", "tool_call_id": "call_example", "content": "Page URL: https://example.com\nPage title: Example Domain\nContent: This domain is for use in illustrative examples in documents."}])]
        results = evaluate(args.model, cases, tools, args.reasoning_effort)
    else:
        samples = {
            "unchanged-saudi": "ودخل الجنوبيون في انتخابات للحكم الإقليمي الذاتي",
            "destination-ar": "ابحث عن مواعيد متحف اللوفر في أبو ظبي لا أقصد متحف اللوفر في باريس",
            "name-date-ar": "أرسل التقرير يوم الأحد لا يوم الاثنين إلى خالد أقصد فاطمة",
            "negative-ar": "لا ترسل الرسالة اليوم",
            "repetition-fr": "Je veux je veux visiter le musée demain",
            "destination-fr": "Cherche les horaires du Louvre à Abou Dhabi non du Louvre à Paris",
            "already-clean": "افتح الموقع الرسمي لمتحف اللوفر في باريس",
        }
        results = evaluate(args.model, [(name, cleaner_messages(text)) for name, text in samples.items()], effort=args.reasoning_effort)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"model": str(args.model), "real_inference": True, "cases": results}, ensure_ascii=False, indent=2) + "\n")
