"""Source ingestion (guide 9.3): extract, chunk, embed, analyse, write.

Nothing from the raw text is ever shown to the Showrunner. It is kept only for the originality
checks. Pattern cards are English, name-free summaries of structure.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path

from psycopg.types.json import Jsonb

from app import db
from app.config import get_config
from app.jobs.errors import PermanentError
from app.models.library import GroupAnalysis, PatternCardDef, SourceAnalysis, TermDef
from app.text import contains_term, detect_language, has_shared_run, normalize_for_match, word_count
from app.vendors import images, llm

SUPPORTED_SUFFIXES = (".txt", ".epub", ".pdf")
EMBED_BATCH = 100
EMBED_MAX_CHARS = {"en": 28000, "bn": 7000, "other": 7000}  # keeps one input under the 8,192-token limit
CARD_COUNT = (5, 15)
CARD_MAX_WORDS = 80
SUMMARY_WORDS = (120, 300)  # "about 200 words"

GROUP_INSTRUCTIONS = """You are analysing one part of a novel or story for a micro-drama studio.
Return:
1. notes: an English summary of this part in at most 150 words: plot events, character relationships and emotional turns.
2. terms: every character name, place name and invented term (made-up words, titles, organisations) that appears.
   For each term give `latin` (the term in Latin script; transliterate if the source is in Bangla), `bangla` (the same term in
   Bangla script; transliterate if the source is in Latin) and `kind` (character, place or invented).
   List nicknames and aliases as separate terms. Do not list ordinary words."""

FINAL_INSTRUCTIONS = """You are building reusable story-structure knowledge from notes about one source story. All output is English.
Return:
1. pattern_cards: between 5 and 15 cards. Each has a `kind` (trope, beat_structure, pacing, hook, cliffhanger, archetype or
   emotional_turn), a short `title`, and a `body` of at most 80 words describing the pattern in general terms.
   Never use a character name, place name or invented term from the source, and never quote the source.
