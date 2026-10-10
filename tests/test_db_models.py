import pytest
from generate_models import MODELS_PATH, SCHEMA_PATH, render_models


@pytest.mark.unit
def test_generated_db_models_match_schema() -> None:
    expected = render_models(SCHEMA_PATH.read_text())

    assert MODELS_PATH.read_text() == expected, (
        "src/nevis/db_models.py is stale; run: uv run python scripts/generate_models.py"
    )
