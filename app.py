import os
import json
import html
import re
import time
import secrets
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import database
import sheet_generator
import test_session
from text_utils import normalize_text

app = Flask(__name__)

# Инициализация базы данных SQLite
database.init_db()

# Пул фоновых потоков для неблокирующей отправки уведомлений
executor = ThreadPoolExecutor(max_workers=3)

# --- НАСТРОЙКИ TELEGRAM (Загружаются из .env или панели хостинга) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# Токен админ-эндпоинта /admin/results (эндпоинт выключен, если не задан)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# Простейший анти-спам: не более одной успешной отправки с IP за период
RATE_LIMIT_SECONDS = 60
_last_submit_by_ip = {}

# Анти-спам на создание сессий: не более 5 стартов теста с IP за 10 минут
SESSION_START_LIMIT = 5
SESSION_START_WINDOW = 600
_session_starts_by_ip = {}

# Минимальное общее время прохождения, ниже которого результат помечается флагом
MIN_TOTAL_SECONDS = 360


def get_chat_ids():
    """Возвращает список всех ID получателей из переменных окружения"""
    if CHAT_ID:
        return [cid.strip() for cid in CHAT_ID.split(",") if cid.strip()]
    return []

def _send_tg_worker(message, result_id=None, photo_bytes=None):
    """
    Фоновый воркер: отправляет заполненный бланк (или текстовое сообщение)
    в Telegram с ретраями и фиксирует статус в БД.
    """
    chat_ids = get_chat_ids()
    if not BOT_TOKEN or not chat_ids:
        err_msg = "Telegram не настроен (BOT_TOKEN или CHAT_ID отсутствуют в переменных окружения)"
        print(f"WARNING: {err_msg}")
        if result_id:
            database.update_telegram_status(result_id, success=False, error=err_msg)
        return

    photo_data = photo_bytes.getvalue() if photo_bytes else None
    all_success = True
    errors = []

    for cid in chat_ids:
        sent_ok = False
        last_error = None
        # До 3 попыток: сразу, через 5 и через 15 секунд.
        # Ретраим только сетевые сбои и 429/5xx — явные ошибки API ретраить бессмысленно.
        for attempt in range(3):
            try:
                if photo_data:
                    # Отправляем заполненный бланк как фото с подписью
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
                    files = {'photo': ('blank_answers.jpg', photo_data, 'image/jpeg')}
                    data = {
                        'chat_id': cid,
                        'caption': message,
                        'parse_mode': 'HTML'
                    }
                    resp = requests.post(url, data=data, files=files, timeout=25)
                else:
                    # Текстовое сообщение, если изображение не сгенерировалось
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                    resp = requests.post(url, json={
                        "chat_id": cid,
                        "text": message,
                        "parse_mode": "HTML"
                    }, timeout=10)

                if resp.ok:
                    sent_ok = True
                    break

                last_error = f"Чат {cid}: HTTP {resp.status_code} ({resp.text[:100]})"
                print(f"Ошибка отправки в Telegram: {last_error}")
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break
            except Exception as e:
                last_error = f"Чат {cid}: {e}"
                print(f"Ошибка сети при отправке в чат {cid}: {e}")

            if attempt < 2:
                time.sleep(5 * (attempt + 1))

        if not sent_ok:
            all_success = False
            errors.append(last_error or f"Чат {cid}: неизвестная ошибка")

    if result_id:
        database.update_telegram_status(
            result_id,
            success=all_success,
            error="; ".join(errors) if errors else None
        )

def send_to_telegram_async(message, result_id=None, photo_bytes=None):
    """Мгновенный запуск отправки в пуле фоновых потоков"""
    executor.submit(_send_tg_worker, message, result_id, photo_bytes)

