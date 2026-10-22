FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config \
    PORT=8484 \
    PUID=99 \
    PGID=100 \
    UMASK=002

# bash, curl, jq, iptables, iproute2: braucht das Tailscale-Startskript von Unraid ("Use Tailscale")
RUN apk add --no-cache su-exec tzdata bash curl ca-certificates jq iptables ip6tables iproute2

LABEL net.unraid.docker.icon="https://raw.githubusercontent.com/2pac1991/mediasort/main/unraid/icon.png" \
      net.unraid.docker.webui="http://[IP]:[PORT:8484]/"

# Chromium und Schriften: eingebauter Browser zum Lösen von Captchas mit Click'n'Load
# Xvfb: virtueller Bildschirm, damit Chromium als normaler Browser läuft statt unsichtbar
RUN apk add --no-cache chromium font-noto xvfb \
 && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# cryptography: Verschlüsselung für die My.JDownloader-API; websocket-client: Steuerung von Chromium
RUN pip install --no-cache-dir cryptography==44.0.0 websocket-client==1.8.0

WORKDIR /opt/mediasort
COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8484
VOLUME ["/config"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD wget -qO- http://127.0.0.1:${PORT}/api/health >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
