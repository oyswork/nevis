from typing import Annotated, Literal

from pydantic import BaseModel, Field

from nevis.clients.schemas import ClientRead
from nevis.documents.schemas import DocumentRead


class ClientHit(BaseModel):
    type: Literal["client"] = "client"
    client: ClientRead


class DocumentHit(BaseModel):
    type: Literal["document"] = "document"
    document: DocumentRead
    # The chunk of the content that matched best (whitespace collapsed).
    matched_text: str


SearchHit = Annotated[ClientHit | DocumentHit, Field(discriminator="type")]
