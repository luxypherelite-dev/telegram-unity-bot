FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BOT_DATA_DIR=/var/lib/telegram-unity-bot

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /var/lib/telegram-unity-bot \
    && chown -R botuser:botuser /app /var/lib/telegram-unity-bot
USER botuser

CMD ["python", "bot.py"]
