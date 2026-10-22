# One image for both the service and the mock; docker-compose picks the command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY mock_dependency ./mock_dependency

RUN useradd --create-home --uid 10001 appuser && mkdir -p /srv/data && chown appuser /srv/data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1
CMD ["uvicorn", "app.api:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
