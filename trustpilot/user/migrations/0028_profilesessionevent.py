import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0027_review_trustpilot_username'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProfileSessionEvent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account_email', models.EmailField(blank=True, null=True)),
                ('warming_day', models.IntegerField(blank=True, null=True)),
                ('date', models.DateField()),
                ('session_status', models.CharField(
                    choices=[
                        ('alive', 'Alive'),
                        ('expired', 'Expired'),
                        ('relogin_required', 'Relogin Required'),
                    ],
                    max_length=20,
                )),
                ('session_dropped_at', models.DateTimeField(blank=True, null=True)),
                ('error_reason', models.TextField(blank=True, null=True)),
                ('notes', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('profile', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='session_events',
                    to='user.profile',
                )),
            ],
            options={
                'verbose_name': 'Profile session event',
                'verbose_name_plural': 'Profile session events',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='profilesessionevent',
            index=models.Index(fields=['profile', 'date'], name='user_profil_profile_date_idx'),
        ),
        migrations.AddIndex(
            model_name='profilesessionevent',
            index=models.Index(fields=['session_status'], name='user_profil_session_status_idx'),
        ),
        migrations.AddIndex(
            model_name='profilesessionevent',
            index=models.Index(fields=['warming_day'], name='user_profil_warming_day_idx'),
        ),
        migrations.AddIndex(
            model_name='profilesessionevent',
            index=models.Index(fields=['date'], name='user_profil_date_idx'),
        ),
        migrations.AddIndex(
            model_name='profilesessionevent',
            index=models.Index(fields=['account_email'], name='user_profil_account_email_idx'),
        ),
    ]
