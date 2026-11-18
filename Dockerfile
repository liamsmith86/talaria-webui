FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock hatch_build.py README.md LICENSE /app/
COPY src /app/src
RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
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
