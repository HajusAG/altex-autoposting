FROM python:3.11-slim

WORKDIR /app

COPY script.py /app/script.py
COPY _allowed_categories.json /app/_allowed_categories.json
COPY _allowed_map.json /app/_allowed_map.json
COPY _altex_sets.json /app/_altex_sets.json

ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/script.py"]
