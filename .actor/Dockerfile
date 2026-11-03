FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8 AS actor
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements-actor.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check \
    && groupadd --gid 10001 actor \
    && useradd --uid 10001 --gid actor --create-home --no-log-init actor \
    && install -d -o actor -g actor -m 0750 /app/storage
# Only storage and the user's home are writable, never the application source.
COPY shopee_actor ./shopee_actor
COPY shopee_contracts.py ./shopee_contracts.py
USER 10001:10001
CMD ["python", "-m", "shopee_actor"]
