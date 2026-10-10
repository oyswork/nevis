import datetime
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from nevis.validation import NO_NUL_PATTERN

TITLE_MAX_LENGTH = 200  # varchar(200) in schema/schema.sql
CONTENT_MAX_LENGTH = 100_000

Title = Annotated[
    str, StringConstraints(min_length=1, max_length=TITLE_MAX_LENGTH, pattern=NO_NUL_PATTERN)
]
Content = Annotated[
    str, StringConstraints(min_length=1, max_length=CONTENT_MAX_LENGTH, pattern=NO_NUL_PATTERN)
]


class DocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: Title
    content: Content


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID
    title: str
    content: str
    # Null until summarized, and always null without a local summarization model.
    summary: str | None
    created_at: datetime.datetime
