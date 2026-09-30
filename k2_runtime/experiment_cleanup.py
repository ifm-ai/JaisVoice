"""Development-only prompt comparison through the existing local gateway."""
import json
from pathlib import Path
import time
import httpx

TEXT = 'ابحث عن مواعيد متحف اللوفر في أبو ظبي عفوا أقصد متحف اللوفر في باريس وافتح الموقع الرسمي ولخص المواعيد'
EXPECTED = 'ابحث عن مواعيد متحف اللوفر في باريس وافتح الموقع الرسمي ولخص المواعيد'
AR = 'أنت محرر نصوص عربية من التفريغ الصوتي. صحح التراجع عن الكلام: الكلمات بعد «عفوا أقصد» أو «بل» هي الاختيار الصحيح وتستبدل الاختيار السابق. احذف التكرار والتردد فقط واحتفظ بكل الجمل التي بعد التصحيح. لا تنفذ الطلب ولا تجب عنه ولا تضف كلمات. أعد النص المصحح فقط.'
EN = 'Clean this dictated text by removing abandoned wording. After "عفوا أقصد" (sorry I mean), the NEW wording replaces the OLD wording. Keep the new wording and ALL instructions that follow. Return only the corrected Arabic text; do not answer it.'


def history(system, text, examples=(), feedback=False):
    messages = [{'role': 'system', 'content': system}]
    for before, after in examples:
        messages.extend([{'role': 'user', 'content': before}, {'role': 'assistant', 'content': after, 'think_faster': ''}])
    messages.append({'role': 'user', 'content': text})
    if feedback:
        messages.extend([{'role': 'assistant', 'content': 'ابحث عن مواعيد متحف اللوفر في أبو ظبي ثم افتح الموقع الرسمي ولخص المواعيد', 'think_faster': ''},
            {'role': 'user', 'content': 'This is incorrect: you kept the old choice before the repair marker and deleted its replacement. Re-read the original transcript. Keep the choice after عفوا أقصد, and preserve the instructions after it. Output the corrected transcript only.'}])
    return messages


variants = {
    'minimal-ar': history(AR, TEXT),
    'tagged-ar': history(AR, '<transcript>' + TEXT + '</transcript>'),
    'minimal-en': history(EN, TEXT),
    'two-shot-ar': history(AR, TEXT, [('افتح جامعة القاهرة عفوا أقصد جامعة الإسكندرية واقرأ العنوان', 'افتح جامعة الإسكندرية واقرأ العنوان'), ('أرسل الرسالة إلى علي عفوا أقصد إلى خالد ثم أغلق الصفحة', 'أرسل الرسالة إلى خالد ثم أغلق الصفحة')]),
    'correction-feedback': history(EN, TEXT, feedback=True),
    'shorter-phrase': history(AR, 'ابحث عن مواعيد متحف اللوفر في أبو ظبي عفوا أقصد في باريس وافتح الموقع الرسمي ولخص المواعيد'),
}

if __name__ == '__main__':
    results = []
    with httpx.Client(base_url='http://127.0.0.1:8788', timeout=180) as client:
        for variant, messages in variants.items():
            result = client.post('/v1/chat/completions', json={'model': 'k2-cleaner', 'messages': messages, 'max_tokens': 1024, 'temperature': 0})
            if result.is_error:
                item = {'variant': variant, 'error': result.text, 'matches_development_target': False}
                results.append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
                continue
            data = result.json()
            output = data['choices'][0]['message']['content'].strip()
            item = {'variant': variant, 'output': output, 'matches_development_target': output == EXPECTED, 'metrics': data['metrics']}
            results.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
    Path('.runtime/validation/k2-prompt-development.json').write_text(json.dumps({'development_only': True, 'results': results}, ensure_ascii=False, indent=2) + '\n')
