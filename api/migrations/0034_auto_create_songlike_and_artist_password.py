from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0001_initial'),
    ]

    operations = [
        # Add artist_password field to existing User model
        migrations.AddField(
            model_name='user',
            name='artist_password',
            field=models.CharField(max_length=255, null=True, blank=True),
        ),

        # Create SongLike table which was missing in the DB
        migrations.CreateModel(
            name='SongLike',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=models.CASCADE, to='api.user')),
                ('song', models.ForeignKey(on_delete=models.CASCADE, to='api.song')),
            ],
            options={
                'ordering': ['-created_at'],
                'unique_together': {('user', 'song')},
            },
        ),
    ]
