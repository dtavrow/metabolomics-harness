FROM python:3.11-slim

COPY models/common/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY models/common/ /app/
COPY models/evidence/main.py /app/main.py
ENV PYTHONPATH=/app
WORKDIR /app
ENTRYPOINT ["python", "/app/main.py"]

