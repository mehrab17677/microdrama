"""Starting a production (guide 12.4): server-side validation, creation, and the graph's repeat of the checks."""
from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass, field

from PIL import Image, UnidentifiedImageError

from app import alerts, audit, db
from app.config import get_config
from app.models.enums import AlertKind
from app.qa import face
from app.vendors import storage

MIN_SIDE = 512
PLACEMENT_TYPES = ("phone_full", "phone_in_hand", "signage", "spoken")
BRAND_ASSET_KINDS = ("logo", "app_screen", "product")
NOT_LISTED_ALERT = (
    "No reference material exists for this genre. Add sources through Feed Showrunner context, "
    "or provide a complete story of your own to use as inspiration."
)


@dataclass
class IntakeForm:
    category: str = ""
    genre_id: str | None = None
    custom_genre: str = ""
    story_text: str = ""
    keywords: list[str] = field(default_factory=list)
    language: str = ""
    aesthetic_note: str = ""
    char_refs: list[tuple[str, bytes]] = field(default_factory=list)          # (role label, image bytes)
    aesthetic_refs: list[bytes] = field(default_factory=list)
    brand_name: str = ""
    product: str = ""
    placement_types: list[str] = field(default_factory=list)
    frequency_note: str = ""
    exclusions: list[str] = field(default_factory=list)
    spoken_wording: str = ""
    brand_assets: list[tuple[str, str, str, bytes]] = field(default_factory=list)  # (kind, label, file name, bytes)


def parse_keywords(text: str) -> list[str]:
    return [k.strip() for k in re.split(r"[,\n;]", text or "") if k.strip()]


def _open(data: bytes):
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def _ready_genre(genre_id) -> bool:
    try:
        uuid.UUID(str(genre_id))
    except ValueError:
        return False
    return bool(db.fetch_one("select 1 from sources where genre_id = %s and status = 'ready' limit 1", (genre_id,)))


def validate(form: IntakeForm) -> list[str]:
    """Every problem at once, in the guide's order."""
    problems: list[str] = []
    # 1. category (and the brand brief)
    if form.category not in ("standard", "ad_placement"):
        problems.append("Choose a category: standard drama or ad placement drama.")
    elif form.category == "ad_placement":
        if not form.brand_name.strip():
            problems.append("Brand name is required for an ad placement drama.")
        if not form.product.strip():
            problems.append("Product is required for an ad placement drama.")
        if not form.placement_types or any(p not in PLACEMENT_TYPES for p in form.placement_types):
            problems.append("Choose at least one placement type (phone_full, phone_in_hand, signage, spoken).")
        if not form.brand_assets:
            problems.append("Add at least one brand asset (logo, app screen or product image).")
    # 2. genre
    if form.genre_id:
        if not _ready_genre(form.genre_id):
            problems.append("That genre has no processed sources yet.")
    elif form.custom_genre.strip():
        words = len(form.story_text.split())
        minimum = int(get_config("story_min_words"))
        if words < minimum:
            problems.append(f"A genre that is not in the library needs a complete story of your own: at least {minimum:,} words (yours has {words:,}).")
    else:
        problems.append(NOT_LISTED_ALERT)
    # 3. keywords
    if not 3 <= len(form.keywords) <= 8:
        problems.append(f"Give 3 to 8 keywords (you gave {len(form.keywords)}).")
    # 4. language
    if form.language not in ("bn", "en"):
        problems.append("Choose the dialogue language: Bangla or English.")
    # 5. character references
    if not form.char_refs:
        problems.append("Add at least one character reference photo.")
    for i, (role, data) in enumerate(form.char_refs, 1):
        label = f"Character reference {i}" + (f" ({role.strip()})" if role.strip() else "")
        if not role.strip():
            problems.append(f"{label} needs a role label.")
        img = _open(data)
        if img is None:
            problems.append(f"{label} is not a readable image.")
            continue
        if min(img.size) < MIN_SIDE:
            problems.append(f"{label} is too small: the shortest side must be at least {MIN_SIDE} pixels.")
            continue
        n = face.count_faces(data)
        if n != 1:
            problems.append(f"{label} must show exactly one clear face (found {n}).")
    # 6. aesthetic references
    note_words = len(form.aesthetic_note.split())
    if len(form.aesthetic_refs) < 3 and note_words < 20:
        problems.append("Give at least 3 aesthetic reference images, or an aesthetic note of at least 20 words.")
    for i, data in enumerate(form.aesthetic_refs, 1):
        if _open(data) is None:
            problems.append(f"Aesthetic reference {i} is not a readable image.")
    return problems


def _png(data: bytes) -> bytes:
    out = io.BytesIO()
    _open(data).convert("RGB").save(out, "PNG")
    return out.getvalue()


def _safe_ext(name: str) -> str:
    ext = re.sub(r"[^a-z0-9]", "", (name.rsplit(".", 1)[-1] if "." in name else "").lower())
    return ext if ext in ("png", "jpg", "jpeg", "svg", "webp") else "png"


