# Generated by Django 2.2.9 on 2020-02-06 21:24

import django.contrib.postgres.fields
from django.db import migrations, models
from main.models import Casebook, ContentNode

def assemble_ancestry(cn):
    if not cn.copy_of:
        return None
    stack = []
    curr = cn.copy_of
    while curr:
        stack = [curr.id] + stack
        curr = curr.copy_of
    return stack

def assemble_ancestry_str(cn):
    stack = assemble_ancestry(cn)
    return stack and "/".join(map(str,stack))

def repair_copy_of(app, schema):
    """
    Several casebooks have ancestry, but no copy_of. Fix that up by making the casebook a copy of the last ancestor.
    """
    for casebook in Casebook.objects.filter(ancestry__isnull=False,copy_of__isnull=True):
        casebook.copy_of = Casebook(id=int(casebook.ancestry.split("/")[-1]))
        casebook.save()

def verify_all_casebook_ancestry(app, schema):
    """
    Make sure that for all casebooks, the ancestry matches up to what can be reconstructed by following copy_of_links
    """
    for casebook in Casebook.objects.all():
        assembled = assemble_ancestry_str(casebook)
        if casebook.ancestry != assembled:
            raise("casebook.id ancestry didn't match. Expected {}, but found {}".format(assembled, casebook.ancestry))

def update_contentnode_provenance(app, schema):
    """
    contentnodes have not previously
    """
    for node in ContentNode.objects.all():
        if node.provenance:
            continue
        if node.ancestry:
            node.provenance = node.ancestry.split("/")
            node.save()
        elif node.copy_of:
            assembled = assemble_ancestry(node)
            node.provenance = assembled
            node.save()


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0035_auto_20200113_2106'),
    ]

    operations = [
        migrations.AddField(
            model_name='contentnode',
            name='provenance',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.BigIntegerField(), default=list,
                                                            size=None),
        ),
        migrations.RunPython(repair_copy_of, migrations.RunPython.noop),
        migrations.RunPython(verify_all_casebook_ancestry, migrations.RunPython.noop),
        migrations.RunPython(update_contentnode_provenance, migrations.RunPython.noop),
    ]