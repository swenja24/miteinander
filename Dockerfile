FROM python:3.13-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.13-slim
LABEL org.opencontainers.image.title="Miteinander" \
      org.opencontainers.image.description="Eingliederungshilfe gemeinsam und übersichtlich organisieren" \
      org.opencontainers.image.licenses="MIT"
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    MEDIA_ROOT=/app/data/media
WORKDIR /app
RUN mkdir -p /app/data /app/staticfiles && chown -R nobody:nogroup /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=nobody:nogroup manage.py ./
COPY --chown=nobody:nogroup config ./config
COPY --chown=nobody:nogroup core ./core
COPY --chown=nobody:nogroup public ./public
COPY --chown=nobody:nogroup docker ./docker
RUN chmod +x /app/docker/entrypoint.sh && \
    DJANGO_SECRET_KEY=container-build-only SECURE_SSL_REDIRECT=false COOKIE_SECURE=false python manage.py collectstatic --noinput
USER nobody
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; host=os.environ.get('ALLOWED_HOSTS','localhost').split(',')[0].strip() or 'localhost'; request=urllib.request.Request('http://127.0.0.1:8000/api/health', headers={'Host':host,'X-Forwarded-Proto':'https'}); urllib.request.urlopen(request, timeout=3)" || exit 1
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "2", "--timeout", "60", "--error-logfile", "-"]
