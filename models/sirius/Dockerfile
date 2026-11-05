FROM continuumio/miniconda3:25.3.1-1

ARG SIRIUS_VERSION=6.5.4
ARG SIRIUS_ARCHIVE_SHA256=9556237eb8306f27dede2b7e3404952e02729a052b87fe942857bd26d20c9d63
RUN conda install --yes python=3.11 pip \
    && conda clean --all --yes
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl unzip \
    && curl --fail --silent --show-error --location \
       "https://github.com/sirius-ms/sirius/releases/download/v${SIRIUS_VERSION}/sirius-${SIRIUS_VERSION}-linux-x64.zip" \
       --output /tmp/sirius.zip \
    && echo "${SIRIUS_ARCHIVE_SHA256}  /tmp/sirius.zip" | sha256sum --check --strict \
    && unzip -q /tmp/sirius.zip -d /opt \
    && test "$(/opt/sirius/bin/sirius --version 2>&1 | head -n 1)" \
    && chmod +x /opt/sirius/bin/sirius \
    && ln -s /opt/sirius/bin/sirius /usr/local/bin/sirius \
    && rm -f /tmp/sirius.zip \
    && apt-get purge --yes curl unzip \
    && apt-get autoremove --yes \
    && rm -rf /var/lib/apt/lists/*
RUN rm -rf /opt/conda/aot
ENV CONDA_PREFIX=/opt/conda
ENV PATH="/opt/sirius/bin:${PATH}"
COPY models/common/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY models/common/ /app/
COPY models/sirius/main.py /app/main.py
COPY models/sirius/preprocess.py /app/preprocess.py
ENV PYTHONPATH=/app
WORKDIR /app
ENTRYPOINT ["python", "/app/main.py"]
