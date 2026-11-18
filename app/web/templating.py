from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

from app import overview  # noqa: E402

templates.env.globals["unread_alerts"] = overview.unread_alerts
templates.env.globals["stage_label"] = lambda key: overview.STAGE_LABELS.get(key, key)
