"""Starting a production (guide 12.4): server-side validation, creation, and the graph's repeat of the checks."""
from __future__ import annotations

import io
import re
import os
import uuid
from dataclasses import dataclass, field

from PIL import Image, UnidentifiedImageError

from app import alerts, audit, db
from app.config import get_config
from app.models.enums import AlertKind
from app.qa import face
from app.vendors import storage

MIN_SIDE = 512
MAX_IMAGE_BYTES = 25 * 1024 * 1024   # per uploaded picture
PLACEMENT_TYPES = ("product_in_use", "product_shown", "product_ambient", "logo_in_scene", "spoken")
VISUAL_PLACEMENTS = ("product_in_use", "product_shown", "product_ambient", "logo_in_scene")   # drawn by the video model inside a scene
BRAND_ASSET_KINDS = ("logo", "product", "app_screen")
PRODUCT_VIEWS = ("front", "back", "side", "top", "open", "in_use")
MIN_PRODUCT_SIDE = 800        # real product photos must be sharp enough to read the label
MIN_LOGO_SIDE = 300
MIN_DESCRIPTION_WORDS = 20
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
NOT_LISTED_ALERT = (
    "No reference material exists for this genre. Add sources through Feed Showrunner context, "
    "or provide a complete story of your own to use as inspiration."
)


class Problem(str):
    """A validation message that remembers which form field it belongs to, so the page can show it beside that field.
    It is still a plain string everywhere else (the JSON `errors` list, tests)."""

    field: str | None

    def __new__(cls, field: str | None, message: str):
        obj = super().__new__(cls, message)
        obj.field = field
        return obj


LEAD_ROLES = (("male lead", "male_photo"), ("female lead", "female_photo"))
ROLE_FIELD = dict(LEAD_ROLES)


def fields_of(problems: list) -> dict[str, list[str]]:
    """{field: [messages]} for the page; a problem without a field goes under "_"."""
    out: dict[str, list[str]] = {}
    for p in problems:
        out.setdefault(getattr(p, "field", None) or "_", []).append(str(p))
    return out


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
    brand_color: str = "#FFFFFF"
    product_description: str = ""      # what the product is and how it is used
    product_physical: str = ""         # size, shape, colours, packaging
    label_texts: list[str] = field(default_factory=list)       # text printed on the pack, one entry per line
    forbidden_claims: list[str] = field(default_factory=list)  # things the story must never claim
    min_exposure_seconds: float = 3.0
    brand_assets: list[tuple[str, str, str, bytes, str]] = field(default_factory=list)  # (kind, label, file name, bytes, view)
    two_leads: bool = False             # the form's two photo boxes: a male lead and a female lead, both required


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


def clean_text(text: str, limit: int = 600) -> str:
    """Everything typed about the brand is untrusted data: control characters out, whitespace collapsed, length limited."""
    text = "".join(ch for ch in (text or "") if ch == "\n" or ch >= " ")
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


