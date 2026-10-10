"""Validation rules shared by the request schemas."""

# Postgres text columns can't store NUL; without this the INSERT fails with a 500, not a 422.
NO_NUL_PATTERN = r"^[^\x00]*$"
