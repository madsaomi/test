import sqlite3
import json
import os
import time
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
        # Gunicorn-воркеры стартуют одновременно: без блокировки оба увидят, что
        # колонки нет, и второй ALTER упадет с 'duplicate column' — воркер не загрузится.
        # IMMEDIATE заставляет второй воркер дождаться конца миграции первого.
        conn.execute("BEGIN IMMEDIATE")
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
                timing_json TEXT,
                work_study TEXT,
                phone2 TEXT,
                source TEXT
            )
        """)
        # Миграция старых баз: добавляем новые колонки
        cols = [r[1] for r in conn.execute("PRAGMA table_info(test_results)")]
        if "flags" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN flags INTEGER DEFAULT 0")
        if "timing_json" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN timing_json TEXT")
        if "work_study" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN work_study TEXT")
        if "phone2" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN phone2 TEXT")
        if "source" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN source TEXT")
        # Снимок вопросов сессии (персональный порядок и варианты) — чтобы
        # пересобрать фото-бланк при досылке, если Telegram был недоступен
        if "questions_json" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN questions_json TEXT")
        # Досылка в Telegram: число попыток и время захвата записи воркером.
        # telegram_sent: 0 — не доставлено, 1 — доставлено, 2 — досылается сейчас
        if "telegram_attempts" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN telegram_attempts INTEGER DEFAULT 0")
        if "telegram_claimed_at" not in cols:
            conn.execute("ALTER TABLE test_results ADD COLUMN telegram_claimed_at TIMESTAMP")

        # Анти-спам: отметки запросов по IP (общие для всех gunicorn-воркеров)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_hits (
                kind TEXT NOT NULL,
                ip TEXT NOT NULL,
                ts REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_hits ON rate_hits(kind, ip, ts)")
        # История сдач по номеру телефона (пометка повторных попыток)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_results_phone ON test_results(phone)")

        # Сессии теста: состояние прохождения между запросами (общее для всех воркеров)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_sessions (
                session_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data_json TEXT NOT NULL,
                finished INTEGER DEFAULT 0,
                submitted INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_created ON test_sessions(created_at)")
        # Миграция старых баз: атомарная метка отправки сессии
        cols = [r[1] for r in conn.execute("PRAGMA table_info(test_sessions)")]
        if "submitted" not in cols:
            conn.execute("ALTER TABLE test_sessions ADD COLUMN submitted INTEGER DEFAULT 0")

        # Маркеры отправленных отчетов (месячных/годовых): защита от дублей между воркерами
        conn.execute("""
            CREATE TABLE IF NOT EXISTS report_markers (
                period_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

def save_result(fio, phone, branch, score, total, level, time_spent, answers_map,
                flags=0, timing_json=None, work_study=None, phone2=None, source=None,
                questions_json=None):
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
            (fio, phone, branch, score, total, percentage, level, time_spent, answers_json, telegram_sent, flags, timing_json, work_study, phone2, source, questions_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
        """, (fio, phone, branch, score, total, percentage, level, time_spent, answers_json, flags, timing_json, work_study, phone2, source, questions_json))
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
            SELECT id, created_at, fio, phone, branch, score, total, percentage, level, time_spent, telegram_sent, flags, work_study, phone2, source
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

def mark_session_submitted(session_id):
    """Атомарно помечает сессию отправленной.
    Возвращает True, если пометка поставлена этим вызовом (первым), иначе False —
    защита от гонки параллельных /submit (дубликатов результата)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE test_sessions SET submitted = 1 WHERE session_id = ? AND submitted = 0",
            (session_id,)
        )
        return cursor.rowcount == 1

def unmark_session_submitted(session_id):
    """Снимает метку отправки (если сохранение результата упало — студент может повторить)"""
    with get_db() as conn:
        conn.execute(
            "UPDATE test_sessions SET submitted = 0 WHERE session_id = ?",
            (session_id,)
        )

def delete_old_sessions(max_age_hours=6):
    """Удаляет брошенные сессии старше max_age_hours"""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM test_sessions WHERE created_at < datetime('now', ?)",
            (f"-{int(max_age_hours * 60)} minutes",)
        )

