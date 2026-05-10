import uuid
from django.db import migrations


CATEGORIES = [
    {"id": "fb5a88bb-4776-40ad-bfc6-e95fa8f0137d", "name": "Döviz",            "order": 1},
    {"id": "22315da9-62a3-4282-bd85-d463d1586867", "name": "Ziynet",            "order": 0},
    {"id": "25bde118-4713-4986-a6fe-3435af6f8f88", "name": "Barkodlu Ürünler", "order": 2},
    {"id": "25ade119-4713-4986-a6fe-3435af6f8f88", "name": "Bilezik",           "order": 4},
    {"id": "25ade118-4713-4986-a6fe-3435af6f8f88", "name": "Saat",              "order": 7},
    {"id": "c2097669-52a6-4642-aa88-aaf1e5b80a56", "name": "Hurda",             "order": 3},
    {"id": "8f77a942-94ee-43ca-b892-97f7337cbad0", "name": "Altın",             "order": 5},
    {"id": "1f7a0eea-0fcc-4338-a3fa-bf850e8a63f5", "name": "Pırlanta",          "order": 6},
]


def seed_categories(apps, schema_editor):
    Categories = apps.get_model("categories", "Categories")
    for row in CATEGORIES:
        Categories.objects.update_or_create(
            id=uuid.UUID(row["id"]),
            defaults={
                "name":       row["name"],
                "order":      row["order"],
                "is_active":  True,
                "is_deleted": False,
            },
        )


def unseed_categories(apps, schema_editor):
    Categories = apps.get_model("categories", "Categories")
    ids = [uuid.UUID(row["id"]) for row in CATEGORIES]
    Categories.objects.filter(id__in=ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("categories", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_categories, reverse_code=unseed_categories),
    ]
