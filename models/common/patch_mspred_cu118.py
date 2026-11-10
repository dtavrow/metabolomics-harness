"""Patch ms-pred's pyproject.toml for a working cu118 GPU build.

data.dgl.ai (dgl's own wheel host) has been down since its domain
registration lapsed, with no ETA for renewal. The only GPU dgl build still
reachable is conda's (dglteam/label/cu118), whose newest release (2.1.0)
bundles PyTorch tensor-adapter shims only up to PyTorch 2.2.1 -- four minor
versions behind ms-pred's pinned torch==2.6.0. This script:

1. Pins torch/torchvision (and the torch-scatter/torch-sparse pyg index)
   down to versions dgl 2.1.0 actually supports.
2. Pins numpy<2 and torchdata==0.7.1, the torch-2.2.1-era releases of two
   dependencies ms-pred otherwise leaves floating (numpy>=2, torchdata<0.10)
   against its default torch==2.6.0. Left unpinned, uv resolves numpy 2.x
   (ABI-incompatible with torch 2.2.1's C extensions -- "_ARRAY_API not
   found") and torchdata 0.9.0 (imports torch.utils._import_utils, which
   doesn't exist before torch 2.3), and `import dgl` crashes through
   dgl.graphbolt's torchdata dependency.
3. Strips the dgl-cpu/dgl-cu124/dgl-cu118 custom index declarations
   entirely. `uv sync` resolves the whole project lockfile up front
   regardless of which extra is being installed, so leaving any of these
   in place makes it try to contact data.dgl.ai even when `dgl` itself is
   excluded from the install step via --no-install-package.

Run once, in-tree, against the repo root containing pyproject.toml. The
Dockerfile installs dgl's actual binary separately, from the same conda
channel, after this patch lets `uv sync` complete.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

VERSION_SUBSTITUTIONS = [
    (r"torch==2\.6\.0\+cu118", "torch==2.2.1+cu118"),
    (r"torchvision==0\.21\.0\+cu118", "torchvision==0.17.1+cu118"),
    (r"torch-2\.6\.0\+cu118", "torch-2.2.1+cu118"),
    (r'"torchdata<0\.10"', '"torchdata==0.7.1"'),
    (r'"numpy>=2"', '"numpy<2"'),
]

DGL_SOURCES_BLOCK = re.compile(r"dgl = \[\n(?:.*\n)*?\]\n")
DGL_INDEX_BLOCK = re.compile(
    r'\[\[tool\.uv\.index\]\]\nname = "dgl-[a-z0-9]+"\n(?:.*\n)*?explicit = true\n\n?',
)


def main() -> None:
    pyproject_path = Path(sys.argv[1] if len(sys.argv) > 1 else "pyproject.toml")
    text = pyproject_path.read_text()

    for pattern, replacement in VERSION_SUBSTITUTIONS:
        text = re.sub(pattern, replacement, text)

    text = DGL_SOURCES_BLOCK.sub("", text)
    text = DGL_INDEX_BLOCK.sub("", text)

    if "data.dgl.ai" in text:
        raise SystemExit("patch_mspred_cu118: data.dgl.ai reference survived patching")

    pyproject_path.write_text(text)


if __name__ == "__main__":
    main()