def validate_brand(form: IntakeForm) -> list[Problem]:
    """The brand pack (docs/AD_PLACEMENT.md): what the product is and how it is used, the printed text, a logo and real product photos."""
    problems: list[Problem] = []
    if len(form.product_description.split()) < MIN_DESCRIPTION_WORDS:
        problems.append(Problem("product_description", f"Describe the product and how it is used in at least {MIN_DESCRIPTION_WORDS} words (yours has {len(form.product_description.split())})."))
    if not [t for t in form.label_texts if t.strip()]:
        problems.append(Problem("label_texts", "List the text printed on the product pack (at least one line); the checks compare the label with it."))
    if not 1 <= form.min_exposure_seconds <= 10:
        problems.append(Problem("min_exposure_seconds", "The minimum on-screen seconds per placement must be between 1 and 10."))
    if not HEX_COLOR.match(form.brand_color.strip() or "#FFFFFF"):
        problems.append(Problem("brand_color", "The brand colour must look like #1A2B3C."))
    if not form.brand_assets:
        problems.append(Problem("brand_files", "Add the brand assets: at least one logo and at least one real photo of the product (front view)."))
        return problems
    kinds = {k for k, _, _, _, _ in form.brand_assets}
    if "logo" not in kinds:
        problems.append(Problem("brand_files", "Add at least one logo file (PNG, JPEG or WebP; a transparent PNG is best)."))
    visual = [p for p in form.placement_types if p in VISUAL_PLACEMENTS]
    fronts = [a for a in form.brand_assets if a[0] in ("product", "app_screen") and a[4] in ("front", "screen")]
    if visual and not fronts:
        problems.append(Problem("brand_files", "Add at least one real photo of the product with the view set to front."))
    for i, (kind, label, name, data, view) in enumerate(form.brand_assets, 1):
        who = f"Brand asset {i} ({label or name})"
        if kind not in BRAND_ASSET_KINDS:
            problems.append(Problem("brand_files", f"{who}: the kind must be one of {', '.join(BRAND_ASSET_KINDS)}."))
            continue
        if kind == "product" and view not in PRODUCT_VIEWS:
            problems.append(Problem("brand_files", f"{who}: the view must be one of {', '.join(PRODUCT_VIEWS)}."))
        img = _open(data)
        if img is None:
            problems.append(Problem("brand_files", f"{who} is not a readable image: use PNG, JPEG or WebP (models cannot use SVG)."))
            continue
        minimum = MIN_PRODUCT_SIDE if kind in ("product", "app_screen") else MIN_LOGO_SIDE
        if min(img.size) < minimum:
            problems.append(Problem("brand_files", f"{who} is too small: the shortest side must be at least {minimum} pixels so the label can be read."))
    return problems


