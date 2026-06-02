# Многоступенчатая сборка: колёса ставятся в builder, в финальный образ
# компилятор и заголовки не попадают — меньше размер и меньше поверхность атаки.

FROM python:3.13-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.13-slim

# Приложение работает не под root: если процесс скомпрометируют, у него не
# будет прав на систему внутри контейнера.
RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --from=builder /install /usr/local

COPY --chown=appuser:appuser app.py assistant.py db.py config.py retriever.py sql_guard.py ./
COPY --chown=appuser:appuser examples/ ./examples/
COPY --chown=appuser:appuser eval/ ./eval/
COPY --chown=appuser:appuser sql/ ./sql/

# Корневой сертификат «Russian Trusted Root CA» для проверки TLS у GigaChat.
# Положите файл в certs/russian_trusted_root_ca.cer и раскомментируйте:
# COPY certs/ /usr/local/share/ca-certificates/
# RUN update-ca-certificates
# После этого можно ставить GIGACHAT_VERIFY_SSL=true.

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
