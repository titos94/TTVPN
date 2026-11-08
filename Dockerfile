# syntax=docker/dockerfile:1

FROM python:3.14-slim

WORKDIR /app

# Копируем requirements.txt и устанавливаем зависимости
COPY requirements.txt requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# В образ попадает только код и необходимые ресурсы, а не локальные данные,
# ключи, резервные копии или конфигурация разработчика.
COPY *.py ./
COPY billing/ ./billing/
COPY managers/ ./managers/
COPY protocol_telemt/ ./protocol_telemt/
COPY static/ ./static/
COPY templates/ ./templates/
COPY translations/ ./translations/
COPY scripts/panel_backup.py scripts/panel_recovery.py ./scripts/
COPY scripts/rknblock/ ./scripts/rknblock/


# Команда запуска приложения
CMD ["python3", "app.py"]
