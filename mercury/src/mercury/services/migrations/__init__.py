"""Database migrations for Mercury StateStore.

Each migration is a Python module with:
- VERSION: int - The migration version number
- DESCRIPTION: str - Human-readable description
- UP_SQL: str - SQL to apply the migration
- DOWN_SQL: str (optional) - SQL to rollback the migration (not always possible)

Migrations are applied in order and tracked in the schema_version table.
"""
