"""AI runtime: vendor-agnostic LLM client + schema-validated runner with offline fallback."""
from .runner import SKILLS, load_schema, run_skill

__all__ = ["SKILLS", "load_schema", "run_skill"]