def validate(form: IntakeForm) -> list[Problem]:
    """Every problem at once, in the guide's order, each tagged with its form field."""
    problems: list[Problem] = []
    # 1. category (and the brand brief)
    if form.category not in ("standard", "ad_placement"):
        problems.append(Problem("category", "Choose a category: standard drama or ad placement drama."))
    elif form.category == "ad_placement":
        if not form.brand_name.strip():
            problems.append(Problem("brand_name", "Brand name is required for an ad placement drama."))
        if not form.product.strip():
            problems.append(Problem("product", "Product is required for an ad placement drama."))
        if not form.placement_types or any(p not in PLACEMENT_TYPES for p in form.placement_types):
            problems.append(Problem("placement_types", "Choose at least one placement type (" + ", ".join(PLACEMENT_TYPES) + ")."))
        problems += validate_brand(form)
    # 2. genre
    if form.genre_id:
        if not _ready_genre(form.genre_id):
            problems.append(Problem("genre_id", "That genre has no processed sources yet."))
    elif form.custom_genre.strip():
        words = len(form.story_text.split())
        minimum = int(get_config("story_min_words"))
        if words < minimum:
            problems.append(Problem("story_text", f"A genre that is not in the library needs a complete story of your own: at least {minimum:,} words (yours has {words:,})."))
    else:
        problems.append(Problem("genre_id", NOT_LISTED_ALERT))
    # 3. keywords
    if not 3 <= len(form.keywords) <= 8:
        problems.append(Problem("keywords", f"Give 3 to 8 keywords (you gave {len(form.keywords)})."))
    # 4. language
    if form.language not in ("bn", "en"):
        problems.append(Problem("language", "Choose the dialogue language: Bangla or English."))
    elif form.language == "bn" and os.environ.get("VOICE_BACKEND") == "local":
        problems.append(Problem("language", "Bangla is not available in this test setup: the open-source test voice (VOICE_BACKEND=local) speaks English only. "
                                "Choose English, or unset VOICE_BACKEND once the ElevenLabs key is configured."))
    for i, (role, data) in enumerate(form.char_refs, 1):
        if len(data) > MAX_IMAGE_BYTES:
            who = f"The {role.strip().lower()} photo" if form.two_leads and role.strip().lower() in ROLE_FIELD else f"Character reference {i}"
            problems.append(Problem(ROLE_FIELD.get(role.strip().lower(), "char_files"), f"{who} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB."))
    for i, d in enumerate(form.aesthetic_refs, 1):
        if len(d) > MAX_IMAGE_BYTES:
            problems.append(Problem("aesthetic_files", f"Aesthetic reference {i} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB."))
    for i, a in enumerate(form.brand_assets, 1):
        if len(a[3]) > MAX_IMAGE_BYTES:
            problems.append(Problem("brand_files", f"Brand asset {i} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB."))
    # 5. character references
    if form.two_leads:
        have = {role.strip().lower() for role, _ in form.char_refs}
        for role, fld in LEAD_ROLES:
            if role not in have:
                problems.append(Problem(fld, f"Add a clear photo of the {role}."))
    elif not form.char_refs:
        problems.append(Problem("char_files", "Add at least one character reference photo."))
    for i, (role, data) in enumerate(form.char_refs, 1):
        fld = ROLE_FIELD.get(role.strip().lower(), "char_files")
        label = f"The {role.strip().lower()} photo" if form.two_leads and role.strip().lower() in ROLE_FIELD else f"Character reference {i}" + (f" ({role.strip()})" if role.strip() else "")
        if not role.strip():
            problems.append(Problem(fld, f"{label} needs a role label."))
        img = _open(data)
        if img is None:
            problems.append(Problem(fld, f"{label} is not a readable image."))
            continue
        if min(img.size) < MIN_SIDE:
            problems.append(Problem(fld, f"{label} is too small: the shortest side must be at least {MIN_SIDE} pixels."))
            continue
        n = face.count_faces(data)
        if n != 1:
            problems.append(Problem(fld, f"{label} must show exactly one clear face (found {n})."))
    # 6. aesthetic references
    note_words = len(form.aesthetic_note.split())
    if len(form.aesthetic_refs) < 3 and note_words < 20:
        problems.append(Problem("aesthetic_files", "Give at least 3 aesthetic reference images, or an aesthetic note of at least 20 words."))
    for i, data in enumerate(form.aesthetic_refs, 1):
        if _open(data) is None:
            problems.append(Problem("aesthetic_files", f"Aesthetic reference {i} is not a readable image."))
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
    for kind, label, name, data, view in form.brand_assets:
        aid = uuid.uuid4()
        key = f"series/{series_id}/brand/{aid}.{_safe_ext(name)}"
        storage.put_bytes(key, data, "application/octet-stream")  # brand assets are stored exactly as supplied
        assets.append((kind if kind in BRAND_ASSET_KINDS else "product", label or name, key, view if kind == "product" else ("screen" if kind == "app_screen" else "logo")))

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
                "insert into brand_briefs (series_id, version, brand_name, product, placement_types, frequency_note, exclusions, spoken_wording, brand_color,"
                " product_description, product_physical, label_texts, forbidden_claims, min_exposure_seconds)"
                " values (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (series_id, clean_text(form.brand_name, 120), clean_text(form.product, 160), form.placement_types, clean_text(form.frequency_note, 300), [clean_text(x, 120) for x in form.exclusions],
                 clean_text(form.spoken_wording, 300) or None, (form.brand_color.strip() or "#FFFFFF").upper(), clean_text(form.product_description, 1500),
                 clean_text(form.product_physical, 800), [clean_text(x, 160) for x in form.label_texts if x.strip()], [clean_text(x, 160) for x in form.forbidden_claims if x.strip()],
                 form.min_exposure_seconds),
            )
            cur.executemany("insert into brand_assets (series_id, kind, label, r2_key, view) values (%s, %s, %s, %s, %s)", [(series_id, k, label, key, v) for k, label, key, v in assets])
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
