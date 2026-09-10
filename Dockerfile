FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock /app/
COPY src /app/src
RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 XDG_CONFIG_HOME=/data
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
RUN useradd --create-home talaria && mkdir /data && chown talaria /data
USER talaria
VOLUME ["/data"]
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=2)"
ENTRYPOINT ["talaria"]
CMD ["--host", "0.0.0.0"]
