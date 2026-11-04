import os
import json
import html
import re
import time
import secrets
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import database
import reports
import sheet_generator
import test_session
from text_utils import normalize_text

app = Flask(__name__)

# Реальный IP клиента за прокси хостинга. Клиент может сам прислать любой
# X-Forwarded-For, поэтому доверяем только последним TRUSTED_PROXIES записям,
# которые дописали наши прокси (Railway/Heroku — 1). Без прокси — 0.
TRUSTED_PROXIES = int(os.environ.get("TRUSTED_PROXIES", "1"))
if TRUSTED_PROXIES > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXIES)

# Инициализация базы данных SQLite
database.init_db()

# Пул фоновых потоков для неблокирующей отправки уведомлений
executor = ThreadPoolExecutor(max_workers=3)

# --- НАСТРОЙКИ TELEGRAM (Загружаются из .env или панели хостинга) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# Токен админ-эндпоинта /admin/results (эндпоинт выключен, если не задан)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# Защита от переполнения: максимум 1 MB в теле запроса
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024

# Анти-спам с учетом реального сценария: студенты филиала сидят за одним Wi-Fi (общий IP),
# поэтому лимиты "классные", а не персональные. Счетчики в SQLite — общие для всех воркеров.
SESSION_START_LIMIT = 60    # стартов теста с IP за 10 минут
SESSION_START_WINDOW = 600

SUBMIT_LIMIT = 30           # успешных отправок с IP за 10 минут
SUBMIT_WINDOW = 600

# Минимальное общее время прохождения, ниже которого результат помечается флагом
MIN_TOTAL_SECONDS = 360

# Минимальный остаток таймера при восстановлении вопроса после перезагрузки
RESYNC_MIN_SECONDS = 3


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
        return False

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

    return all_success

def send_to_telegram_async(message, result_id=None, photo_bytes=None):
    """Мгновенный запуск отправки в пуле фоновых потоков"""
    executor.submit(_send_tg_worker, message, result_id, photo_bytes)

def send_to_telegram_sync(message):
    """Синхронная отправка (для отчетов в фоновом потоке). Возвращает успех."""
    return _send_tg_worker(message)

def speed_line(flags, too_fast, too_slow, total_elapsed):
    """Отметка о скорости прохождения для администратора"""
    if flags:
        return f"🚩 <b>Скорость:</b> подозрительная ({too_fast} сверхбыстрых, {too_slow} затянутых, {int(total_elapsed)} сек общее)"
    return f"✅ <b>Скорость:</b> в норме ({int(total_elapsed)} сек)"

def build_result_message(r, speed, resent=False):
    """Подпись к бланку в Telegram (пользовательские данные экранируются)"""
    title = "📝 <b>Новый результат теста!</b>"
    if resent:
        title += " <i>(досылка)</i>"
    msg_lines = [
        title + "\n",
        f"👤 <b>ФИО:</b> {html.escape(r['fio'])}",
        f"📞 <b>Телефон:</b> {r['phone']}",
    ]
    if r.get('phone2'):
        msg_lines.append(f"📞 <b>Телефон 2:</b> {r['phone2']} <i>(родители/запасной)</i>")
    msg_lines.append(f"🏢 <b>Филиал:</b> {html.escape(r['branch'] or '')}")
    if r.get('work_study'):
        msg_lines.append(f"🎓 <b>Учеба / Работа:</b> {html.escape(r['work_study'])}")
    if r.get('source'):
        msg_lines.append(f"📢 <b>Источник:</b> {html.escape(r['source'])}")

    msg_lines.extend([
        "",
        f"📊 <b>Результат:</b> {r['score']} из {r['total']}",
        f"🎓 <b>Уровень:</b> {r['level']}",
        f"⏱ <b>Время:</b> {html.escape(r['time_spent'] or '')}",
        speed,
        f"📅 <b>Дата:</b> {r['date']}",
        f"🆔 <b>ID записи:</b> #{r['id']}"
    ])
    return "\n".join(msg_lines)

