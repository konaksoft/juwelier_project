# Generated manually for FAZ 1 (Yerel Saat & Yerelleştirme)
# Almanya/Avrupa pazarı hazırlığı: StoreConfiguration'a timezone_code alanı.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settings', '0003_storeconfiguration_primary_currency'),
    ]

    operations = [
        migrations.AddField(
            model_name='storeconfiguration',
            name='timezone_code',
            field=models.CharField(
                choices=[
                    ('Europe/Berlin', 'Berlin (UTC+1 / UTC+2 DST)'),
                    ('Europe/Vienna', 'Wien (UTC+1 / UTC+2 DST)'),
                    ('Europe/Zurich', 'Zürich (UTC+1 / UTC+2 DST)'),
                    ('Europe/Paris', 'Paris (UTC+1 / UTC+2 DST)'),
                    ('Europe/Amsterdam', 'Amsterdam (UTC+1 / UTC+2 DST)'),
                    ('Europe/Brussels', 'Brüssel (UTC+1 / UTC+2 DST)'),
                    ('Europe/London', 'London (UTC+0 / UTC+1 DST)'),
                    ('Europe/Istanbul', 'İstanbul (UTC+3)'),
                ],
                default='Europe/Berlin',
                help_text=(
                    'Bu mağazanın yerel saat dilimi. Rapor/ekstre/fiş zaman damgaları '
                    'bu saat dilimine göre gösterilir. Sistem genel TIME_ZONE varsayılanı '
                    'Europe/Berlin (Almanya). Çoklu lokasyon kullanmıyorsanız değiştirmeyin.'
                ),
                max_length=64,
                verbose_name='Mağaza Saat Dilimi',
            ),
        ),
    ]
