import uuid
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
)

from nevis.validation import NO_NUL_PATTERN

NAME_MAX_LENGTH = 100  # varchar(100) in schema/schema.sql
DESCRIPTION_MAX_LENGTH = 10_000
SOCIAL_LINK_MAX_LENGTH = 2048
SOCIAL_LINKS_MAX_COUNT = 20


def _require_http_url(value: str) -> str:
    """Accept only absolute http(s) URLs: links get rendered as hrefs (no javascript: or data:)."""
    HttpUrl(value)
    return value


Name = Annotated[
    str, StringConstraints(min_length=1, max_length=NAME_MAX_LENGTH, pattern=NO_NUL_PATTERN)
]
SocialLink = Annotated[
    str,
    StringConstraints(min_length=1, max_length=SOCIAL_LINK_MAX_LENGTH, pattern=NO_NUL_PATTERN),
    AfterValidator(_require_http_url),
]
Description = Annotated[
    str, StringConstraints(max_length=DESCRIPTION_MAX_LENGTH, pattern=NO_NUL_PATTERN)
]


class ClientCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "first_name": "John",
                    "last_name": "Doe",
                    "email": "john.doe@neviswealth.com",
                    "description": "Prefers sustainable funds; retiring in 2030",
                    "social_links": ["https://www.linkedin.com/in/johndoe"],
                }
            ]
        },
    )

    first_name: Name
    last_name: Name
    email: EmailStr
    description: Description | None = None
    social_links: list[SocialLink] = Field(default_factory=list, max_length=SOCIAL_LINKS_MAX_COUNT)

    @field_validator("description")
    @classmethod
    def _blank_description_to_none(cls, description: str | None) -> str | None:
        # Whitespace is already stripped; keep one representation of "no description".
        return description or None


class ClientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str
    # Documented as format: email like the spec's Client, without re-validating stored rows.
    email: Annotated[str, Field(json_schema_extra={"format": "email"})]
    description: str | None
    social_links: list[str]
