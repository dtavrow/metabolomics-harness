# syntax=docker/dockerfile:1
#
# MWExtract -- Metabolomics Workbench MS/MS extractor.
#
# The corpus index baked in at /modelfiles is a SNAPSHOT: studies deposited
# after the audit date below are invisible to it.  That is deliberate -- a run
# must be reproducible against a fixed corpus -- but it means the audit date is
# part of the image's identity, not metadata.  Tag accordingly.

FROM continuumio/miniconda3

LABEL org.opencontainers.image.title="MWExtract"
LABEL org.opencontainers.image.description="Fetch the MS/MS payload of one Metabolomics Workbench study"
LABEL io.metabolomics.corpus-audit-date="2026-09-26"
LABEL io.metabolomics.corpus-files="20764"
LABEL io.metabolomics.corpus-ms2-spectra="175700331"
LABEL io.metabolomics.corpus-studies="347"

SHELL ["/bin/bash", "-lc"]

# pyarrow is not optional: the corpus index is Parquet, and without it the
# image cannot read its own baked data.
#
# mwtab is deliberately NOT installed.  It was used for a single URL
# constructor; it must be installed --no-deps (its pins conflict with the
# solved environment) and then imports jsonschema at module load anyway, so a
# --no-deps install fails at import.  Satisfying it properly would add docopt,
# jsonschema and setuptools_scm to the one image that needs network egress.
# workbench_study.mwtab_id_field() reproduces the constructor in two lines.
RUN source /opt/conda/etc/profile.d/conda.sh && \
    conda create -n mwextract \
      --solver=libmamba \
      --override-channels \
      -c conda-forge \
      python=3.11 \
      pandas \
      numpy \
      pyarrow \
      requests \
      -y && \
    conda clean -afy

ENV PATH="/opt/conda/envs/mwextract/bin:${PATH}"

# Fail the BUILD rather than the first run if a dependency is missing.
RUN /opt/conda/envs/mwextract/bin/python -c \
    "import pandas, numpy, pyarrow, requests; print('deps ok')"

WORKDIR /modelfiles
COPY ./modelfiles/ /modelfiles/

# Import every stage and read the baked index at build time, so a broken
# module or a corrupt Parquet surfaces here and not against a live FTP session.
RUN /opt/conda/envs/mwextract/bin/python -c \
    "import sys; sys.path.insert(0, '/modelfiles'); \
     import MWExtract, select_storage, download_selected, workbench_study; \
     import pandas as pd; \
     t = pd.read_parquet('/modelfiles/workbench_msms_index.parquet'); \
     assert len(t) == 20764, len(t); \
     assert int(t.ms2_spectrum_count.sum()) == 175700331; \
     print('index ok:', t.shape)"

RUN mkdir -p /input /output

ENV INPUT_DIR=/input
ENV OUTPUT_DIR=/output
# The Workbench FTP asks for a contact address; supply it at run time with
# -e MWB_EMAIL=... or --email.  Deliberately not baked in.
ENV MWB_EMAIL=""

VOLUME ["/input", "/output"]

ENTRYPOINT ["/opt/conda/envs/mwextract/bin/python", "/modelfiles/MWExtract.py"]
