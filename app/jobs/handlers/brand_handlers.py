"""llm_product_sheet: describe the real product from the brand's photos once, so prompts can carry an exact, validated description (docs/AD_PLACEMENT.md)."""
from __future__ import annotations

from types import SimpleNamespace

from psycopg.types.json import Jsonb

from app import db, placement
from app.jobs.errors import PermanentError
from app.stages import brand, look, story
from app.stages.ingest import _cost_rows
from app.vendors import llm

KINDS = ["llm_product_sheet"]


def llm_product_sheet(job: dict) -> dict:
    sid = job["series_id"]
    brief = placement.brief(sid)
    if brief is None:
        return {"skipped": "no brand brief"}
    photos = placement.asset_rows(sid, ("product", "app_screen"))[:5]
    logo = placement.asset_rows(sid, ("logo",))[:1]
    images = [look.fetch_bytes(r["r2_key"]) for r in [*photos, *logo]]
    material = f"{len(photos)} product photo(s), then {len(logo)} logo file.\n\n" + brand.data_block(brief) + story.notes_block(job)
    sheet, usage = llm.complete("product_sheet", brand.SHEET_INSTRUCTIONS, [{"role": "user", "content": material}], schema=brand.ProductSheet, images=images,
                                fixture=lambda: brand.sheet_fixture(brief))
    problems = brand.validate_sheet(sheet, brief)
    if problems:
        raise PermanentError(f"{len(problems)} problem(s): " + "; ".join(problems))
    db.execute("update brand_briefs set product_sheet = %s where id = %s", (Jsonb(sheet.model_dump()), brief["id"]))
    return {"elements": len(sheet.elements), "actual_cost": round(usage["cost_usd"], 6), "cost_log": _cost_rows([usage])}


HANDLERS = {kind: SimpleNamespace(run=globals()[kind]) for kind in KINDS}
