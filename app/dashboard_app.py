# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Optional, standalone progress dashboard backend.

This is not part of the harness itself -- the harness works entirely
through `claude` + app/cli.py with no server at all. This exists only to
feed a live-progress dashboard (e.g. a separate Next.js frontend) while a
pipeline run is in progress, for demo purposes. Run it yourself:

    uv run uvicorn app.dashboard_app:app --host 0.0.0.0 --port 8000

and point a Cloudflare tunnel / your frontend's API base URL at it.
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.app_utils.dashboard_routes import build_dashboard_router

load_dotenv()

# Plain FastAPI/Starlette CORSMiddleware, unlike the old ADK-based backend's
# custom origin parsing, takes literal origins and a regex as two separate
# params -- it has no "regex:" prefix convention. ALLOW_ORIGIN_REGEX exists
# for exactly one case: Vercel's per-branch preview subdomains, which aren't
# a fixed, literal URL.
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else []
)
allow_origin_regex = os.getenv("ALLOW_ORIGIN_REGEX") or None

app = FastAPI(
    title="metabolomics-harness-dashboard",
    description="Live pipeline progress feed for the optional dashboard frontend.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_origin_regex=allow_origin_regex,
    allow_methods=["GET"],
    allow_headers=["*"],
    # Cloudflare Access sets its session cookie on this hostname; the
    # frontend's fetch()/EventSource calls need to forward it cross-origin,
    # which requires the browser to see this -- credentials can't combine
    # with a literal "*" origin, but allow_origins is never "*" here.
    allow_credentials=True,
)
app.include_router(build_dashboard_router())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
