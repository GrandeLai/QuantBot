FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install ".[data,alpaca]"
CMD ["quantbot", "demo"]
