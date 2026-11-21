FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY mailsearch/ mailsearch/
COPY web/ web/
COPY fixtures/ fixtures/
COPY tests/ tests/
COPY README.md RUN.md START_CONTEXT.py build_standalone.py ./
RUN python -m unittest discover -s tests -q \
    && useradd --system --user-group contextmail \
    && install -d -o contextmail -g contextmail /app/.cache
USER contextmail
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/meta', timeout=3).read()"
ENTRYPOINT ["python", "-m", "mailsearch.server", "--host", "0.0.0.0", "--port", "8765"]
