FROM python:3.11-slim

# Отключаем буферизацию вывода Python (чтобы логи сразу отображались в Railway)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5000

WORKDIR /app

# Сначала копируем только зависимости для кэширования слоев сборки
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Шрифты DejaVu (кириллица + серифный заголовок бланка)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Копируем остальные файлы проекта
COPY . .

# Открываем порт
EXPOSE 5000

# Запуск приложения через gunicorn
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --threads 4 app:app"]
