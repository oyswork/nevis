import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

NAME_MAX_LENGTH = 100  # varchar(100) in schema/schema.sql
DESCRIPTION_MAX_LENGTH = 10_000
SOCIAL_LINK_MAX_LENGTH = 2048
SOCIAL_LINKS_MAX_COUNT = 20

# Postgres text columns can't store NUL; without this the INSERT fails with a 500, not a 422.
NO_NUL_PATTERN = r"^[^\x00]*$"

Name = Annotated[
    str, StringConstraints(min_length=1, max_length=NAME_MAX_LENGTH, pattern=NO_NUL_PATTERN)
]
SocialLink = Annotated[
    str,
    StringConstraints(min_length=1, max_length=SOCIAL_LINK_MAX_LENGTH, pattern=NO_NUL_PATTERN),
]
Description = Annotated[
    str, StringConstraints(max_length=DESCRIPTION_MAX_LENGTH, pattern=NO_NUL_PATTERN)
]


class ClientCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    first_name: Name
    last_name: Name
    email: EmailStr
    description: Description | None = None
    social_links: list[SocialLink] = Field(default_factory=list, max_length=SOCIAL_LINKS_MAX_COUNT)


class ClientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    description: str | None
    social_links: list[str]