def build_result_sheet(r, questions):
    """Официальный бланк ответов с подсветкой; None, если не сгенерировался"""
    if not questions:
        return None
    try:
        return sheet_generator.generate_filled_sheet(result_data=r, all_questions=questions)
    except Exception as e:
        print(f"Ошибка генерации бланка ответов #{r.get('id')}: {e}")
        return None

# ================= ДОСЫЛКА В TELEGRAM =================
# Если Telegram был недоступен в момент сдачи (все ретраи исчерпаны или сервер
# перезапустился), результат остается с telegram_sent = 0. Фоновый цикл раз в
# 10 минут пересобирает бланк из снимка вопросов и досылает его.

def _resend_unsent():
    if not BOT_TOKEN or not get_chat_ids():
        return  # без настроек не тратим попытки досылки
    for row in database.claim_unsent_results():
        try:
            timing = json.loads(row["timing_json"] or "{}")
            created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S") \
                .replace(tzinfo=timezone.utc).astimezone(reports.TZ_TASHKENT)
            r = dict(row)
            r["date"] = created.strftime('%d.%m.%Y %H:%M')
            r["answers"] = json.loads(row["answers_json"] or "{}")
            speed = speed_line(row["flags"] or 0, timing.get("too_fast", 0),
                               timing.get("too_slow", 0), timing.get("total_sec", 0))
            message = build_result_message(r, speed, resent=True)
            # Старые записи без снимка вопросов досылаются текстом
            questions = json.loads(row["questions_json"]) if row["questions_json"] else None
            photo = build_result_sheet(r, questions)
        except Exception as e:
            database.update_telegram_status(row["id"], success=False, error=f"Досылка: {e}")
            print(f"Ошибка подготовки досылки #{row['id']}: {e}")
            continue
        if _send_tg_worker(message, result_id=row["id"], photo_bytes=photo):
            print(f"Результат #{row['id']} дослан в Telegram")

# ================= ПЕРИОДИЧЕСКИЕ ОТЧЕТЫ =================
# Месячный — 1-го числа за предыдущий месяц (догоняет, даже если сервер был выключен),
# годовой — в первую неделю января за весь год.
# Оба gunicorn-воркера крутят поток-планировщик, но отправляет только тот,
# кто первым атомарно захватил маркер периода в БД.

def _maybe_send_reports():
    now = datetime.now(reports.TZ_TASHKENT)

    # Месячный отчет за предыдущий месяц: маркер в БД не дает отправить дважды
    prev = _prev_month(now)
    key = f"monthly-{prev.strftime('%Y-%m')}"
    if database.claim_report(key, "monthly"):
        _deliver_report(key, prev.year, prev.month)

    # Годовой отчет: в любой день января за предыдущий год
    # (мягкое окно — догонит, даже если первую неделю января сервер не работал)
    if now.month == 1:
        key = f"yearly-{now.year - 1}"
        if database.claim_report(key, "yearly"):
            _deliver_report(key, now.year - 1, None)

def _deliver_report(key, year, month):
    """Отправляет отчет и гарантирует освобождение маркера при любом сбое —
    иначе исключение между claim и send потеряло бы отчет навсегда."""
    try:
        text = reports.build_report(year, month)
    except Exception as e:
        database.release_report(key)
        print(f"Ошибка сборки отчета {key}: {e} — повторим в следующем цикле")
        return

    if not BOT_TOKEN or not get_chat_ids():
        database.release_report(key)
        print(f"Telegram не настроен: отчет {key} будет отправлен после настройки переменных")
        return

    if send_to_telegram_sync(text):
        print(f"Отчет {key} отправлен")
    else:
        database.release_report(key)  # повторим в следующем цикле
        print(f"Не удалось отправить отчет {key} — повторим")

def _prev_month(dt):
    return (dt.replace(day=1) - timedelta(days=1)).replace(day=1)

def _report_worker():
    time.sleep(10)  # даем приложению и БД подняться
    while True:
        try:
            _maybe_send_reports()
        except Exception as e:
            print(f"Ошибка планировщика отчетов: {e}")
        try:
            _resend_unsent()
        except Exception as e:
            print(f"Ошибка досылки результатов: {e}")
        time.sleep(600)  # проверка каждые 10 минут

