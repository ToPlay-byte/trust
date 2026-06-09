from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0028_profilesessionevent'),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='profilesessionevent',
            name='user_profil_account_email_idx',
        ),
        migrations.RemoveField(
            model_name='profilesessionevent',
            name='account_email',
        ),
    ]