def claim_report(period_key, kind):
    """Атомарно захватывает право отправить отчет за период.
    True — этот воркер первый (ему и отправлять), False — уже отправлено/отправляется."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO report_markers (period_key, kind) VALUES (?, ?)",
            (period_key, kind)
        )
        return cursor.rowcount == 1

def release_report(period_key):
    """Освобождает маркер (отправка не удалась — следующий цикл повторит)"""
    with get_db() as conn:
        conn.execute("DELETE FROM report_markers WHERE period_key = ?", (period_key,))

def get_results_in_range(start_utc, end_utc):
    """Результаты с created_at в диапазоне [start_utc, end_utc) (строки 'YYYY-MM-DD HH:MM:SS')"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, created_at, fio, phone, branch, score, percentage, level, flags
            FROM test_results
            WHERE created_at >= ? AND created_at < ?
            ORDER BY id ASC
        """, (start_utc, end_utc))
        return [dict(row) for row in cursor.fetchall()]

def claim_unsent_results(limit=10, min_age_minutes=15, max_age_days=7,
                         max_attempts=10, lease_minutes=30):
    """Атомарно захватывает недоставленные в Telegram результаты для досылки.

    - свежие (моложе min_age_minutes) не трогаем: их еще досылает воркер /submit;
    - старше max_age_days не трогаем: не заваливаем админов архивом;
    - после max_attempts досылок сдаемся (например, бот удален из чата);
    - запись, захваченная упавшим воркером, освобождается через lease_minutes.
    Возвращает захваченные строки со всем нужным для пересборки бланка."""
    pending = """
        telegram_attempts < ?
        AND created_at >= datetime('now', ?)
        AND ((telegram_sent = 0 AND created_at < datetime('now', ?))
             OR (telegram_sent = 2 AND telegram_claimed_at < datetime('now', ?)))
    """
    params = (max_attempts, f"-{int(max_age_days)} days",
              f"-{int(min_age_minutes)} minutes", f"-{int(lease_minutes)} minutes")

    claimed = []
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT id FROM test_results WHERE {pending} ORDER BY id ASC LIMIT ?",
                       params + (limit,))
        for (result_id,) in cursor.fetchall():
            # Повторяем условие в UPDATE: второй воркер ту же запись не захватит
            cursor.execute(f"""
                UPDATE test_results
                SET telegram_sent = 2, telegram_claimed_at = CURRENT_TIMESTAMP,
                    telegram_attempts = telegram_attempts + 1
                WHERE id = ? AND {pending}
            """, (result_id,) + params)
            if cursor.rowcount == 1:
                claimed.append(result_id)
        conn.commit()

        if not claimed:
            return []
        marks = ",".join("?" * len(claimed))
        cursor.execute(f"""
            SELECT id, created_at, fio, phone, branch, score, total, level, time_spent,
                   answers_json, questions_json, work_study, phone2, source, flags, timing_json
            FROM test_results
            WHERE id IN ({marks})
            ORDER BY id ASC
        """, claimed)
        return [dict(row) for row in cursor.fetchall()]

def phone_history(phone, before_id):
    """Прошлые сдачи с этого номера (до записи before_id): (количество, последняя или None).
    Студент может пройти тест несколько раз и отправить лучшую попытку —
    админ должен это видеть."""
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM test_results WHERE phone = ? AND id < ?", (phone, before_id)
        ).fetchone()[0]
        last = conn.execute(
            "SELECT score, total, created_at FROM test_results WHERE phone = ? AND id < ? ORDER BY id DESC LIMIT 1",
            (phone, before_id)
        ).fetchone()
        return count, (dict(last) if last else None)

def backup_snapshot(dest_path):
    """Согласованная копия базы в dest_path (для бэкапа).
    VACUUM INTO безопасен при параллельной записи и сжимает файл; временные
    таблицы (сессии в процессе, счетчики лимитов) из копии удаляются.
    Возвращает число результатов в копии."""
    if os.path.exists(dest_path):
        os.remove(dest_path)
    with get_db() as conn:
        conn.execute("VACUUM INTO ?", (dest_path,))
    copy = sqlite3.connect(dest_path)
    try:
        copy.execute("DELETE FROM test_sessions")
        copy.execute("DELETE FROM rate_hits")
        copy.commit()
        copy.execute("VACUUM")
        return copy.execute("SELECT COUNT(*) FROM test_results").fetchone()[0]
    finally:
        copy.close()

def rate_allow(kind, ip, limit, window_sec, record=True):
    """Проверка лимита запросов с IP за окно (общая для всех воркеров).
    True — запрос разрешен (и при record=True сразу засчитан), False — лимит исчерпан."""
    now = time.time()
    with get_db() as conn:
        # IMMEDIATE: проверка и запись — одна транзакция, параллельный воркер ждет
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM rate_hits WHERE kind = ? AND ts < ?", (kind, now - window_sec))
        count = conn.execute(
            "SELECT COUNT(*) FROM rate_hits WHERE kind = ? AND ip = ?", (kind, ip)
        ).fetchone()[0]
        if count >= limit:
            return False
        if record:
            conn.execute("INSERT INTO rate_hits (kind, ip, ts) VALUES (?, ?, ?)", (kind, ip, now))
        return True

def rate_record(kind, ip):
    """Засчитывает запрос в лимит (когда считаем только успешные действия)"""
    with get_db() as conn:
        conn.execute("INSERT INTO rate_hits (kind, ip, ts) VALUES (?, ?, ?)", (kind, ip, time.time()))
