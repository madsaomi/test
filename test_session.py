"""Банк вопросов и управление сессиями теста.

Каждый студент получает персональный тест: случайная выборка 50 вопросов
из банка (по фиксированному плану уровней), перемешанный порядок вариантов.
Вопросы выдаются по одному, ответы проверяются на сервере и хранятся в сессии.
"""
import json
import os
import random
import secrets
import time

import database

BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'questions.json')

# План одного теста (50 вопросов): уровень -> количество
# Сложность идет по возрастанию, чтобы сохранить калибровку уровней Beginner..Upper-Intermediate+
_CHOICE_PLAN = [("A1", 10), ("A2", 10), ("B1", 10), ("B2", 10), ("C1", 5)]
_INPUT_PLAN = [("B1", 2), ("B2", 2), ("C1", 1)]

# Минимальное разумное время ответа, сек. Быстрее — ответ помечается как подозрительный.
MIN_ANSWER_SECONDS = {"choice": 1.2, "input": 2.0}

with open(BANK_PATH, encoding='utf-8') as _f:
    QUESTION_BANK = json.load(_f)


def _validate_bank(bank):
    """Проверка целостности банка при старте (fail fast)"""
    if len(bank) < 50:
        raise RuntimeError("Банк вопросов содержит меньше 50 вопросов")
    for q in bank:
        for field in ("id", "type", "question", "answer", "level"):
            if field not in q:
                raise RuntimeError(f"Вопрос {q.get('id')}: нет поля '{field}'")
        if q["type"] == "choice":
            if len(q.get("options", [])) != 4:
                raise RuntimeError(f"Вопрос {q['id']}: не 4 варианта ответа")
            if q["answer"] not in q["options"]:
                raise RuntimeError(f"Вопрос {q['id']}: ответ не среди вариантов")
    ids = [q["id"] for q in bank]
    if len(ids) != len(set(ids)):
        raise RuntimeError("В банке есть вопросы с дублирующимися id")
    for lvl, cnt in _CHOICE_PLAN:
        pool = [q for q in bank if q["type"] == "choice" and q["level"] == lvl]
        if len(pool) < cnt:
            raise RuntimeError(f"Не хватает вопросов уровня {lvl}: {len(pool)} < {cnt}")
    for lvl, cnt in _INPUT_PLAN:
        pool = [q for q in bank if q["type"] == "input" and q["level"] == lvl]
        if len(pool) < cnt:
            raise RuntimeError(f"Не хватает открытых вопросов уровня {lvl}: {len(pool)} < {cnt}")


_validate_bank(QUESTION_BANK)


def build_session_questions():
    """Собирает персональный набор из 50 вопросов (позиции 1..50):
    случайная выборка по уровням + перемешанные варианты ответов."""
    questions = []
    pos = 1
    for lvl, cnt in _CHOICE_PLAN:
        pool = [q for q in QUESTION_BANK if q["type"] == "choice" and q["level"] == lvl]
        for q in random.sample(pool, cnt):
            options = q["options"][:]
            random.shuffle(options)
            questions.append({
                "id": pos, "type": "choice", "question": q["question"],
                "options": options, "answer": q["answer"], "time": 30
            })
            pos += 1
    for lvl, cnt in _INPUT_PLAN:
        pool = [q for q in QUESTION_BANK if q["type"] == "input" and q["level"] == lvl]
        for q in random.sample(pool, cnt):
            questions.append({
                "id": pos, "type": "input", "question": q["question"],
                "answer": q["answer"], "time": 40
            })
            pos += 1
    return questions


def create_session():
    """Создает сессию теста. Возвращает (sid, данные сессии)."""
    sid = secrets.token_hex(16)
    data = {
        "questions": build_session_questions(),
        "current": 1,          # номер вопроса, который ждем от студента
        "answers": {},         # позиция -> сырой ответ
        "correct": {},         # позиция -> 1/0 (считается сервером)
        "seconds": {},         # позиция -> время ответа по серверным часам
        "shown_at": time.time(),
        "too_fast": 0,         # счетчик сверхбыстрых ответов (флаг списывания)
        "started_at": time.time(),
        "finished": False,
        "submitted": False,
    }
    database.create_session(sid, json.dumps(data, ensure_ascii=False))
    return sid, data


def load_session(sid):
    """Возвращает данные сессии или None."""
    row = database.get_session(sid)
    return json.loads(row["data_json"]) if row else None


def save_session(sid, data, finished=False):
    """Сохраняет состояние сессии."""
    database.update_session(sid, json.dumps(data, ensure_ascii=False), finished=1 if finished else 0)


def cleanup_sessions(max_age_hours=6):
    """Удаляет брошенные сессии старше max_age_hours."""
    database.delete_old_sessions(max_age_hours)
