# Generated by Django 2.2.13 on 2020-08-25 13:46

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0066_auto_20200824_1702'),
    ]

    operations = [
        migrations.AlterField(
            model_name='historicaltextblock',
            name='content',
            field=models.CharField(blank=True, max_length=5242880, null=True),
        ),
        migrations.AlterField(
            model_name='textblock',
            name='content',
            field=models.CharField(blank=True, max_length=5242880, null=True),
        ),
    ]