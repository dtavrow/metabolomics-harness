FROM python:3.11-slim@sha256:0a236bf8f0b6013383df04d1cd02cb0abe5fbfa1a7012141d7ff9202b7cb30a6

COPY parsers/common/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY parsers/common/ /app/
COPY parsers/tests/ /app/tests/
WORKDIR /app
ENV MWPARSER_FORMAT=mzml
RUN python -c "import pyarrow, pyteomics; import schema; assert schema.SCHEMA_VERSION == '1.0.0'"
ENTRYPOINT ["python", "/app/main.py"]
