from unittest.mock import NonCallableMagicMock, create_autospec

import asyncpg
import pytest

from nevis.db import DbPool, register_vector_codec
from nevis.embeddings import EMBEDDING_DIMENSIONS


def unit_vector(axis: int) -> list[float]:
    return [1.0 if position == axis else 0.0 for position in range(EMBEDDING_DIMENSIONS)]


@pytest.mark.unit
async def test_missing_vector_extension_is_reported_clearly() -> None:
    connection: NonCallableMagicMock = create_autospec(asyncpg.Connection, instance=True)
    connection.set_type_codec.side_effect = ValueError("unknown type: public.vector")

    with pytest.raises(RuntimeError, match=r"'vector' extension is missing.*02-extensions\.sql"):
        await register_vector_codec(connection)


@pytest.mark.integration
async def test_vector_round_trips_as_a_list_of_floats(db_pool: DbPool) -> None:
    value = [0.5] * EMBEDDING_DIMENSIONS

    stored = await db_pool.fetchval("SELECT $1::vector", value)

    assert stored == value
    assert isinstance(stored, list)


@pytest.mark.integration
async def test_vector_parameter_orders_by_cosine_distance(db_pool: DbPool) -> None:
    rows = await db_pool.fetch(
        "SELECT axis FROM (VALUES (0, $1::vector), (1, $2::vector)) AS v (axis, embedding) "
        "ORDER BY embedding <=> $3 LIMIT 1",
        unit_vector(0),
        unit_vector(1),
        unit_vector(1),
    )

    assert [row["axis"] for row in rows] == [1]