def create_production(user_id, form: IntakeForm) -> uuid.UUID:
    """Create the series and everything it starts with in one transaction. Call validate() first."""
    series_id = uuid.uuid4()
    # Files first: the transaction should be short, and an orphan object is harmless.
    refs = []
    for role, data in form.char_refs:
        rid = uuid.uuid4()
        key = f"series/{series_id}/brief/{rid}.png"
        png = _png(data)
        storage.put_bytes(key, png, "image/png")
        refs.append(("character", role.strip(), key, face.face_vector(png)))
    for data in form.aesthetic_refs:
        rid = uuid.uuid4()
        key = f"series/{series_id}/brief/{rid}.png"
        storage.put_bytes(key, _png(data), "image/png")
        refs.append(("aesthetic", None, key, None))
    assets = []
    for kind, label, name, data in form.brand_assets:
        aid = uuid.uuid4()
        key = f"series/{series_id}/brand/{aid}.{_safe_ext(name)}"
        storage.put_bytes(key, data, "application/octet-stream")  # brand assets are stored exactly as supplied
        assets.append((kind if kind in BRAND_ASSET_KINDS else "product", label or name, key))

    with db.transaction() as conn:
        genre_name = form.custom_genre.strip()
        if form.genre_id:
            genre_name = conn.execute("select name from genres where id = %s", (form.genre_id,)).fetchone()["name"]
        pinned = conn.execute("select max(lore_version) v from sources where status = 'ready'").fetchone()["v"]
        conn.execute(
            "insert into series (id, owner_id, title, category, genre_id, custom_genre, language, status, current_stage, thread_id, pinned_lore_version)"
            " values (%s, %s, %s, %s, %s, %s, %s, 'running', 'story', %s, %s)",
            (series_id, user_id, f"Untitled {genre_name}", form.category, form.genre_id or None, None if form.genre_id else form.custom_genre.strip(),
             form.language, str(series_id), pinned),
        )
        conn.execute(
            "insert into briefs (series_id, version, keywords, aesthetic_note, story_text) values (%s, 1, %s, %s, %s)",
            (series_id, form.keywords, form.aesthetic_note.strip() or None, form.story_text.strip() or None if not form.genre_id else None),
        )
        cur = conn.cursor()
        cur.executemany(
            "insert into brief_refs (series_id, kind, role_label, r2_key, face_vector) values (%s, %s, %s, %s, %s::vector)",
            [(series_id, k, role, key, db.to_vector(v) if v else None) for k, role, key, v in refs],
        )
        if form.category == "ad_placement":
            conn.execute(
                "insert into brand_briefs (series_id, version, brand_name, product, placement_types, frequency_note, exclusions, spoken_wording)"
                " values (%s, 1, %s, %s, %s, %s, %s, %s)",
                (series_id, form.brand_name.strip(), form.product.strip(), form.placement_types, form.frequency_note.strip(), form.exclusions, form.spoken_wording.strip() or None),
            )
            cur.executemany("insert into brand_assets (series_id, kind, label, r2_key) values (%s, %s, %s, %s)", [(series_id, k, label, key) for k, label, key in assets])
        for gate in (0, 1):
            conn.execute("insert into gate_decisions (series_id, gate, decision, decided_by) values (%s, %s, 'approve', %s)", (series_id, gate, user_id))
        audit.log(conn, user_id, "production.create", series_id, {"category": form.category, "language": form.language})
        conn.execute(
            "insert into graph_commands (series_id, graph, thread_id, kind, payload, created_at) values (%s, 'production', %s, 'start', '{}'::jsonb, clock_timestamp())",
            (series_id, str(series_id)),
        )
        alerts.notify([user_id], AlertKind.action_started, "Your production has started.", f"/productions/{series_id}", series_id, conn=conn)
    return series_id


def check_series(series_id) -> list[str]:
    """The graph's first node repeats the checks: files open, images are big enough, one clear face per
    character reference, the genre exists or a complete story is attached (guide 12.4)."""
    import pathlib
    import tempfile

    problems: list[str] = []
    s = db.fetch_one("select * from series where id = %s", (series_id,))
    brief = db.fetch_one("select * from briefs where series_id = %s order by version desc limit 1", (series_id,))
    if s["genre_id"]:
        if not _ready_genre(s["genre_id"]):
            problems.append("The chosen genre has no processed sources.")
    elif len((brief["story_text"] or "").split()) < int(get_config("story_min_words")):
        problems.append("No complete story is attached for a genre that is not in the library.")
    if not 3 <= len(brief["keywords"]) <= 8:
        problems.append("The brief needs 3 to 8 keywords.")
    with tempfile.TemporaryDirectory() as tmp:
        for ref in db.fetch_all("select * from brief_refs where series_id = %s order by created_at", (series_id,)):
            path = pathlib.Path(tmp) / "ref"
            try:
                storage.get_to_file(ref["r2_key"], path)
            except Exception:  # noqa: BLE001
                problems.append(f"A {ref['kind']} reference file is missing.")
                continue
            data = path.read_bytes()
            img = _open(data)
            if img is None:
                problems.append(f"A {ref['kind']} reference does not open as an image.")
            elif ref["kind"] == "character":
                if min(img.size) < MIN_SIDE:
                    problems.append(f"Character reference '{ref['role_label']}' is smaller than {MIN_SIDE} pixels.")
                elif face.count_faces(data) != 1:
                    problems.append(f"Character reference '{ref['role_label']}' must show exactly one clear face.")
    if not db.fetch_one("select 1 from brief_refs where series_id = %s and kind = 'character' limit 1", (series_id,)):
        problems.append("There is no character reference.")
    if s["category"] == "ad_placement" and not db.fetch_one("select 1 from brand_assets where series_id = %s limit 1", (series_id,)):
        problems.append("An ad placement drama needs at least one brand asset.")
    return problems
