"""Add `charged_at` and `failure_reason` to a bill.

`charged_at` is added defensively, because this is the second branch to introduce a column by
that name. `feat/ghl-billing-with-stripe-fallback` added it in its own 0006
(`0006_bill_charged_at_bill_ghl_invoice_id_and_more`), so a server that has ever run that branch
already has the column in `rebilling_bill` while `django_migrations` holds no row under *this*
migration's name. Django therefore re-runs it, and a plain `AddField` fails there with
`duplicate column name: charged_at`.

`SeparateDatabaseAndState` keeps the two halves honest: Django's model state always gains the
field, so the model and the recorded schema agree whichever branch the database came from, while
only the database half is conditional.

`failure_reason` needs no guard. It exists on this branch alone, so nothing can have added it
already, and a guard there would be dead code hiding a real error.
"""

from django.db import migrations, models

COLUMN = 'charged_at'


def _has_column(connection, model) -> bool:
    with connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(cursor, model._meta.db_table)
    return any(column.name == COLUMN for column in columns)


def _charged_at_field():
    """The field as this migration declares it, built here rather than read off the model.

    `SeparateDatabaseAndState` hands its database operations the state from *before* its state
    operations ran, so the historical `Bill` reachable from here has no `charged_at` on it to
    look up. Declaring the field is also the more honest thing for a migration to do: it says
    what it adds instead of trusting whatever the model happens to say later.
    """
    field = models.DateTimeField(null=True, blank=True)
    field.set_attributes_from_name(COLUMN)
    return field


def add_charged_at(apps, schema_editor):
    """Add the column only where it is missing."""
    bill = apps.get_model('rebilling', 'Bill')
    if _has_column(schema_editor.connection, bill):
        return
    schema_editor.add_field(bill, _charged_at_field())


def remove_charged_at(apps, schema_editor):
    """Drop it only where it is present, so unapplying is as safe as applying."""
    bill = apps.get_model('rebilling', 'Bill')
    if not _has_column(schema_editor.connection, bill):
        return
    schema_editor.remove_field(bill, _charged_at_field())


class Migration(migrations.Migration):
    dependencies = [
        ('rebilling', '0005_bill_paid_at_bill_status_bill_stripe_invoice_id_and_more'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='bill',
                    name='charged_at',
                    field=models.DateTimeField(blank=True, null=True),
                ),
            ],
            database_operations=[
                migrations.RunPython(add_charged_at, remove_charged_at),
            ],
        ),
        migrations.AddField(
            model_name='bill',
            name='failure_reason',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
