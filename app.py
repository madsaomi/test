import threading
from flask import Flask, render_template, request, jsonify
import json
import requests
from datetime import datetime
import html
import re

import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# --- НАСТРОЙКИ TELEGRAM (Загружаются из .env или панели хостинга) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# Кэшируем вопросы в памяти при старте (не читаем диск при каждом запросе)
with open('questions.json', 'r', encoding='utf-8') as f:
    ALL_QUESTIONS = json.load(f)

# Готовим очищенные вопросы для фронтенда заранее
CLIENT_QUESTIONS = []
for q in ALL_QUESTIONS:
    item = {
        "id": q.get("id"),
        "type": q.get("type"),
        "question": q.get("question"),
    }
    if q.get("type") == "choice":
        item["options"] = q.get("options", [])
    CLIENT_QUESTIONS.append(item)

def get_chat_ids():
    """Возвращает список всех ID получателей"""
    global CHAT_ID
    
    # Если ID уже указаны в .env — возвращаем список
    if CHAT_ID:
        return [cid.strip() for cid in CHAT_ID.split(",") if cid.strip()]
    
    # Иначе пробуем получить из последнего сообщения боту
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", timeout=5).json()
        if r.get("result"):
            auto_id = str(r["result"][-1]["message"]["chat"]["id"])
            CHAT_ID = auto_id
            return [auto_id]
    except Exception as e:
        print(f"Ошибка получения chat_id: {e}")
    return []

def _send_tg_worker(message):
    """Фоновый поток: рассылает сообщение всем получателям"""
    chat_ids = get_chat_ids()
    if not chat_ids:
        print("WARNING: CHAT_ID не найден. Напишите боту в Telegram или укажите ID в .env")
        return
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for cid in chat_ids:
        try:
            requests.post(url, json={
                "chat_id": cid,
                "text": message,
                "parse_mode": "HTML"
            }, timeout=10)
        except Exception as e:
            print(f"Ошибка отправки в чат {cid}: {e}")

def send_to_telegram_async(message):
    """Мгновенный запуск отправки в отдельном фоновом потоке"""
    thread = threading.Thread(target=_send_tg_worker, args=(message,), daemon=True)
    thread.start()

@app.route('/')
def index():
    # Отдаем заранее подготовленный кэш (0 миллисекунд нагрузки на диск)
    return render_template('index.html', questions=CLIENT_QUESTIONS)

import re

@app.route('/submit', methods=['POST'])
def submit():
    data = request.json or {}
    
    raw_fio = str(data.get('fio', '')).strip()
    raw_phone = str(data.get('phone', '')).strip()
    raw_branch = str(data.get('branch', '')).strip()
    
    # Серверная валидация
    if not raw_fio or len(raw_fio) < 3:
        return jsonify({"status": "error", "message": "Введите корректное ФИО"}), 400
        
    # Проверка формата телефона (+998XXXXXXXXX)
    clean_digits = re.sub(r'\D', '', raw_phone)
    if len(clean_digits) != 12 or not clean_digits.startswith('998'):
        return jsonify({"status": "error", "message": "Некорректный номер телефона"}), 400
    
    fio = html.escape(raw_fio[:100])
    phone = "+" + clean_digits
    branch = html.escape(raw_branch[:50])
    time_spent = html.escape(str(data.get('time', 'Не указано')).strip()[:30])
    now = datetime.now().strftime('%d.%m.%Y  %H:%M')
    
    # Безопасный подсчет баллов на стороне сервера с нормализацией текста
    answers_map = data.get('answers', {})
    
    score = 0
    total = len(ALL_QUESTIONS)
    
    def normalize_text(text):
        # Заменяем фигурные апострофы на стандартные
        t = str(text).replace('’', "'").replace('`', "'")
        # Удаляем точки/запятые на концах, переводим в нижний регистр и сжимаем пробелы
        t = re.sub(r'[^\w\s\']', '', t.lower())
        return " ".join(t.split())
    
    for q in ALL_QUESTIONS:
        q_id = str(q.get('id'))
        user_answer = normalize_text(answers_map.get(q_id, ''))
        correct_answer = normalize_text(q.get('answer', ''))
        if user_answer and user_answer == correct_answer:
            score += 1
    
    # Определяем уровень по баллам
    if score <= 15:
        level = "Beginner"
    elif score <= 24:
        level = "Elementary"
    elif score <= 32:
        level = "Pre-Intermediate"
    elif score <= 39:
        level = "Intermediate"
    elif score <= 45:
        level = "Upper-Intermediate"
    else:
        level = "Upper-Intermediate+"
    
    # Формируем сообщение
    message = (
        f"📝 <b>Новый результат теста!</b>\n\n"
        f"👤 <b>ФИО:</b> {fio}\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"🏢 <b>Филиал:</b> {branch}\n\n"
        f"📊 <b>Результат:</b> {score} из {total}\n"
        f"🎓 <b>Уровень:</b> {level}\n"
        f"⏱ <b>Время:</b> {time_spent}\n"
        f"📅 <b>Дата:</b> {now}"
    )
    
    # Асинхронная отправка без блокировки пользователя
    send_to_telegram_async(message)
    
    return jsonify({
        "status": "success", 
        "message": "Результаты успешно отправлены",
        "score": score,
        "total": total,
        "level": level
    })

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
