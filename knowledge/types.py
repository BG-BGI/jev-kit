"""The two shapes that cross module boundaries: a Candidate going in, a Node coming out."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Candidate:
    bucket_hint: Optional[str]
    text: str
    tool: str = ""
    signature: Optional[str] = None
    exception_type: str = ""
    entities: tuple = ()
    scope: str = "machine"
    session_id: str = ""
    provenance: str = ""
    source: str = "hook"  # "hook" | "self_report" | "explicit"
    rejected_alternatives: tuple = ()
    fixes_signature: Optional[str] = None


@dataclass
class Node:
    id: int
    bucket: str
    text: str
    scope: str
    signature: Optional[str]
    exception_type: str
    entities: tuple
    evidence: int
    created: float
    touched: float
    superseded_by: Optional[int]
    provenance: str
