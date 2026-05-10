from django.db import migrations


def disable_ziynet(apps, schema_editor):
    Categories = apps.get_model("categories", "Categories")
    Categories.objects.filter(name="Ziynet").update(is_active=False)


def enable_ziynet(apps, schema_editor):
    Categories = apps.get_model("categories", "Categories")
    Categories.objects.filter(name="Ziynet").update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("categories", "0002_seed_categories"),
    ]

    operations = [
        migrations.RunPython(disable_ziynet, reverse_code=enable_ziynet),
    ]
