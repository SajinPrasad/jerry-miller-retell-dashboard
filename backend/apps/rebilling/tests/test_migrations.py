"""Migration 0006 has to survive a database that already carries `charged_at`.

Two branches introduced a column by that name under two different migration filenames, so a
server that ran the other one has the column while `django_migrations` has no row for this
migration - and Django re-runs it. These tests pin the guard that makes the second run a no-op
rather than `duplicate column name: charged_at`.
"""

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps as global_apps
from django.db import connection

from apps.rebilling.models import Bill

# The module name starts with a digit, so it cannot be reached with a plain import statement.
migration = importlib.import_module(
    'apps.rebilling.migrations.0006_bill_charged_at_and_failure_reason'
)

pytestmark = pytest.mark.django_db


def test_the_guard_sees_a_column_that_is_already_there():
    """Proves the test below is not passing vacuously: the column really is present."""
    bill = global_apps.get_model('rebilling', 'Bill')

    assert migration._has_column(connection, bill) is True


def test_adding_charged_at_again_is_a_no_op_rather_than_a_duplicate_column_error():
    """The exact failure the server hit, reproduced: the column exists, the migration re-runs.

    The test database has already been migrated, so `charged_at` is there - the same position a
    server left in by `feat/ghl-billing-with-stripe-fallback` is in.
    """
    bill = global_apps.get_model('rebilling', 'Bill')
    # SQLite will not open a real schema editor inside the transaction pytest-django wraps each
    # test in, and none is needed: the guard returns before touching it, which is the point.
    schema_editor = SimpleNamespace(connection=connection)

    migration.add_charged_at(global_apps, schema_editor)

    assert migration._has_column(connection, bill) is True
    # Still usable, and nothing was dropped or rebuilt on the way through.
    assert Bill.objects.count() == 0


def test_failure_reason_is_still_added_by_this_migration():
    """The reason `--fake` is the wrong recovery: it would skip this column entirely.

    `failure_reason` exists only on this branch, so no other migration can have created it. A
    faked 0006 leaves the model expecting a column the database does not have.
    """
    with connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(cursor, Bill._meta.db_table)

    assert 'failure_reason' in {column.name for column in columns}
