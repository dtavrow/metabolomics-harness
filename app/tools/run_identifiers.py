"""A tiny tool so the overseer never has to invent a run id by hand."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime


def generate_run_id(study_id: str) -> str:
    """Generate a readable, collision-resistant run id for grouping a set
    of analysis-model calls against one study."""

    slug = re.sub(r"[^A-Za-z0-9]+", "", study_id).lower()
    timestamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%S")
    return f"{slug}-{timestamp}-{uuid.uuid4().hex[:6]}"
