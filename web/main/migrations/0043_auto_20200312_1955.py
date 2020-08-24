# Generated by Django 2.2.10 on 2020-03-12 19:55

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import main.models


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0042_historicalcase'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='contentnode',
            name='playlist_id',
        ),
        migrations.AlterField(
            model_name='contentnode',
            name='casebook',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='contents', to='main.ContentNode'),
        ),
        migrations.AlterField(
            model_name='contentnode',
            name='collaborators',
            field=models.ManyToManyField(related_name='old_casebooks', through='main.ContentCollaborator', to=settings.AUTH_USER_MODEL),
        ),
        migrations.CreateModel(
            name='Casebook',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(default='Untitled', max_length=10000)),
                ('subtitle', models.CharField(blank=True, max_length=10000, null=True)),
                ('headnote', models.TextField(blank=True, null=True)),
                ('state', models.CharField(choices=[(main.models.Casebook.LifeCycle('Fresh'), 'Fresh'), (main.models.Casebook.LifeCycle('Clone'), 'Clone'), (main.models.Casebook.LifeCycle('Draft'), 'Draft'), (main.models.Casebook.LifeCycle('Public'), 'Public'), (main.models.Casebook.LifeCycle('Archived'), 'Archived')], max_length=10)),
                ('draft', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='draft_of', to='main.Casebook')),
                ('old_casebook', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='replacement_casebook', to='main.ContentNode')),
            ],
            options={
                'managed': True,
            },
        ),
    ]