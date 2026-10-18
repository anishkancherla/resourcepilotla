FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml README.md LICENSE ./
COPY resourcepilot resourcepilot
COPY agents agents
RUN pip install --no-cache-dir .

USER app
CMD ["python", "-m", "resourcepilot.container_entrypoint"]