def calculate_level(score, total=50):
    """
    Градация уровней по баллам (при 50 вопросах):
      0 - 15 : Beginner
     16 - 24 : Elementary
     25 - 32 : Pre-Intermediate
     33 - 39 : Intermediate
     40 - 45 : Upper-Intermediate
     46 - 50 : Upper-Intermediate+
    """
    if total == 50:
        if score <= 15:
            return "Beginner"
        elif score <= 24:
            return "Elementary"
        elif score <= 32:
            return "Pre-Intermediate"
        elif score <= 39:
            return "Intermediate"
        elif score <= 45:
            return "Upper-Intermediate"
        else:
            return "Upper-Intermediate+"

    # Резервный расчет по процентным долям на случай изменения числа вопросов
    pct = (score / total) * 100 if total > 0 else 0
    if pct <= 30:       # 15 / 50
        return "Beginner"
    elif pct <= 48:     # 24 / 50
        return "Elementary"
    elif pct <= 64:     # 32 / 50
        return "Pre-Intermediate"
    elif pct <= 78:     # 39 / 50
        return "Intermediate"
    elif pct <= 90:     # 45 / 50
        return "Upper-Intermediate"
    else:               # 46+ / 50
        return "Upper-Intermediate+"

def get_client_ip():
    """IP клиента (за прокси берем первый X-Forwarded-For)"""
    return request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()

def client_question(q):
    """Вопрос для клиента: без правильного ответа, с персональным порядком вариантов"""
    item = {
        "id": q["id"],
        "type": q["type"],
        "question": q["question"],
        "time_limit": q["time"],
    }
    if q["type"] == "choice":
        item["options"] = q["options"]
    return item

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/test/start', methods=['POST'])
def test_start():
    """Создает персональную сессию теста и возвращает первый вопрос"""
    ip = get_client_ip()
    now_ts = time.time()

    # Анти-спам: лимит стартов с одного IP
    starts = [t for t in _session_starts_by_ip.get(ip, []) if now_ts - t < SESSION_START_WINDOW]
    if len(starts) >= SESSION_START_LIMIT:
        return jsonify({"status": "error", "message": "Слишком много попыток. Попробуйте позже."}), 429
    starts.append(now_ts)
    _session_starts_by_ip[ip] = starts

    # Периодическая чистка брошенных сессий
    try:
        test_session.cleanup_sessions()
    except Exception as e:
        print(f"Ошибка очистки сессий: {e}")

    sid, data = test_session.create_session()
    question = data["questions"][0]
    data["shown_at"] = time.time()
    test_session.save_session(sid, data)

    return jsonify({
        "status": "success",
        "sid": sid,
        "total": len(data["questions"]),
        "question": client_question(question)
    })

@app.route('/api/question/<sid>', methods=['GET'])
def api_question(sid):
    """Возвращает текущий вопрос сессии (используется для ресинхронизации)"""
    data = test_session.load_session(sid)
    if not data:
        return jsonify({"status": "error", "message": "Сессия не найдена"}), 404

    idx = data["current"] - 1
    if idx >= len(data["questions"]):
        return jsonify({"status": "success", "finished": True})

    data["shown_at"] = time.time()
    test_session.save_session(sid, data)
    return jsonify({
        "status": "success",
        "total": len(data["questions"]),
        "question": client_question(data["questions"][idx])
    })

