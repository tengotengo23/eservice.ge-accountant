FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Chromium საერთო დირექტორიაში: `playwright install` root-ით ეშვება, აპლიკაცია კი appuser-ით.
    # ნაგულისხმევად ბრაუზერი /root/.cache-ში იდებოდა და appuser-ისთვის "Executable doesn't exist" იყო.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /srv

# დამოკიდებულებები + Chromium ცალკე ფენად: კოდის ცვლილებაზე (~ყოველ commit-ზე) თავიდან არ იტვირთება
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install . \
    && playwright install --with-deps chromium \
    && rm -rf app build ./*.egg-info

COPY app ./app
RUN pip install --no-deps . && rm -rf build ./*.egg-info

# root-ის გარეშე
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request as u; sys.exit(u.urlopen('http://127.0.0.1:8000/health', timeout=4).status != 200)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
