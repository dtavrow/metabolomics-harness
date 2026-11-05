ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG MSPRED_COMMIT=708148c
ARG MSPRED_EXTRA=cpu
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential libxrender1 libxext6 libsm6 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.9.7
RUN git clone https://github.com/coleygroup/ms-pred.git /opt/ms-pred \
    && git -C /opt/ms-pred checkout ${MSPRED_COMMIT} \
    && cd /opt/ms-pred \
    && uv sync --no-dev --extra ${MSPRED_EXTRA}
ENV PATH=/opt/ms-pred/.venv/bin:${PATH}
COPY models/common/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY models/common/ /app/
COPY models/glacier/main.py /app/main.py
COPY models/glacier/default_backend.py /app/default_backend.py
COPY models/glacier/preprocess.py /app/preprocess.py
RUN python /app/default_weights.py --model glacier
ENV PYTHONPATH=/app:/opt/ms-pred/src
WORKDIR /app
ENTRYPOINT ["python", "/app/main.py"]
