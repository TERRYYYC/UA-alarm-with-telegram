from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class IngestRequest(BaseModel):
    source_id: int
    external_id: str
    revision: int = 1
    body_text: str
    published_at: datetime
    received_at: Optional[datetime] = None
    forward_json: str = "{}"
    edit_json: str = "{}"
    original_url: Optional[str] = None


class CorrectRequest(BaseModel):
    verification: Optional[str] = None
    occurred_at: Optional[datetime] = None
    place_id: Optional[int] = None
    reason: str
    operator: str = "operator"


class ResolveRequest(BaseModel):
    place_id: int
    event_type: Optional[str] = None
    occurred_at: Optional[datetime] = None
    operator: str = "operator"


class RejectRequest(BaseModel):
    reason: str
    operator: str = "operator"


class PlaceUpsert(BaseModel):
    canonical_name: str
    names: Optional[list[str]] = None
    lat: float
    lng: float
    admin1: Optional[str] = None
    admin2: Optional[str] = None
    precision: str = "settlement"
    name_zh: Optional[str] = None
