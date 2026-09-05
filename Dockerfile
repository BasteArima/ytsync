FROM python:3.12-alpine

# ffmpeg — склейка видео+аудио.
# deno — JS-движок: YouTube требует решать "n challenge", иначе yt-dlp видит
#        только раскадровки и падает с "Requested format is not available".
#        У yt-dlp Deno включён по умолчанию, дополнительных флагов не нужно.
RUN apk add --no-cache ffmpeg tzdata deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py core.py extras.py ./
COPY templates ./templates
# Шрифты лежат внутри образа: домашнему сервису незачем ходить за ними
# в интернет при каждом открытии страницы, да и без сети всё должно работать
COPY static ./static

# Контейнер работает под 1005:100, домашний каталог ему недоступен —
# кэши Deno и yt-dlp уводим в /tmp, иначе падает на правах.
ENV YTSYNC_CONFIG=/config/config.json \
    YTSYNC_DB=/config/ytsync.db \
    YTSYNC_DOWNLOAD_ROOT=/media \
    YTSYNC_COOKIES=/config/cookies.txt \
    PYTHONUNBUFFERED=1 \
    DENO_DIR=/tmp/deno \
    XDG_CACHE_HOME=/tmp/cache

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD wget -qO- http://127.0.0.1:8099/healthz || exit 1

CMD ["waitress-serve", "--host=0.0.0.0", "--port=8099", "app:app"]
