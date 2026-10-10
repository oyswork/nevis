"""Meilisearch indexes and their settings, applied by ``nevis.search.setup``.

The indexes hold only what search needs; results are read back from Postgres by id.
"""

from meilisearch_python_sdk.models.settings import HuggingFaceEmbedder, MeilisearchSettings

CLIENTS_INDEX = "clients"
DOCUMENT_CHUNKS_INDEX = "document_chunks"
PRIMARY_KEY = "id"

# Substring matches on client fields use the experimental CONTAINS filter (see decisions.md).
EXPERIMENTAL_FEATURES = {"containsFilter": True}

DOCUMENT_EMBEDDER = "bge_small"
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
# Text past this is not embedded; chunks are sized to fit (title included).
EMBEDDED_TEXT_MAX_BYTES = 2048

CLIENT_TEXT_FIELDS = ["first_name", "last_name", "email", "description"]

INDEX_SETTINGS: dict[str, MeilisearchSettings] = {
    CLIENTS_INDEX: MeilisearchSettings(
        searchable_attributes=CLIENT_TEXT_FIELDS,
        # CONTAINS only works on filterable attributes.
        filterable_attributes=list(CLIENT_TEXT_FIELDS),
    ),
    DOCUMENT_CHUNKS_INDEX: MeilisearchSettings(
        searchable_attributes=["title", "text"],
        # For deleting a document's chunks, or the ones past its last chunk.
        filterable_attributes=["document_id", "ordinal"],
        # A document matches once, through its best chunk.
        distinct_attribute="document_id",
        embedders={
            DOCUMENT_EMBEDDER: HuggingFaceEmbedder(
                model=EMBEDDING_MODEL,
                document_template="{{doc.title}}: {{doc.text}}",
                document_template_max_bytes=EMBEDDED_TEXT_MAX_BYTES,
            )
        },
    ),
}