2. plot_summary: about 200 words describing the whole story's plot.
3. source_card: logline, tone, setting, main_conflict and themes, for a person deciding whether to use this source."""


# ---------------------------------------------------------------- extraction

class _HtmlText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def _html_to_text(raw: str) -> str:
    parser = _HtmlText()
    parser.feed(raw)
    text = html.unescape("".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def extract_text(path: str | Path, file_name: str) -> str:
    """Plain text of a .txt, .epub or .pdf. Epub documents are separated by form feeds, which the
    chunker treats as chapter breaks. Raises PermanentError with a clear message when unreadable."""
    suffix = Path(file_name).suffix.lower()
    path = Path(path)
    try:
        if suffix == ".txt":
            raw = path.read_bytes()
            for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise PermanentError("the text file is not valid UTF-8; save it as UTF-8 and upload again")
        elif suffix == ".epub":
            from ebooklib import ITEM_DOCUMENT, epub

            book = epub.read_epub(str(path), options={"ignore_ncx": True})
            docs = [_html_to_text(item.get_content().decode("utf-8", "ignore")) for item in book.get_items_of_type(ITEM_DOCUMENT)]
            text = "\f".join(d for d in docs if d)
        elif suffix == ".pdf":
            from pypdf import PdfReader

            text = "\n\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
        else:
            raise PermanentError(f"unsupported file type {suffix or '(none)'}; use .txt, .epub or .pdf")
    except PermanentError:
        raise
    except Exception as exc:  # noqa: BLE001 - corrupt files raise many different errors
        raise PermanentError(f"the {suffix[1:] or 'file'} could not be read: {type(exc).__name__}: {str(exc)[:160]}") from exc
    if not text.strip():
        raise PermanentError("no text could be extracted (an image-only or empty file?)")
    return text


# ---------------------------------------------------------------- chunking

_HEADING = re.compile(
    r"^[ \t]*(?:(?:chapter|ch\.)[ \t]+(?:[0-9]+|[ivxlcdm]+|[a-z\-]+)"
    r"|part[ \t]+(?:[0-9]+|[ivxlcdm]+)"
    r"|(?:অধ্যায়|পরিচ্ছেদ|পর্ব)(?:[ \t]*[:\-–]?[ \t]*[০-৯0-9]+)?)(?![\w]).{0,60}$",
    re.IGNORECASE | re.MULTILINE,
)


def _paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n[ \t]*\n", text) if p.strip()]
    if len(paras) == 1 and "\n" in paras[0]:
        paras = [p.strip() for p in paras[0].split("\n") if p.strip()]
    return paras


def _cap_split(text: str, cap_words: int) -> list[str]:
    """Pieces of at most cap_words words, cut at paragraph breaks (hard cut inside a giant paragraph)."""
    pieces, cur, cur_words = [], [], 0
    for para in _paragraphs(text):
        words = para.split()
        while len(words) > cap_words:  # one enormous paragraph
            if cur:
                pieces.append("\n\n".join(cur))
                cur, cur_words = [], 0
            pieces.append(" ".join(words[:cap_words]))
            words = words[cap_words:]
        if not words:
            continue
        if cur_words + len(words) > cap_words and cur:
            pieces.append("\n\n".join(cur))
            cur, cur_words = [], 0
        cur.append(" ".join(words))
        cur_words += len(words)
    if cur:
        pieces.append("\n\n".join(cur))
    return pieces


def split_chunks(text: str, language: str | None = None) -> list[str]:
    """Split by chapter headings; if none are found, every `chunk_words` words at a paragraph break.
    A chapter longer than the limit is split further so every chunk can be embedded."""
    cap = get_config("chunk_words").get(language or detect_language(text), 1000)
    if "\f" in text:  # epub documents
        sections = [s for s in text.split("\f") if s.strip()]
    else:
        matches = list(_HEADING.finditer(text))
        if len(matches) >= 2:
            starts = [m.start() for m in matches]
            sections = [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]
            front = text[:starts[0]].strip()
            if front:  # text before the first heading
                if word_count(front) >= 100:
                    sections.insert(0, front)
                else:
                    sections[0] = front + "\n\n" + sections[0]
        else:
            sections = [text]
    chunks: list[str] = []
    for section in sections:
        chunks.extend(_cap_split(section, cap))
    return chunks


# ---------------------------------------------------------------- analysis

def _cost_rows(usages: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for u in usages:
        if "tokens_in" in u and ":" in u["model"]:
            vendor, model = u["model"].split(":", 1)
            rows.append({"vendor": vendor, "model": model, "units": u["tokens_in"], "unit_name": "tokens_in", "cost_usd": llm.llm_cost(u["model"], u["tokens_in"], 0)})
            rows.append({"vendor": vendor, "model": model, "units": u["tokens_out"], "unit_name": "tokens_out", "cost_usd": llm.llm_cost(u["model"], 0, u["tokens_out"])})
        else:  # embeddings
            rows.append({"vendor": "openai", "model": u["model"], "units": u.get("tokens", 0), "unit_name": "tokens_in", "cost_usd": u["cost_usd"]})
    return rows


def _groups(chunks: list[str], group_words: int) -> list[str]:
    groups, cur, cur_words = [], [], 0
    for c in chunks:
        w = word_count(c)
        if cur and cur_words + w > group_words:
            groups.append("\n\n".join(cur))
            cur, cur_words = [], 0
        cur.append(c)
        cur_words += w
    if cur:
        groups.append("\n\n".join(cur))
    return groups


def validate_analysis(analysis: SourceAnalysis, terms_normalized: list[str], source_text: str) -> list[str]:
    """Code checks on the model's output. Returns a list of problems (empty means valid)."""
    problems: list[str] = []
    n = len(analysis.pattern_cards)
    if not CARD_COUNT[0] <= n <= CARD_COUNT[1]:
        problems.append(f"return between {CARD_COUNT[0]} and {CARD_COUNT[1]} pattern cards, not {n}")
    for i, card in enumerate(analysis.pattern_cards, 1):
        label = f"card {i} ('{card.title[:40]}')"
        if not card.title.strip() or not card.body.strip():
            problems.append(f"{label} has an empty title or body")
        if word_count(card.body) > CARD_MAX_WORDS:
            problems.append(f"{label} body is {word_count(card.body)} words; the limit is {CARD_MAX_WORDS}")
        norm = normalize_for_match(card.title + " " + card.body)
        leaked = [t for t in terms_normalized if contains_term(norm, t)]
        if leaked:
            problems.append(f"{label} contains source names or terms ({', '.join(leaked[:3])}); remove them")
        if has_shared_run(card.body, source_text, 8):
            problems.append(f"{label} quotes the source text; describe it in your own words")
    words = word_count(analysis.plot_summary)
    if not SUMMARY_WORDS[0] <= words <= SUMMARY_WORDS[1]:
        problems.append(f"plot_summary is {words} words; write about 200")
    return problems


