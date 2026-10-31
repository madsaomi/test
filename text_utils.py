import re

def normalize_text(text):
    """Нормализация текста для сравнения ответов: нижний регистр,
    единый апостроф, без пунктуации, схлопнутые пробелы."""
    t = str(text).replace('’', "'").replace('`', "'")
    t = re.sub(r'[^\w\s\']', '', t.lower())
    return " ".join(t.split())
