FROM python:3.12-slim

WORKDIR /srv

# psycopg[binary] and pymupdf ship manylinux wheels — no system libs needed.
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY app ./app
COPY db ./db
COPY scripts ./scripts

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