def analyse(chunks: list[str], language: str) -> tuple[SourceAnalysis, list[TermDef], list[dict]]:
    """Two steps: per-part notes and terms, then cards, summary and source card from the notes.
    Returns (analysis, deduplicated terms, usages)."""
    usages: list[dict] = []
    notes: list[str] = []
    terms: dict[tuple[str, str], TermDef] = {}
    group_words = get_config("ingest_group_words").get(language, 4000)
    for i, group in enumerate(_groups(chunks, group_words), 1):
        result, usage = llm.complete("writer", GROUP_INSTRUCTIONS, [{"role": "user", "content": group}], schema=GroupAnalysis)
        usages.append(usage)
        notes.append(f"Part {i}: {result.notes}")
        for t in result.terms:
            terms.setdefault((t.latin.strip().lower(), t.bangla.strip()), t)
    term_list = list(terms.values())
    norm_terms = sorted({normalize_for_match(x) for t in term_list for x in (t.latin, t.bangla) if x.strip()} - {""})
    source_text = "\n\n".join(chunks)
    material = "\n\n".join(notes)
    feedback = ""
    for attempt in (1, 2):
        analysis, usage = llm.complete("writer", FINAL_INSTRUCTIONS, [{"role": "user", "content": material + feedback}], schema=SourceAnalysis)
        usages.append(usage)
        problems = validate_analysis(analysis, norm_terms, source_text)
        if not problems:
            return analysis, term_list, usages
        feedback = "\n\nYour last answer had these problems. Fix all of them:\n- " + "\n- ".join(problems)
    raise PermanentError("the analysis kept failing validation: " + "; ".join(problems))


def embed_chunks(chunks: list[str], language: str) -> tuple[list[list[float]], list[dict]]:
    limit = EMBED_MAX_CHARS.get(language, 7000)
    vectors, usages = [], []
    for i in range(0, len(chunks), EMBED_BATCH):
        vecs, usage = images.embed([c[:limit] for c in chunks[i:i + EMBED_BATCH]])
        vectors.extend(vecs)
        usages.append(usage)
    return vectors, usages


# ---------------------------------------------------------------- orchestration

def ingest(source: dict, local_path: str | Path) -> dict:
    """Do all the work for one source and write the results in one transaction. Returns the job
    result (with cost_log rows)."""
    text = extract_text(local_path, source["file_name"])
    language = detect_language(text)
    chunks = split_chunks(text, language)
    vectors, embed_usages = embed_chunks(chunks, language)
    analysis, terms, usages = analyse(chunks, language)
    card_vecs, card_usage = images.embed([f"{c.title}. {c.body}" for c in analysis.pattern_cards])

    blocklist: dict[str, tuple[str, str]] = {}
    for t in terms:
        for term in (t.latin, t.bangla):
            norm = normalize_for_match(term)
            if norm:
                blocklist.setdefault(norm, (term.strip(), t.kind))

    card = {**analysis.source_card.model_dump(), "word_count": word_count(text), "chunk_count": len(chunks), "language": language}
    with db.transaction() as conn:
        sid = source["id"]
        for table in ("source_chunks", "pattern_cards", "blocklist_terms"):  # a retry replaces earlier output
            conn.execute(f"delete from {table} where source_id = %s", (sid,))
        cur = conn.cursor()
        cur.executemany(
            "insert into source_chunks (source_id, chapter_index, text, embedding) values (%s, %s, %s, %s::vector)",
            [(sid, i, c, db.to_vector(v)) for i, (c, v) in enumerate(zip(chunks, vectors))],
        )
        cur.executemany(
            "insert into pattern_cards (source_id, genre_id, kind, title, body, embedding) values (%s, %s, %s, %s, %s, %s::vector)",
            [(sid, source["genre_id"], c.kind, c.title.strip(), c.body.strip(), db.to_vector(v)) for c, v in zip(analysis.pattern_cards, card_vecs)],
        )
        cur.executemany(
            "insert into blocklist_terms (source_id, term, term_normalized, kind) values (%s, %s, %s, %s)",
            [(sid, term, norm, kind) for norm, (term, kind) in blocklist.items()],
        )
        conn.execute(
            "update sources set status = 'ready', error = null, language = %s, source_card = %s, plot_summary = %s,"
            " lore_version = nextval('lore_version_seq') where id = %s",
            (language, Jsonb(card), analysis.plot_summary.strip(), sid),
        )
    all_usages = [*embed_usages, *usages, card_usage]
    return {
        "source_id": str(sid), "chunks": len(chunks), "pattern_cards": len(analysis.pattern_cards), "blocklist_terms": len(blocklist),
        "language": language, "actual_cost": round(sum(u["cost_usd"] for u in all_usages), 6), "cost_log": _cost_rows(all_usages),
    }
