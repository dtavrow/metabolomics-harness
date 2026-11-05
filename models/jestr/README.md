# JESTR model image

JESTR uses the same stable parquet input, checkpoint manifest, annotation
output, eligibility, and run-report contract as FLARE, GLACIER, and ICEBERG.

Build either image from the repository root:

```text
docker build -f models/jestr/Dockerfile.cpu -t mwannotate-jestr:cpu .
docker build -f models/jestr/Dockerfile -t mwannotate-jestr:cuda .
```

The build downloads and verifies the official MassSpecGym JESTR molecule and
spectrum encoders from `HassounLab/JESTR1`. No repository or weight download
occurs at container runtime.

```text
docker run --rm -v /local/output:/data mwannotate-jestr:cpu \
  --spectra /data/ST004067/processed/mzxml/spectra.parquet \
  --candidates /data/candidates.parquet \
  --output-root /data --run-id jestr-msg-001
```

`preprocess.py` reproduces JESTR's MassSpecGym training protocol: peak rows are
interpreted as `(intensity, m/z)`, intensities are scaled by
`intensity / max * 999`, and peaks above 1000 m/z are removed. Retrieval uses
cosine similarity between the released molecule and spectrum encoders.

Custom checkpoint bundles remain supported with `--checkpoint-bundle`. The old
MGF/CSV implementation remains in `modelfiles/JESTR.py` as upstream reference
code, but it is not the image entry point.
