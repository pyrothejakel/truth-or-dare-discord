FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
COPY tests ./tests
RUN python -m unittest discover -s tests -v
RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data && chown bot:bot /app/data
USER bot
CMD ["truth-or-dare-bot"]
