# -*- coding: utf-8 -*-
# Generated by Django 1.9.7 on 2016-08-29 19:52
from __future__ import unicode_literals

from django.db import migrations
import share.robot


class Migration(migrations.Migration):

    dependencies = [
        ('share', '0001_initial'),
        ('djcelery', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(
            code=share.robot.RobotUserMigration('io.psyarxiv'),
        ),
        migrations.RunPython(
            code=share.robot.RobotOauthTokenMigration('io.psyarxiv'),
        ),
        migrations.RunPython(
            code=share.robot.RobotScheduleMigration('io.psyarxiv'),
        ),
    ]
