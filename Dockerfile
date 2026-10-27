FROM python:3.12-slim

# Unbuffered output so `docker compose logs -f` shows lines immediately.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first: this layer is cached unless requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY rss2tg.py ./

# Non-root runtime user, overridable with `user:` in compose. The config file
# and the SQLite dedupe database both live in /data, so /data is the only path
# that needs to be writable at runtime.
RUN useradd --uid 10001 --user-group --no-create-home \
        --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data /app \
    && chmod 0755 /app \
    && chmod 0644 /app/rss2tg.py /app/requirements.txt
# The chmod is required, not cosmetic: COPY preserves the source file modes, and
# a build context on a shared folder (vboxsf, some SMB/NFS mounts) delivers
# 0770 root:root. Without it, any non-root `user:` in compose fails with
# "python: can't open file '/app/rss2tg.py': [Errno 13] Permission denied".

# No config file is baked into the image. At startup the app looks for
# /data/config.yml, then /data/config.yaml, then /app/config.yml, and honours
# $CONFIG_PATH if set. That is why compose needs no config mount or argument.
VOLUME ["/data"]

# Validates the config and opens the SQLite file. No network calls, so it is
# safe to run on a schedule. Fails loudly if /data has no config yet.
HEALTHCHECK --interval=5m --timeout=15s --start-period=20s --retries=3 \
    CMD ["python", "/app/rss2tg.py", "--check"]

USER app

ENTRYPOINT ["python", "/app/rss2tg.py"]
