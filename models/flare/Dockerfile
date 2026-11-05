ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG FLARE_COMMIT=991d5ce
ARG TORCH_VERSION=2.4.1
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN apt-get update && apt-get install -y --no-install-recommends git libxrender1 libxext6 libsm6 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch==${TORCH_VERSION} --index-url ${TORCH_INDEX_URL}
RUN GIT_LFS_SKIP_SMUDGE=1 git clone https://huggingface.co/spaces/HassounLab/FLARE /opt/flare \
    && git -C /opt/flare checkout ${FLARE_COMMIT} \
    && pip install --no-cache-dir -r /opt/flare/requirements.txt
COPY models/common/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY models/common/ /app/
COPY models/flare/main.py /app/main.py
COPY models/flare/default_backend.py /app/default_backend.py
COPY models/flare/preprocess.py /app/preprocess.py
RUN python /app/default_weights.py --model flare
ENV PYTHONPATH=/app:/opt/flare
WORKDIR /app
ENTRYPOINT ["python", "/app/main.py"]
