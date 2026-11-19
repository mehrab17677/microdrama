"""Dry-run support (guide 7.7): fixture files, deterministic embeddings, and schema-valid objects."""
from __future__ import annotations

import hashlib
import json
import math
import typing
from pathlib import Path
from types import NoneType, UnionType

from pydantic import BaseModel

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"


def fixture_path(name: str) -> Path:
    path = FIXTURE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"dry-run fixture missing: {path} (run scripts/make_fixtures.py)")
    return path


def fixture_bytes(name: str) -> bytes:
    return fixture_path(name).read_bytes()


def fake_embedding(text: str, dims: int = 1536) -> list[float]:
    """Deterministic unit vector: same text, same vector; different text, unrelated vector."""
    out: list[float] = []
    counter = 0
    while len(out) < dims:
        digest = hashlib.sha256(f"{counter}|{text}".encode("utf-8")).digest()
        out.extend((b - 127.5) / 127.5 for b in digest)
        counter += 1
    out = out[:dims]
    norm = math.sqrt(sum(v * v for v in out))
    return [v / norm for v in out]


def synthesize(schema: type[BaseModel]):
    """A minimal valid instance of a pydantic model. A hand-written file
    tests/fixtures/llm/<SchemaName>.json takes precedence when present."""
    path = FIXTURE_DIR / "llm" / f"{schema.__name__}.json"
    if path.exists():
        return schema.model_validate(json.loads(path.read_text()))
    return schema.model_validate(_value(schema))


def _value(tp, name: str = "x"):  # noqa: C901 - a small type walker
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return {n: _value(f.annotation, n) for n, f in tp.model_fields.items() if f.is_required()} | {
            n: _value(f.annotation, n) for n, f in tp.model_fields.items() if not f.is_required() and _wants(f.annotation)
        }
    if origin in (typing.Union, UnionType):
        non_none = [a for a in args if a is not NoneType]
        return _value(non_none[0], name)
    if origin is typing.Literal:
        return args[0]
    if origin in (list, typing.List):
        return [_value(args[0], name)] if args else []
    if origin in (dict, typing.Dict):
        return {"key": _value(args[1], name)} if args else {}
    if tp is str:
        return f"fixture {name}"
    if tp is bool:
        return True
    if tp is int:
        return 1
    if tp is float:
        return 1.0
    if isinstance(tp, type) and issubclass(tp, __import__("enum").Enum):
        return list(tp)[0].value
    if tp in (typing.Any, object):
        return {}
    raise TypeError(f"cannot synthesize a value for {tp!r}; add tests/fixtures/llm/<Schema>.json")


def _wants(tp) -> bool:
    return False
