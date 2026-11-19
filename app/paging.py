"""Server-side pagination (LIMIT/OFFSET with a one-row look-ahead)."""
from __future__ import annotations

from typing import Any

from sqlmodel import Session

DEFAULT_PER_PAGE = 50
MAX_PAGE = 10_000


def parse_page(v: Any) -> int:
    try:
        return min(MAX_PAGE, max(1, int(v)))
    except (TypeError, ValueError):
        return 1


def paginate(s: Session, stmt: Any, page: int, per_page: int = DEFAULT_PER_PAGE) -> tuple[list, int, bool]:
    page = min(MAX_PAGE, max(1, page))
    items = list(s.exec(stmt.offset((page - 1) * per_page).limit(per_page + 1)).all())
    return items[:per_page], page, len(items) > per_page
