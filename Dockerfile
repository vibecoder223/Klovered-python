FROM python:3.12-slim

WORKDIR /srv

# System libs PyMuPDF needs at runtime are bundled in the wheel; keep image slim.
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
