from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0034_user_artist_password'),
    ]

    operations = [
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
            },
        ),
        migrations.AlterUniqueTogether(
            name='songlike',
            unique_together={('user', 'song')},
        ),
    ]
