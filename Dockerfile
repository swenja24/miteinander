FROM python:3.13-alpine
LABEL org.opencontainers.image.title="Miteinander" \
      org.opencontainers.image.description="Eingliederungshilfe gemeinsam und übersichtlich organisieren" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY server.py ./
COPY public ./public
RUN mkdir -p /app/data && chown -R nobody:nogroup /app
USER nobody
ENV NODE_ENV=production PORT=3000 DATA_DIR=/app/data
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q -O - http://127.0.0.1:3000/api/health || exit 1
CMD ["python", "server.py"]
