"""Web process (pages, API, fal webhook, event stream). Phase 0: health check only."""
from fastapi import FastAPI

app = FastAPI(title="microdrama")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
