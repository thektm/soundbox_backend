import django.core.validators
from decimal import Decimal
from django.db import migrations, models


RESTORE_COLUMN_SQL = r'''
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'api_playconfiguration'
          AND column_name = 'minimum_payout_amount'
    ) THEN
        ALTER TABLE "api_playconfiguration"
        ADD COLUMN "minimum_payout_amount" numeric(15, 2);
    END IF;
END
$$;

UPDATE "api_playconfiguration"
SET "minimum_payout_amount" = 0.01
WHERE "minimum_payout_amount" IS NULL;

ALTER TABLE "api_playconfiguration"
ALTER COLUMN "minimum_payout_amount" SET NOT NULL;

ALTER TABLE "api_playconfiguration"
ALTER COLUMN "minimum_payout_amount" DROP DEFAULT;
'''


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0038_remove_playconfiguration_minimum_payout_amount'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=RESTORE_COLUMN_SQL,
                    reverse_sql=(
                        'ALTER TABLE "api_playconfiguration" '
                        'DROP COLUMN IF EXISTS "minimum_payout_amount";'
                    ),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='playconfiguration',
                    name='minimum_payout_amount',
                    field=models.DecimalField(
                        decimal_places=2,
                        default=Decimal('0.01'),
                        help_text=(
                            'Minimum withdrawable artist balance required to '
                            'submit a payout request.'
                        ),
                        max_digits=15,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal('0.01'))
                        ],
                    ),
                ),
            ],
        ),
    ]
