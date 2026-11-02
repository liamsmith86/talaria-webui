FROM python:3.15.0-slim@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58 AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock hatch_build.py README.md LICENSE /app/
COPY src /app/src
RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.15.0-slim@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 XDG_CONFIG_HOME=/data TALARIA_CONTAINER=1
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
RUN useradd --create-home talaria && mkdir /data && chown talaria /data
USER talaria
VOLUME ["/data"]
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=3s CMD ["talaria", "healthcheck"]
ENTRYPOINT ["talaria"]
CMD ["--host", "0.0.0.0"]
