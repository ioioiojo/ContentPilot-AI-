FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp
WORKDIR /app

RUN groupadd --system contentpilot && useradd --system --gid contentpilot --home-dir /nonexistent contentpilot
COPY pyproject.toml README.md requirements.lock ./
COPY contentpilot_agent ./contentpilot_agent
RUN pip install --no-cache-dir -c requirements.lock .
RUN mkdir -p /app/secure && chown contentpilot:contentpilot /app/secure && chmod 700 /app/secure

USER contentpilot
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=3s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["uvicorn", "contentpilot_agent.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
