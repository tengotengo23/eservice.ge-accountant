# ARG: base image-ის ჩანაცვლება (digest-ით დაპინვა, შიდა mirror) build-ის შეცვლის გარეშე
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Playwright ნაგულისხმევად ბრაუზერს $HOME/.cache-ში აყენებს: root-ით დაყენებულს appuser ვეღარ
    # იპოვიდა. საერთო, ყველასთვის წაკითხვადი მისამართი:
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /srv

# 1) დამოკიდებულებები და Chromium - ცალკე layer-ებად: კოდის ცვლილება მათ ხელახლა არ აყენებს
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install . && pip uninstall -y ai-accountant \
    && rm -rf app build *.egg-info
RUN playwright install --with-deps chromium && chmod -R a+rX /ms-playwright

# 2) აპლიკაციის კოდი
COPY app ./app
RUN pip install --no-deps . && rm -rf build *.egg-info

# root-ის გარეშე
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
# reverse proxy-ის უკან: FORWARDED_ALLOW_IPS=<proxy IP> (uvicorn), რომ აუდიტში კლიენტის რეალური IP ჩაიწეროს
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
