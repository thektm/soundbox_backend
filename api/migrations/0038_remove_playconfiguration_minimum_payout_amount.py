from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0037_notification_recipient_role'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='playconfiguration',
            name='minimum_payout_amount',
        ),
    ]
