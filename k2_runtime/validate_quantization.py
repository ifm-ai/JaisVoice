"""Compare real converted model logits against its original floating-point weights.

The source is loaded with physically stored dtypes, not its config's dtype label.
Only our inspected MLX implementation is used; no downloaded Python is executed.
"""
import argparse
import gc
import json
from pathlib import Path
import time

import mlx.core as mx
import numpy as np
from .model import load_model
from .gateway import prepare_k2_prompt

PROMPTS = [
    [{"role": "user", "content": "Bonjour, réponds en une phrase."}],
    [{"role": "user", "content": "ما هي عاصمة فرنسا؟"}],
    [{"role": "user", "content": "Open https://example.com in the browser."}],
]


def logits(path):
    model, tokenizer, _ = load_model(path)
    results = []
    for messages in PROMPTS:
        tokens, _ = prepare_k2_prompt(tokenizer, messages, None, "low", {})
        values = model(mx.array([tokens]))[0, -1, :].astype(mx.float32)
        results.append(np.array(values))
    del model, tokenizer
    gc.collect()
    mx.clear_cache()
    return results


def compare(source, quantized):
    started = time.perf_counter()
    reference = logits(source)
    actual = logits(quantized)
    cases = []
    for i, (before, after) in enumerate(zip(reference, actual)):
        # Centering eliminates a harmless uniform logit shift.
        before -= before.mean()
        after -= after.mean()
        rmse = float(np.sqrt(np.mean((before - after) ** 2)))
        relative = rmse / float(np.sqrt(np.mean(before ** 2)))
        log_softmax = lambda x: x - x.max() - np.log(np.exp(x - x.max()).sum())
        reference_log_p, quantized_log_p = log_softmax(before), log_softmax(after)
        kl = float(np.sum(np.exp(reference_log_p) * (reference_log_p - quantized_log_p)))
        overlap = len(set(np.argsort(before)[-20:]) & set(np.argsort(after)[-20:])) / 20
        cases.append({"case": i, "reference_top1": int(before.argmax()), "quantized_top1": int(after.argmax()),
                      "top1_matches": bool(before.argmax() == after.argmax()), "centered_logit_nrmse": relative,
                      "kl_reference_to_quantized_nats": kl, "top20_overlap": overlap})
    target_config = json.loads((quantized / "config.json").read_text())
    result = {"test": "source-floating-vs-converted-logits", "source": str(source), "converted": str(quantized),
              "converted_precision": target_config.get("quantization", target_config.get("dtype")), "cases": cases,
              "elapsed_s": round(time.perf_counter() - started, 2), "peak_memory_gb": mx.get_peak_memory() / 1e9,
              "acceptance": "top-1 agreement and KL(reference || quantized) < 0.05 nats on each prompt",
              "passed": all(case["top1_matches"] and case["kl_reference_to_quantized_nats"] < .05 for case in cases)}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--quantized", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = compare(args.source, args.quantized)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if not result["passed"]:
        raise SystemExit("Quantization agreement threshold failed")
