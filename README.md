# Stanford English Test

Онлайн-тестирование по английскому языку для языкового центра Stanford.

## Стек технологий
* **Python 3 / Flask** — бэкенд и обработка результатов
* **HTML5 / CSS3 / JavaScript** — адаптивный интерфейс тестирования
* **Telegram Bot API** — мгновенные уведомления администраторам

## Локальный запуск
1. Склонируйте репозиторий:
   ```bash
   git clone <URL_РЕПОЗИТОРИЯ>
   cd <ПАПКА_ПРОЕКТА>
   ```
2. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
3. Скопируйте `.env.example` в `.env` и укажите данные бота:
   ```bash
   cp .env.example .env
   ```
4. Запустите сервер:
   ```bash
   python app.py
   ```
   Сайт будет доступен по адресу `http://127.0.0.1:5000`.

## Деплой на бесплатный хостинг (Render.com)
1. Создайте **New Web Service** на Render и подключите репозиторий.
2. Параметры сборки:
   * **Build Command:** `pip install -r requirements.txt`
   * **Start Command:** `gunicorn app:app`
3. В разделе **Environment Variables** добавьте:
   * `BOT_TOKEN` — токен от @BotFather
   * `CHAT_ID` — ID чата/группы для получения результатов
