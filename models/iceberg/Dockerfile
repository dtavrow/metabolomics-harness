ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG MSPRED_COMMIT=708148c
ARG MSPRED_EXTRA=cpu
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential libxrender1 libxext6 libsm6 curl bzip2 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.9.7
COPY models/common/patch_mspred_cu118.py /tmp/patch_mspred_cu118.py
RUN git clone https://github.com/coleygroup/ms-pred.git /opt/ms-pred \
    && git -C /opt/ms-pred checkout ${MSPRED_COMMIT} \
    && cd /opt/ms-pred \
    && if [ "${MSPRED_EXTRA}" = "cu118" ]; then \
         # See patch_mspred_cu118.py: data.dgl.ai is down (expired domain,
         # no ETA), so pin torch/torchvision/pyg down to what the newest
         # conda-distributed dgl build (dglteam/label/cu118) still
         # supports and resolve+install everything else (minus dgl
         # itself) normally. The next RUN layer splices dgl's binary in
         # by hand -- kept separate so iterating on that step doesn't
         # force re-resolving this one's large dependency set.
         python3 /tmp/patch_mspred_cu118.py pyproject.toml \
         && uv sync --no-dev --extra cu118 --no-install-package dgl; \
       else \
         uv sync --no-dev --extra ${MSPRED_EXTRA}; \
       fi
RUN if [ "${MSPRED_EXTRA}" = "cu118" ]; then \
      cd /opt/ms-pred \
      && curl -fsSL -o /tmp/dgl.tar.bz2 \
           "https://conda.anaconda.org/dglteam/label/cu118/linux-64/dgl-2.1.0.cu118-py311_0.tar.bz2" \
      && mkdir -p /tmp/dgl_extracted \
      && tar -xjf /tmp/dgl.tar.bz2 -C /tmp/dgl_extracted \
      && cp -r /tmp/dgl_extracted/lib/python3.11/site-packages/dgl \
           .venv/lib/python3.11/site-packages/dgl \
      && uv pip install --python .venv/bin/python networkx psutil \
      && rm -rf /tmp/dgl.tar.bz2 /tmp/dgl_extracted; \
    fi
ENV PATH=/opt/ms-pred/.venv/bin:${PATH}
# torch's pip-bundled CUDA libs (nvidia-cublas-cu11 etc.) live inside the
# venv's site-packages, not a system library path -- dgl's graphbolt C++
# extension dlopen()s them by bare name and needs this to find them.
ENV LD_LIBRARY_PATH=/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/cublas/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/cuda_cupti/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/cuda_nvrtc/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/cuda_runtime/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/cudnn/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/cufft/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/curand/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/cusolver/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/cusparse/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/nccl/lib:/opt/ms-pred/.venv/lib/python3.11/site-packages/nvidia/nvtx/lib:${LD_LIBRARY_PATH}
COPY models/common/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
# requirements.txt pins pyarrow==26.0.0, which requires numpy>=2 -- conflicts
# with the numpy<2 patch_mspred_cu118.py applies for dgl/torch-2.2.1
# compatibility. Re-pin to a numpy-1.x-era pyarrow for the cu118 build only.
RUN if [ "${MSPRED_EXTRA}" = "cu118" ]; then \
      uv pip install --python /opt/ms-pred/.venv/bin/python "numpy<2" "pyarrow==14.0.2"; \
    fi
COPY models/common/ /app/
COPY models/iceberg/main.py /app/main.py
COPY models/iceberg/default_backend.py /app/default_backend.py
COPY models/iceberg/preprocess.py /app/preprocess.py
# The iceberg checkpoint's Dropbox-hosted archive has drifted from the
# pinned SHA-256 in default_weights.py as of 2026-11-09 (the small
# hparams.yaml files still match; the actual best.ckpt weights don't) --
# an upstream/unrelated problem, confirmed by the fact mwannotate-iceberg:cpu
# built earlier has the correct, pin-matching weights already on disk. Reuse
# those instead of re-downloading from the now-drifted source.
COPY --from=mwannotate-iceberg:cpu /opt/model-bundles/iceberg /opt/model-bundles/iceberg
ENV PYTHONPATH=/app:/opt/ms-pred/src
WORKDIR /app
ENTRYPOINT ["python", "/app/main.py"]
