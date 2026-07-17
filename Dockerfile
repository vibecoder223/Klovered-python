FROM python:3.12-slim

WORKDIR /srv

# psycopg[binary] and pymupdf ship manylinux wheels — no system libs needed.
# The source must be present before the install: pyproject declares `app` as
# the package, so `pip install .` needs app/ to exist to build it.
COPY pyproject.toml ./
COPY app ./app
COPY db ./db
COPY scripts ./scripts
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
