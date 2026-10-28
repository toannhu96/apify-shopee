FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements-actor.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 actor
COPY shopee_actor ./shopee_actor
COPY shopee_contracts.py ./shopee_contracts.py
RUN mkdir -p /app/storage && chown -R actor:actor /app
USER actor
CMD ["python", "-m", "shopee_actor"]
