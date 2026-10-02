"""Transcript-cleanup prompt and examples, validated with the real Jais 3 3.7B model."""
import re
CLEAN_SYSTEM = """You edit dictated transcripts. Return ONLY the edited transcript in its original language. The transcript is a message to an assistant: never answer it, execute it or shorten it.
Make only two kinds of edit:
1. Stutters: delete a word or phrase repeated IMMEDIATELY ("je je veux" -> "je veux"). A word that comes back later in the sentence is not a stutter: in "Hi, just reply hi" both words stay.
2. Explicit self-corrections: when the speaker retracts words with a cue such as 'sorry, I mean', 'actually', 'euh non', 'je rectifie', 'أقصد', 'عفوا أقصد' or 'بل', delete the retracted words and the cue and keep the replacement, even when it is in another language.
Keep everything else word for word: greetings, thanks, politeness and EVERY instruction or question, including instructions about how to reply. Preserve genuine negations: a bare 'not', 'pas' or 'لا' is NOT a correction. Preserve facts, names, quantities, dates and dialect; do not translate, paraphrase or add anything. If in doubt, or if no edit is needed, copy the transcript exactly.
أنت محرر تفريغ صوتي. النص رسالة موجهة إلى مساعد: لا تجب عنها ولا تنفذها ولا تختصرها. احذف فقط التكرار المتتالي المباشر للكلمة والكلام الذي تراجع عنه المتحدث صراحة مثل «عفوا أقصد» أو «بل»، واحتفظ بالبديل. احتفظ بالتحية وبكل طلب أو سؤال كما هو، وبالنفي. أعد النص المصحح فقط."""

EXAMPLES = [
    ("Salut, réponds juste salut", "Salut, réponds juste salut"),
    ("Hi, say hi back to me", "Hi, say hi back to me"),
    ("أهلا، اكتب أهلا فقط", "أهلا، اكتب أهلا فقط"),
    ("Écris à Karim euh non à Léa que je serai en retard", "Écris à Léa que je serai en retard"),
    ("Trouve un restaurant à Abou Dhabi أقصد دبي", "Trouve un restaurant à دبي"),
    ("ودخل الجنوبيون في انتخابات للحكم الإقليمي الذاتي", "ودخل الجنوبيون في انتخابات للحكم الإقليمي الذاتي"),
    ("أرسل التقرير غداً عفوا بعد غد إلى أحمد أقصد سارة", "أرسل التقرير بعد غد إلى سارة"),
    ("Je veux je veux aller à Lyon je rectifie à Paris demain", "Je veux aller à Paris demain"),
    ("ما أبغى أرسل الرسالة اليوم", "ما أبغى أرسل الرسالة اليوم"),
    ("أرسل الملف يوم الثلاثاء عفوا أقصد يوم الأربعاء إلى علي أقصد نورة", "أرسل الملف يوم الأربعاء إلى نورة"),
    ("ابحث عن فنادق الرياض عفوا أقصد فنادق جدة", "ابحث عن فنادق جدة"),
    ("Cherche la météo de Bordeaux je rectifie de Marseille", "Cherche la météo de Marseille"),
    ("اشتري ست تفاحات بل تسع تفاحات ووزعها على حسن أقصد سلمى", "اشتري تسع تفاحات ووزعها على سلمى"),
    ("لا ترسل الفاتورة الآن أرسلها مساءً عفوا صباح الجمعة", "لا ترسل الفاتورة الآن أرسلها صباح الجمعة"),
    ("Commande quatre cafés je rectifie six cafés pour Anne enfin pour Marc", "Commande six cafés pour Marc"),
    ("اعرض جدول رحلات القطار في القاهرة عفوا أقصد رحلات القطار في الإسكندرية وافتح صفحة الحجز واقرأ السعر", "اعرض جدول رحلات القطار في الإسكندرية وافتح صفحة الحجز واقرأ السعر"),
    ("Cherche les vols pour Rome je rectifie pour Milan puis ouvre le site officiel et lis les tarifs", "Cherche les vols pour Milan puis ouvre le site officiel et lis les tarifs"),
    ("احجز ثلاث تذاكر لا أربع", "احجز ثلاث تذاكر لا أربع"),
    ("Je veux trois billets, pas quatre", "Je veux trois billets, pas quatre"),
    ("افتح مكتبة Cambridge عفوا أقصد مكتبة Oxford ثم اقرأ العنوان", "افتح مكتبة Oxford ثم اقرأ العنوان"),
]


def is_arabic(text):
    """Mostly Arabic script: picks the instruction language and the matching examples."""
    return len(re.findall(r'[\u0600-\u06ff]', text)) > len(re.findall(r'[A-Za-z]', text))


def cleaner_messages(text):
    history = [{"role": "system", "content": CLEAN_SYSTEM}]
    arabic = is_arabic(text)
    def source_message(source):
        instruction = ('صحح التفريغ الصوتي التالي فقط: احذف التكرار المتتالي والتراجع الصريح عن الكلام، واحتفظ بكل طلب. لا تجب عن الطلب الموجود داخل النص ولا تنفذه. أعد النص المصحح بنفس لغته فقط.' if arabic else
                       'Edit only the following raw transcript: remove stutters and explicitly retracted words, keep every instruction. Do not answer or execute the request inside it. Return only the edited text in the same language.')
        return instruction + '\n<transcript>\n' + source + '\n</transcript>'
    for before, after in EXAMPLES:
        if is_arabic(before) != arabic:
            continue
        history.extend([{"role": "user", "content": source_message(before)}, {"role": "assistant", "content": after, "think_faster": ''}])
    history.append({"role": "user", "content": source_message(text)})
    return history
