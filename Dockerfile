FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config \
    PORT=8484 \
    PUID=99 \
    PGID=100 \
    UMASK=002

RUN apk add --no-cache su-exec tzdata

WORKDIR /opt/mediasort
COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8484
VOLUME ["/config"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD wget -qO- http://127.0.0.1:${PORT}/api/status >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
