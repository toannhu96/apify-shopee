FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements-service.txt ./
RUN pip install --no-cache-dir -r requirements-service.txt
COPY shopee_affiliate/ ./shopee_affiliate/
COPY shopee_service/ ./shopee_service/
COPY shopee_contracts.py ./
USER 10001:10001
CMD ["uvicorn", "shopee_service.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
