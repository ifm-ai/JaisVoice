"""Bounded medium/high development runs with actual captured tool schemas."""
import argparse
import copy
import json
from pathlib import Path
import time
import httpx
from .prompts import cleaner_messages


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--effort', choices=['low', 'medium', 'high'], required=True)
    parser.add_argument('--case', choices=['cleaner', 'statement', 'web'], required=True)
    parser.add_argument('--temperature', type=float, default=0)
    args = parser.parse_args()
    if args.case == 'cleaner':
        body = {'model': 'k2-cleaner', 'messages': cleaner_messages('ابحث عن متحف اللوفر في أبو ظبي عفوا أقصد في Paris وأفتح الموقع الرسمي')}
    else:
        filename = 'real-agent-request-acknowledgement-01.json' if args.case == 'statement' else 'real-agent-request-web-01.json'
        body = json.loads((Path('.runtime/validation') / filename).read_text())
        body['messages'] = body['messages'][:2]
    body.update(stream=False, reasoning_effort=args.effort, temperature=args.temperature, max_completion_tokens=2048, debug_trace=True)
    body.pop('max_tokens', None)
    started = time.perf_counter()
    response = httpx.post('http://127.0.0.1:8788/v1/chat/completions', json=body, timeout=180)
    result = {'case': args.case, 'reasoning_effort': args.effort, 'temperature': args.temperature,
              'max_completion_tokens': 2048, 'http_status': response.status_code,
              'wall_ms': round((time.perf_counter() - started) * 1000)}
    data = response.json()
    if response.is_success:
        message = data['choices'][0]['message']
        result.update(content=message.get('content', '').strip(), tool_calls=message.get('tool_calls', []),
                      metrics=data.get('metrics'), usage=data.get('usage'))
    else:
        result['error'] = data
    directory = Path('.runtime/validation')
    (directory / f'k2-{args.case}-{args.effort}-temp{args.temperature}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)
