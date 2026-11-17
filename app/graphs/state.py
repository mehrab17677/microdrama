"""Graph state (guide 8.1). Nothing else goes in state: no lists, so no reducers are needed."""
from typing import TypedDict


class ProductionState(TypedDict, total=False):
    series_id: str
    batch: str          # 'pilot' or 'full'
    round: int          # repair round inside the current batch, starts at 0
    last_resume: dict   # payload of the most recent resume
