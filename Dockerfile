FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SURVEY_DB=/data/survey.sqlite3

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY examples ./examples

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

WORKDIR /srv/app
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--access-logfile", "-", "main:app"]
