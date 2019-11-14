# -*- coding: utf-8 -*-
# Generated by Django 1.11.23 on 2019-10-08 13:30
from __future__ import unicode_literals

from django.db import migrations


def forwards_func(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.using(db_alias).filter(codename="submit_testjob").delete()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("lava_scheduler_app", "0044_reintroduce_cancel_resubmit_permission")
    ]

    operations = [
        migrations.AlterModelOptions(
            name="testjob",
            options={
                "permissions": (
                    ("cancel_resubmit_testjob", "Can cancel or resubmit test jobs"),
                )
            },
        ),
        migrations.RunPython(forwards_func, noop),
    ]