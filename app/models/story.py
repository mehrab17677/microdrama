"""Story-stage schemas used so far (guide 9.1). More arrive with phase 6."""
from pydantic import BaseModel


class StoryCheck(BaseModel):
    has_named_leads: bool
    has_conflict: bool
    has_beginning_middle_end: bool
