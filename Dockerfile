# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md requirements.txt ./
COPY src ./src
COPY prompts ./prompts

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/.data && chown -R app:app /app

ENV PYTHONUNBUFFERED=1 \
    PROMPT_DIR=/app/prompts

USER app

ENTRYPOINT ["ai-creative-pipeline"]
CMD ["--help"]
