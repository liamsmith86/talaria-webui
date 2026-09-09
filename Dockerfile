FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 XDG_CONFIG_HOME=/data
WORKDIR /app
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install --no-cache-dir . && useradd --create-home talaria && mkdir /data && chown talaria /data
USER talaria
VOLUME ["/data"]
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=2)"
ENTRYPOINT ["talaria"]
CMD ["--host", "0.0.0.0"]
