from django.db import migrations, models
import django.db.models.deletion


ARTIST_MARKERS = (
    "صفحه هنرمندی شما را دنبال کرد",
    "followed your artist profile",
    "آهنگ «",
    "liked your song",
    "آلبوم «",
    "liked your album",
    "درخواست احراز هویت هنرمندی",
    "artist verification request",
    "درخواست تسویه",
    "تسویه شما",
    "payout request",
    "your payout",
    "your song '",
    "برای انتشار «",
    "your release '",
    "انتشار «",
)


def classify_existing_notifications(apps, schema_editor):
    Notification = apps.get_model("api", "Notification")
    Artist = apps.get_model("api", "Artist")

    artist_user_by_id = dict(
        Artist.objects.exclude(user_id__isnull=True).values_list("id", "user_id")
    )
    artist_by_user_id = dict(
        Artist.objects.exclude(user_id__isnull=True).values_list("user_id", "id")
    )

    for notification in Notification.objects.all().iterator(chunk_size=1000):
        if notification.artist_id:
            notification.recipient_role = "artist"
            if not notification.user_id:
                notification.user_id = artist_user_by_id.get(notification.artist_id)
        elif notification.user_id:
            combined = f"{notification.text or ''}\n{notification.text_en or ''}".casefold()
            is_artist = any(marker.casefold() in combined for marker in ARTIST_MARKERS)
            notification.recipient_role = "artist" if is_artist else "audience"
            if is_artist:
                notification.artist_id = artist_by_user_id.get(notification.user_id)
        else:
            notification.delete()
            continue

        # Artist notifications without a linked account cannot be delivered to
        # an authenticated app. Keep no orphan row that could bypass scoping.
        if notification.recipient_role == "artist" and not notification.user_id:
            notification.delete()
            continue

        # Audience rows never carry artist ownership.
        if notification.recipient_role == "audience":
            notification.artist_id = None

        notification.save(
            update_fields=["recipient_role", "user", "artist"]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0036_playconfiguration_minimum_payout_amount"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="recipient_role",
            field=models.CharField(
                choices=[("audience", "Audience app"), ("artist", "Artist app")],
                db_index=True,
                default="audience",
                max_length=20,
            ),
        ),
        migrations.RunPython(
            classify_existing_notifications,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="notification",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notifications",
                to="api.user",
            ),
        ),
        migrations.AddConstraint(
            model_name="notification",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        recipient_role="audience",
                        artist__isnull=True,
                    )
                    | models.Q(recipient_role="artist")
                ),
                name="notification_has_role_recipient",
            ),
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(
                fields=["user", "recipient_role", "has_read", "-created_at"],
                name="notif_user_role_unread_idx",
            ),
        ),
    ]