@app.route('/api/answer/<sid>', methods=['POST'])
def api_answer(sid):
    """Принимает ответ на текущий вопрос и сразу возвращает следующий"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Некорректный формат запроса"}), 400

    data = test_session.load_session(sid)
    if not data:
        return jsonify({"status": "error", "message": "Сессия не найдена"}), 404
    if data.get("finished"):
        return jsonify({"status": "error", "message": "Тест уже завершен"}), 400

    pos = payload.get("pos")
    if not isinstance(pos, int) or pos != data["current"]:
        return jsonify({"status": "error", "message": "Несинхронизированный вопрос"}), 409

    answer = payload.get("answer", "")
    if not isinstance(answer, str) or len(answer) > 200:
        return jsonify({"status": "error", "message": "Некорректный ответ"}), 400

    q = data["questions"][pos - 1]
    elapsed = time.time() - data.get("shown_at", time.time())

    # Флаг скорости: человек не может прочитать вопрос быстрее минимума
    if answer.strip() and elapsed < test_session.MIN_ANSWER_SECONDS[q["type"]]:
        data["too_fast"] += 1

    # Серверная проверка ответа — клиент правильных ответов не знает
    data["answers"][str(pos)] = answer
    norm = normalize_text(answer)
    raw_ans = q["answer"]
    if isinstance(raw_ans, list):
        is_correct = bool(answer) and norm in [normalize_text(a) for a in raw_ans]
    else:
        is_correct = bool(answer) and norm == normalize_text(raw_ans)
    data["correct"][str(pos)] = 1 if is_correct else 0
    data["seconds"][str(pos)] = round(elapsed, 1)

    data["current"] = pos + 1
    finished = data["current"] > len(data["questions"])
    if finished:
        data["finished"] = True
    else:
        data["shown_at"] = time.time()  # старт отсчета для следующего вопроса
    test_session.save_session(sid, data, finished=finished)

    resp = {"status": "success"}
    if not finished:
        resp["next"] = client_question(data["questions"][data["current"] - 1])
    return jsonify(resp)

@app.route('/submit', methods=['POST'])
def submit():
    """Завершает сессию: сохраняет результат (баллы из сессии) и шлет в Telegram"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Некорректный формат запроса"}), 400

    # Honeypot: скрытое поле, которое заполняют только боты
    if str(payload.get('hp', '')).strip():
        return jsonify({"status": "error", "message": "Ошибка отправки"}), 400

    sid = str(payload.get('sid', '')).strip()
    if not re.fullmatch(r'[0-9a-f]{32}', sid):
        return jsonify({"status": "error", "message": "Некорректная сессия теста"}), 400

    session_data = test_session.load_session(sid)
    if not session_data:
        return jsonify({"status": "error", "message": "Сессия не найдена. Пройдите тест заново."}), 404
    if not session_data.get('finished'):
        return jsonify({"status": "error", "message": "Тест не завершен"}), 400
    if session_data.get('submitted'):
        return jsonify({"status": "error", "message": "Результат уже отправлен"}), 400

    client_ip = get_client_ip()
    now_ts = time.time()

    # Анти-спам: одна успешная отправка с IP за RATE_LIMIT_SECONDS
    last_submit_ts = _last_submit_by_ip.get(client_ip)
    if last_submit_ts and now_ts - last_submit_ts < RATE_LIMIT_SECONDS:
        return jsonify({"status": "error", "message": "Результат уже отправлен. Повторите попытку через минуту."}), 429
    # Чистим старые записи, чтобы словарь не рос бесконечно
    if len(_last_submit_by_ip) > 5000:
        cutoff = now_ts - RATE_LIMIT_SECONDS * 2
        for stale_ip in [ip for ip, ts in _last_submit_by_ip.items() if ts < cutoff]:
            _last_submit_by_ip.pop(stale_ip, None)

    raw_fio = str(payload.get('fio', '')).strip()
    raw_phone = str(payload.get('phone', '')).strip()
    raw_branch = str(payload.get('branch', '')).strip()

    # Серверная валидация ФИО
    if not raw_fio or len(raw_fio) < 3:
        return jsonify({"status": "error", "message": "Введите корректное ФИО"}), 400

    # Проверка формата телефона (+998XXXXXXXXX)
    clean_digits = re.sub(r'\D', '', raw_phone)
    if len(clean_digits) != 12 or not clean_digits.startswith('998'):
        return jsonify({"status": "error", "message": "Некорректный номер телефона"}), 400

    # Сырые значения — для БД и бланка; экранирование HTML делаем только в тексте для Telegram
    fio = raw_fio[:100]
    phone = "+" + clean_digits
    branch = raw_branch[:50]
    time_spent = str(payload.get('time', 'Не указано')).strip()[:30]
    now = datetime.now().strftime('%d.%m.%Y %H:%M')

    # Баллы считаются только из серверной сессии — клиент не присылает ответы
    total = len(session_data["questions"])
    score = sum(session_data["correct"].values())
    level = calculate_level(score, total)

    # Анализ скорости прохождения (серверные замеры)
    total_elapsed = now_ts - session_data.get("started_at", now_ts)
    too_fast = session_data.get("too_fast", 0)
    fast_total = total_elapsed < MIN_TOTAL_SECONDS
    flags = too_fast + (1 if fast_total else 0)
    timing_json = json.dumps({
        "total_sec": int(total_elapsed),
        "per_question": session_data.get("seconds", {}),
        "too_fast": too_fast,
        "fast_total": fast_total
    }, ensure_ascii=False)

    # Персистентное сохранение в базу данных SQLite (защита от потери заявки)
    result_id = database.save_result(
        fio=fio,
        phone=phone,
        branch=branch,
        score=score,
        total=total,
        level=level,
        time_spent=time_spent,
        answers_map=session_data["answers"],
        flags=flags,
        timing_json=timing_json
    )

    # Сессия использована — повторная отправка невозможна
    session_data["submitted"] = True
    test_session.save_session(sid, session_data, finished=True)

    # Отправка принята — отмечаем IP для анти-спама
    _last_submit_by_ip[client_ip] = now_ts

    # Отметка о скорости для администратора
    if flags:
        speed_line = f"🚩 <b>Скорость:</b> подозрительная ({too_fast} сверхбыстрых, {int(total_elapsed)} сек общее)\n"
    else:
        speed_line = f"✅ <b>Скорость:</b> в норме ({int(total_elapsed)} сек)\n"

    # Формируем текстовую подпись к фото в Telegram (экранируем пользовательские данные)
    message = (
        f"📝 <b>Новый результат теста!</b>\n\n"
        f"👤 <b>ФИО:</b> {html.escape(fio)}\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"🏢 <b>Филиал:</b> {html.escape(branch)}\n\n"
        f"📊 <b>Результат:</b> {score} из {total}\n"
        f"🎓 <b>Уровень:</b> {level}\n"
        f"⏱ <b>Время:</b> {html.escape(time_spent)}\n"
        f"{speed_line}"
        f"📅 <b>Дата:</b> {now}\n"
        f"🆔 <b>ID записи:</b> #{result_id}"
    )

    # Генерация официального бланка ответов с подсветкой (по вопросам этой сессии)
    photo_bytes = None
    try:
        res_data = {
            'id': result_id,
            'fio': fio,
            'phone': phone,
            'branch': branch,
            'date': now,
            'score': score,
            'total': total,
            'level': level,
            'answers': session_data["answers"]
        }
        photo_bytes = sheet_generator.generate_filled_sheet(
            result_data=res_data,
            all_questions=session_data["questions"]
        )
    except Exception as e:
        print(f"Ошибка генерации бланка ответов: {e}")

    # Фоновая отправка в Telegram без блокировки клиента
    send_to_telegram_async(message, result_id=result_id, photo_bytes=photo_bytes)

    return jsonify({
        "status": "success",
        "message": "Результаты успешно сохранены и приняты",
        "result_id": result_id,
        "score": score,
        "total": total,
        "level": level
    })

@app.route('/admin/results')
def admin_results():
    """Список последних результатов. Доступен только с ADMIN_TOKEN
    (заголовок X-Admin-Token или параметр ?key=). Если токен не задан — 404."""
    if not ADMIN_TOKEN:
        return jsonify({"status": "error", "message": "Не найдено"}), 404

    token = request.headers.get('X-Admin-Token', '') or request.args.get('key', '')
    if not secrets.compare_digest(token, ADMIN_TOKEN):
        return jsonify({"status": "error", "message": "Доступ запрещен"}), 403

    try:
        limit = min(int(request.args.get('limit', 100)), 500)
    except ValueError:
        limit = 100

    return jsonify({"status": "success", "results": database.get_recent_results(limit)})

@app.errorhandler(400)
def bad_request(e):
    return jsonify({"status": "error", "message": "Некорректный формат запроса"}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"status": "error", "message": "Не найдено"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"status": "error", "message": "Метод не поддерживается"}), 405

@app.errorhandler(500)
def internal_error(e):
    """Ошибки сервера отдаются клиенту в JSON — фронтенд ожидает JSON-ответ"""
    return jsonify({"status": "error", "message": "Внутренняя ошибка сервера. Попробуйте позже."}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
