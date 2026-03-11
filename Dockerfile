FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY coupang_cart_agent ./coupang_cart_agent
COPY main.py ./
COPY tests ./tests

RUN uv sync --frozen

EXPOSE 8080

CMD ["uv", "run", "python", "-m", "coupang_cart_agent", "serve-http", "--host", "0.0.0.0", "--port", "8080"]