threading.Thread(target=_report_worker, daemon=True).start()
# ========================================================

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
    """IP клиента (за прокси его подставляет ProxyFix из доверенной записи X-Forwarded-For)"""
    return request.remote_addr or '?'

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
    # Анти-спам: лимит стартов с одного IP
    if not database.rate_allow("start", get_client_ip(), SESSION_START_LIMIT, SESSION_START_WINDOW):
        return jsonify({"status": "error", "message": "Слишком много попыток. Попробуйте позже."}), 429

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

    # Часы вопроса НЕ перезапускаем: иначе перезагрузка страницы давала бы полный
    # таймер заново и обнуляла замер для флага "затянутого" ответа.
    # Клиенту отдаем остаток времени (минимум пара секунд, чтобы успеть отрисовать).
    q = data["questions"][idx]
    elapsed = time.time() - data.get("shown_at", time.time())
    item = client_question(q)
    item["time_left"] = max(int(q["time"] - elapsed), RESYNC_MIN_SECONDS)
    return jsonify({
        "status": "success",
        "total": len(data["questions"]),
        "question": item
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
    # bool — подкласс int в Python: true в JSON иначе прошел бы как позиция 1
    if not isinstance(pos, int) or isinstance(pos, bool) or pos != data["current"]:
        return jsonify({"status": "error", "message": "Несинхронизированный вопрос"}), 409

    answer = payload.get("answer", "")
    if not isinstance(answer, str) or len(answer) > 200:
        return jsonify({"status": "error", "message": "Некорректный ответ"}), 400

    q = data["questions"][pos - 1]
    elapsed = time.time() - data.get("shown_at", time.time())

    # Анализ скорости: слишком быстро — подсказка/угадывание, слишком долго — подглядывание
    if answer.strip() and elapsed < test_session.MIN_ANSWER_SECONDS[q["type"]]:
        data["too_fast"] += 1
    elif answer.strip() and elapsed > q["time"] * 2.5:
        data["too_slow"] += 1

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

    client_ip = get_client_ip()
    now_ts = time.time()

    # Анти-спам: не более SUBMIT_LIMIT успешных отправок с IP за окно
    # (засчитываем ниже, только когда результат сохранен)
    if not database.rate_allow("submit", client_ip, SUBMIT_LIMIT, SUBMIT_WINDOW, record=False):
        return jsonify({"status": "error", "message": "Слишком много отправок. Попробуйте позже."}), 429

    raw_fio = str(payload.get('fio', '')).strip()
    raw_phone = str(payload.get('phone', '')).strip()
    raw_branch = str(payload.get('branch', '')).strip()
    raw_work_study = str(payload.get('work_study', '')).strip()
    raw_phone2 = str(payload.get('phone2', '')).strip()
    raw_source = str(payload.get('source', '')).strip()

    # Серверная валидация ФИО
    if not raw_fio or len(raw_fio) < 3:
        return jsonify({"status": "error", "message": "Введите корректное ФИО"}), 400

    # Проверка формата основного телефона (+998XXXXXXXXX)
    clean_digits = re.sub(r'\D', '', raw_phone)
    if len(clean_digits) != 12 or not clean_digits.startswith('998'):
        return jsonify({"status": "error", "message": "Некорректный номер телефона"}), 400

    # Второй номер (опциональный)
    phone2 = None
    if raw_phone2:
        clean_p2 = re.sub(r'\D', '', raw_phone2)
        if len(clean_p2) == 12 and clean_p2.startswith('998'):
            phone2 = "+" + clean_p2
        elif len(clean_p2) == 9:
            phone2 = "+998" + clean_p2

    work_study = raw_work_study[:100] if raw_work_study else None
    source = raw_source[:50] if raw_source else None

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
    too_slow = session_data.get("too_slow", 0)
    fast_total = total_elapsed < MIN_TOTAL_SECONDS
    flags = too_fast + (1 if fast_total else 0) + (1 if too_slow >= 3 else 0)
    timing_json = json.dumps({
        "total_sec": int(total_elapsed),
        "per_question": session_data.get("seconds", {}),
        "too_fast": too_fast,
        "too_slow": too_slow,
        "fast_total": fast_total
    }, ensure_ascii=False)

    # Атомарная метка отправки: гонка параллельных запросов не создаст дубликат
    if not database.mark_session_submitted(sid):
        return jsonify({"status": "error", "message": "Результат уже отправлен"}), 400

    # Персистентное сохранение в базу данных SQLite (защита от потери заявки).
    # questions_json — снимок вопросов сессии: по нему бланк можно пересобрать
    # и дослать в Telegram, если отправка в момент сдачи не удалась.
    questions_snapshot = json.dumps(session_data["questions"], ensure_ascii=False)
    try:
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
            timing_json=timing_json,
            work_study=work_study,
            phone2=phone2,
            source=source,
            questions_json=questions_snapshot
        )
    except Exception:
        # Сохранение не удалось — возвращаем студенту возможность повторить
        database.unmark_session_submitted(sid)
        raise

    # Отправка принята — отмечаем IP для анти-спама
    database.rate_record("submit", client_ip)

    res_data = {
        'id': result_id,
        'fio': fio,
        'phone': phone,
        'phone2': phone2,
        'work_study': work_study,
        'source': source,
        'branch': branch,
        'date': now,
        'score': score,
        'total': total,
        'level': level,
        'time_spent': time_spent,
        'answers': session_data["answers"]
    }
    speed = speed_line(flags, too_fast, too_slow, total_elapsed)
    message = build_result_message(res_data, speed)
    photo_bytes = build_result_sheet(res_data, session_data["questions"])

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
        limit = max(1, min(int(request.args.get('limit', 100)), 500))
    except ValueError:
        limit = 100

    return jsonify({"status": "success", "results": database.get_recent_results(limit)})

@app.route('/admin/report')
def admin_report():
    """Ручная отправка отчета в Telegram: ?period=2026-10 (месяц) или ?period=2026 (год).
    Без period — за предыдущий месяц. Требует ADMIN_TOKEN."""
    if not ADMIN_TOKEN:
        return jsonify({"status": "error", "message": "Не найдено"}), 404

    token = request.headers.get('X-Admin-Token', '') or request.args.get('key', '')
    if not secrets.compare_digest(token, ADMIN_TOKEN):
        return jsonify({"status": "error", "message": "Доступ запрещен"}), 403

    period = request.args.get('period', '').strip()
    now = datetime.now(reports.TZ_TASHKENT)
    try:
        if re.fullmatch(r'\d{4}', period):
            year, month = int(period), None
        elif re.fullmatch(r'\d{4}-\d{2}', period):
            year, month = int(period[:4]), int(period[5:7])
            if not 1 <= month <= 12:
                raise ValueError
        else:
            prev = _prev_month(now)
            year, month = prev.year, prev.month
    except ValueError:
        return jsonify({"status": "error", "message": "Формат period: ГГГГ или ГГГГ-ММ"}), 400

    if not BOT_TOKEN or not get_chat_ids():
        return jsonify({"status": "error", "sent": False,
                        "message": "Telegram не настроен (BOT_TOKEN / CHAT_ID)",
                        "preview": reports.build_report(year, month)})

    text = reports.build_report(year, month)
    sent = send_to_telegram_sync(text)
    return jsonify({"status": "success" if sent else "error", "sent": sent, "preview": text})

@app.errorhandler(400)
def bad_request(e):
    return jsonify({"status": "error", "message": "Некорректный формат запроса"}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"status": "error", "message": "Не найдено"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"status": "error", "message": "Метод не поддерживается"}), 405

@app.errorhandler(413)
def request_too_large(e):
    return jsonify({"status": "error", "message": "Слишком большой запрос"}), 413

@app.errorhandler(500)
def internal_error(e):
    """Ошибки сервера отдаются клиенту в JSON — фронтенд ожидает JSON-ответ"""
    return jsonify({"status": "error", "message": "Внутренняя ошибка сервера. Попробуйте позже."}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
