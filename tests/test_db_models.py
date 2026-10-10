from datetime import UTC, datetime
from uuid import uuid4

import pytest
from generate_models import MODELS_PATH, SCHEMA_PATH, render_models
from pydantic import ValidationError

from nevis.db_models import Client

TIMESTAMPS_DDL = """
CREATE TABLE events (
    happened_at timestamptz NOT NULL,
    seen_at timestamptz[] NOT NULL,
    logged_at timestamp NOT NULL
);
"""


@pytest.mark.unit
def test_generated_db_models_match_schema() -> None:
    expected = render_models(SCHEMA_PATH.read_text())

    assert MODELS_PATH.read_text() == expected, (
        "src/nevis/db_models.py is stale; run: uv run python scripts/generate_models.py"
    )


@pytest.mark.unit
def test_timestamptz_columns_map_to_aware_datetime() -> None:
    code = render_models(TIMESTAMPS_DDL)

    assert "from pydantic import AwareDatetime, BaseModel" in code
    assert "    happened_at: AwareDatetime\n" in code
    assert "    seen_at: list[AwareDatetime]\n" in code


@pytest.mark.unit
def test_generated_models_omit_future_annotations_import() -> None:
    # Python 3.14 evaluates annotations lazily (PEP 649), so the import is redundant.
    code = render_models(TIMESTAMPS_DDL)

    assert "from __future__" not in code


@pytest.mark.unit
def test_timestamp_without_time_zone_stays_plain_datetime() -> None:
    code = render_models(TIMESTAMPS_DDL)

    assert "    logged_at: datetime.datetime\n" in code


@pytest.mark.unit
def test_generated_client_rejects_naive_created_at() -> None:
    row = {
        "id": uuid4(),
        "first_name": "John",
        "last_name": "Doe",
        "email": "john.doe@neviswealth.com",
        "description": None,
        "social_links": [],
    }

    Client.model_validate({**row, "created_at": datetime.now(UTC)})
    with pytest.raises(ValidationError, match="timezone"):
        Client.model_validate({**row, "created_at": datetime(2026, 10, 10, 12, 0)})
