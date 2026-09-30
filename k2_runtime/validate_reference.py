"""Run the exact upstream IFM RMSNorm class against our MLX implementation.

PyTorch is validation-only and loaded from a separate install target; it is not
a production dependency. Only the inspected class is compiled from the pinned
reference file, avoiding execution of the rest of the downloaded model module.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys


def validate(source, dependencies):
    import mlx.core as mx
    import numpy as np
    from .model import GroupRMSNorm
    sys.path.insert(0, str(dependencies))
    import torch

    source_text = source.read_text()
    tree = ast.parse(source_text)
    node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'K2HorizonRMSNorm')
    namespace = {'torch': torch, 'nn': torch.nn}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    Reference = namespace['K2HorizonRMSNorm']
    results = []
    rng = np.random.default_rng(320)
    for groups, dimensions in [(1, 4096), (2, 2560), (4, 4096)]:
        for dtype in ['float32', 'bfloat16']:
            source_values = rng.normal(size=(2, 9, dimensions)).astype(np.float32)
            source_values *= np.repeat(np.array([.1, 1, 10, 100], dtype=np.float32), dimensions // 4)
            weights = rng.uniform(.5, 1.5, dimensions).astype(np.float32)
            tdtype = getattr(torch, dtype)
            mdtype = getattr(mx, dtype)
            reference = Reference(dimensions, groups, eps=1e-6).to(tdtype)
            with torch.no_grad():
                reference.weight.copy_(torch.from_numpy(weights).to(tdtype))
                expected = reference(torch.from_numpy(source_values).to(tdtype)).float().numpy()
            local = GroupRMSNorm(dimensions, groups, 1e-6)
            local.weight = mx.array(weights).astype(mdtype)
            actual = np.array(local(mx.array(source_values).astype(mdtype)).astype(mx.float32))
            error = float(np.max(np.abs(actual - expected)))
            # BF16 rounding has a coarser ULP; inputs and outputs remain BF16.
            tolerance = 2e-6 if dtype == 'float32' else .03125
            result = {'groups': groups, 'dimensions': dimensions, 'dtype': dtype, 'max_absolute_error': error, 'passed': error <= tolerance}
            results.append(result)
            print(json.dumps(result), flush=True)
    return {'reference': str(source), 'reference_sha256': hashlib.sha256(source_text.encode()).hexdigest(),
            'exact_upstream_class': 'K2HorizonRMSNorm', 'torch_version': torch.__version__, 'cases': results,
            'passed': all(item['passed'] for item in results)}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--dependencies', type=Path, default=Path('.runtime/validation-deps'))
    p.add_argument('--report', type=Path, required=True)
    args = p.parse_args()
    result = validate(args.source, args.dependencies)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    if not result['passed']:
        raise SystemExit('Reference primitive parity failed')
