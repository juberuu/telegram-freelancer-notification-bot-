FROM python:3.12-slim
WORKDIR /app
COPY projectbot ./projectbot
COPY config.example.json ./config.example.json
RUN useradd --uid 10001 --create-home bot && mkdir /app/data && chown bot:bot /app/data
USER bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "projectbot.app"]
