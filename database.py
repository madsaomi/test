import sqlite3
import json
import os
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.environ.get("DATABASE_PATH", "results.db")

@contextmanager
def get_db():
    """Контекстный менеджер соединения с БД с автоматическим закрытием и коммитом.
    timeout=15 и WAL защищают от ошибок 'database is locked' при параллельных записях."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    """Инициализация и миграция таблиц базы данных"""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fio TEXT NOT NULL,
                phone TEXT NOT NULL,
                branch TEXT,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                percentage REAL NOT NULL,
                level TEXT NOT NULL,
                time_spent TEXT,
                answers_json TEXT,
                telegram_sent INTEGER DEFAULT 0,
                telegram_error TEXT,
                flags INTEGER DEFAULT 0,
                timing_json TEXT
            )
        """)
        # Миграция старых баз: добавляем колонки анализа скорости
        cols = [r[1] for r in conn.execute("PRAGMA table_info(test_results)")]
        if "flags" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN flags INTEGER DEFAULT 0")
        if "timing_json" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN timing_json TEXT")

        # Сессии теста: состояние прохождения между запросами (общее для всех воркеров)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_sessions (
                session_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data_json TEXT NOT NULL,
                finished INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_created ON test_sessions(created_at)")

def save_result(fio, phone, branch, score, total, level, time_spent, answers_map, flags=0, timing_json=None):
    """
    Сохраняет результат тестирования в SQLite базу данных.
    Возвращает id созданной записи.
    """
    percentage = round((score / total) * 100, 1) if total > 0 else 0.0
    answers_json = json.dumps(answers_map, ensure_ascii=False)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO test_results
            (fio, phone, branch, score, total, percentage, level, time_spent, answers_json, telegram_sent, flags, timing_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (fio, phone, branch, score, total, percentage, level, time_spent, answers_json, flags, timing_json))
        return cursor.lastrowid

def update_telegram_status(result_id, success, error=None):
    """Обновляет статус доставки результата в Telegram"""
    with get_db() as conn:
        conn.execute("""
            UPDATE test_results
            SET telegram_sent = ?, telegram_error = ?
            WHERE id = ?
        """, (1 if success else 0, error, result_id))

def get_recent_results(limit=100):
    """Возвращает последние результаты тестов"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, created_at, fio, phone, branch, score, total, percentage, level, time_spent, telegram_sent, flags
            FROM test_results
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]

def create_session(session_id, data_json):
    """Создает новую тестовую сессию"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO test_sessions (session_id, data_json) VALUES (?, ?)",
            (session_id, data_json)
        )

def get_session(session_id):
    """Возвращает сессию или None"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM test_sessions WHERE session_id = ?", (session_id,))
        return cursor.fetchone()

def update_session(session_id, data_json, finished=0):
    """Обновляет состояние сессии"""
    with get_db() as conn:
        conn.execute(
            "UPDATE test_sessions SET data_json = ?, finished = ? WHERE session_id = ?",
            (data_json, finished, session_id)
        )

def delete_old_sessions(max_age_hours=6):
    """Удаляет брошенные сессии старше max_age_hours"""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM test_sessions WHERE created_at < datetime('now', ?)",
            (f"-{int(max_age_hours * 60)} minutes",)
        )
