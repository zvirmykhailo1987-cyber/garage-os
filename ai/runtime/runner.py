"""Run an AI skill: prompt + JSON schema → validated draft → human-in-the-loop.

* strict output: the result is validated with ``jsonschema``; an invalid LLM answer gets
  one retry with the validation error, then falls back to the deterministic extractor;
* the model only ever produces DRAFTS labelled for review; money is never computed by it;
* every run is logged to ``AiRun`` (provider, fallback flag, latency, confidence).
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from app import config

from . import extractors
from .llm_client import LLMUnavailable, complete

AI_DIR = Path(__file__).resolve().parent.parent
SKILLS: dict[str, tuple[str, Callable[[str], dict]]] = {
    "work-order-draft": ("work-order-draft", extractors.extract_work_order),
    "nl-filter": ("nl-filter", extractors.extract_nl_filter),
}


@cache
def load_schema(name: str) -> dict:
    return json.loads((AI_DIR / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))


@cache
def _prompt(name: str) -> str:
    p = AI_DIR / "prompts" / f"{name}.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def run_skill(skill: str, text: str, *, session: Any = None, actor: str = "",
              org_id: int | None = None, branch_id: int | None = None) -> dict:
    if skill not in SKILLS:
        raise ValueError(f"unknown skill {skill}")
    schema_name, extractor = SKILLS[skill]
    schema = load_schema(schema_name)
    text = (text or "")[:2000]
    t0 = time.perf_counter()
    draft: dict | None = None
    if config.LLM_PROVIDER != "stub":
        system = _prompt(schema_name) + "\n\nReturn ONLY a JSON object valid against this schema:\n" \
            + json.dumps(schema)
        err = ""
        for attempt in range(2):
            try:
                user = text if attempt == 0 else f"{text}\n\n(Previous answer was invalid: {err})"
                cand = complete(system, user)
                validate(cand, schema)
                draft = cand
                break
            except LLMUnavailable:
                break
            except (ValidationError, ValueError) as e:
                err = str(e)[:200]
    fallback = draft is None
    if draft is None:
        draft = extractor(text)
        validate(draft, schema)            # our own extractor must honour the contract
    ms = int((time.perf_counter() - t0) * 1000)
    conf = float(draft.get("confidence", 1.0))
    if session is not None and org_id is not None:
        from app.models import AiRun
        session.add(AiRun(skill=skill, provider="stub" if fallback else config.LLM_PROVIDER,
                          confidence=conf, fallback=fallback, ms=ms, actor=actor[:120],
                          org_id=org_id, branch_id=branch_id))
    return {"skill": skill, "draft": draft, "fallback": fallback, "confidence": conf}
