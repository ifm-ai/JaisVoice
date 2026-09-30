"""Real resident gateway checks, including untouched held-out cleanup cases."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import httpx
from .prompts import cleaner_messages

DEVELOPMENT_CASES = [
    ("museum-city", "ابحث عن ساعات عمل متحف المستقبل في أبو ظبي لا في دبي", "ابحث عن ساعات عمل متحف المستقبل في دبي"),
    ("negation-and-time", "لا ترسل الرسالة اليوم أرسلها غداً لا بعد غد", "لا ترسل الرسالة اليوم أرسلها بعد غد"),
    ("number-and-name", "احجز ثلاث تذاكر لا أربع تذاكر باسم يوسف أقصد مريم", "احجز أربع تذاكر باسم مريم"),
    ("french-number-name", "Réserve deux places non cinq places pour Chloé enfin pour Lucas", "Réserve cinq places pour Lucas"),
]
FRESH_HOLDOUTS = [
    ("later-day-with-negation", "لا تحجز اليوم احجز يوم الخميس لا يوم السبت", "لا تحجز اليوم احجز يوم السبت"),
    ("arabic-quantity-recipient", "اطلب خمس نسخ لا ثماني نسخ باسم سامي أقصد ليلى", "اطلب ثماني نسخ باسم ليلى"),
    ("french-quantity-recipient", "Réserve trois chambres non sept chambres pour Julien enfin pour Amélie", "Réserve sept chambres pour Amélie"),
    ("french-new-museum", "Lance une recherche sur le musée d'Orsay à Lyon non à Paris", "Lance une recherche sur le musée d'Orsay à Paris"),
    ("arabic-flights", "ابحث عن أسعار رحلات الطيران إلى الدوحة لا إلى مسقط", "ابحث عن أسعار رحلات الطيران إلى مسقط"),
]
MAIN_DEMO = [('main-demo-louvre-ar',
    'ابحث عن مواعيد متحف اللوفر في أبو ظبي لا أقصد متحف اللوفر في باريس وافتح الموقع الرسمي ولخص المواعيد',
    'ابحث عن مواعيد متحف اللوفر في باريس وافتح الموقع الرسمي ولخص المواعيد')]
EXPLICIT_CUE_CASES = [
    ('main-demo-explicit-repair',
     'ابحث عن مواعيد متحف اللوفر في أبو ظبي عفوا أقصد متحف اللوفر في باريس وافتح الموقع الرسمي ولخص المواعيد',
     'ابحث عن مواعيد متحف اللوفر في باريس وافتح الموقع الرسمي ولخص المواعيد'),
    ('new-clinic-day-tail', 'سجل الموعد في العيادة يوم الاثنين عفوا أقصد يوم الجمعة وأرسل تذكيرا',
     'سجل الموعد في العيادة يوم الجمعة وأرسل تذكيرا'),
    ('new-french-recipient-tail', 'Envoie le dossier à Nora je rectifie à Elsa puis archive une copie',
     'Envoie le dossier à Elsa puis archive une copie'),
    ('negative-quantity', 'أريد ثلاث تذاكر وليس أربع', 'أريد ثلاث تذاكر وليس أربع'),
    ('negative-recipient', 'لا ترسل الرسالة إلى أحمد', 'لا ترسل الرسالة إلى أحمد'),
    ('explicit-quantity', 'اطلب خمس نسخ بل ثماني نسخ باسم سامي عفوا أقصد ليلى', 'اطلب ثماني نسخ باسم ليلى'),
    ('explicit-negative-and-day', 'لا تحجز اليوم احجز يوم الخميس عفوا أقصد يوم السبت', 'لا تحجز اليوم احجز يوم السبت'),
]


def run(base_url, cohort):
    results = []
    samples = FRESH_HOLDOUTS if cohort == 'fresh-v2' else DEVELOPMENT_CASES
    if cohort == 'development-all':
        samples = DEVELOPMENT_CASES + FRESH_HOLDOUTS + MAIN_DEMO
    if cohort == 'explicit-cues-v3':
        samples = EXPLICIT_CUE_CASES
    category = 'unseen-holdout-no-prompt-retuning' if cohort == 'fresh-v2' else 'development-former-holdout'
    if cohort == 'explicit-cues-v3':
        category = 'explicit-cue-evaluation-after-development'
    with httpx.Client(base_url=base_url, timeout=180) as client:
        health = client.get('/health').raise_for_status().json()
        if health['status'] != 'ready':
            raise RuntimeError(health)
        for name, text, expected in samples:
            started = time.perf_counter()
            response = client.post('/v1/chat/completions', json={'model': 'k2-cleaner',
                'messages': cleaner_messages(text), 'temperature': 0, 'max_tokens': 2048})
            response.raise_for_status()
            data = response.json()
            actual = data['choices'][0]['message']['content'].strip()
            result = {'case': name, 'category': category, 'input': text,
                      'expected': expected, 'actual': actual, 'exact_match': actual == expected,
                      'wall_ms': round((time.perf_counter() - started) * 1000), 'metrics': data['metrics']}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        response = client.post('/v1/chat/completions', json={'model': 'k2-agent', 'messages': [
            {'role': 'system', 'content': 'Reply concisely in French.'}, {'role': 'user', 'content': 'Dis bonjour en une phrase.'}], 'max_tokens': 1024})
        response.raise_for_status()
        data = response.json()
        results.append({'case': 'both-resident-agent-response', 'actual': data['choices'][0]['message']['content'].strip(), 'metrics': data['metrics']})
    return {'real_inference': True, 'both_models_resident': True, 'health': health, 'cohort': cohort,
            'prompt_sha256': hashlib.sha256(Path(__file__).with_name('prompts.py').read_bytes()).hexdigest(),
            'heldout_exact_matches': sum(item.get('exact_match', False) for item in results), 'heldout_count': len(samples), 'cases': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8788')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--cohort', choices=['fresh-v2', 'development-v1', 'development-all', 'explicit-cues-v3'], default='fresh-v2')
    args = parser.parse_args()
    report = run(args.base_url, args.cohort)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'heldout_exact_matches': report['heldout_exact_matches'], 'heldout_count': report['heldout_count']}))
