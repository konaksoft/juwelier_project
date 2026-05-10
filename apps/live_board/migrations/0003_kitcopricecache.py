import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('live_board', '0002_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='KitcoPriceCache',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('metal_type', models.CharField(choices=[('GOLD', 'Altın'), ('SILVER', 'Gümüş'), ('PLATINUM', 'Platin'), ('PALLADIUM', 'Paladyum'), ('RHODIUM', 'Rodyum')], max_length=16, verbose_name='Metal')),
                ('currency', models.CharField(choices=[('USD', 'USD'), ('EUR', 'EUR'), ('GBP', 'GBP'), ('CAD', 'CAD'), ('AUD', 'AUD'), ('JPY', 'JPY'), ('CHF', 'CHF')], max_length=8, verbose_name='Para Birimi')),
                ('unit', models.CharField(choices=[('OZ', 'Troy Ons'), ('GRAM', 'Gram')], default='OZ', max_length=8, verbose_name='Birim')),
                ('bid_price', models.DecimalField(decimal_places=4, help_text="Kitco'dan çekilen ham spot alış fiyatı. İşçilik ve kâr marjı DAHİL DEĞİLDİR.", max_digits=14, verbose_name='Alış Fiyatı (Bid)')),
                ('ask_price', models.DecimalField(decimal_places=4, help_text="Kitco'dan çekilen ham spot satış fiyatı. İşçilik ve kâr marjı DAHİL DEĞİLDİR.", max_digits=14, verbose_name='Satış Fiyatı (Ask)')),
                ('source_timestamp', models.DateTimeField(blank=True, help_text="Kitco'nun ham yanıtındaki piyasa zamanı (originalTime alanı).", null=True, verbose_name='Kaynak Zaman Damgası')),
                ('last_updated', models.DateTimeField(auto_now=True, verbose_name='Son Güncelleme (DB)')),
            ],
            options={
                'verbose_name': 'Kitco Fiyat Önbellek Kaydı',
                'verbose_name_plural': 'Kitco Fiyat Önbellek Kayıtları',
                'db_table': 'KitcoPriceCache',
            },
        ),
        migrations.AddIndex(
            model_name='kitcopricecache',
            index=models.Index(fields=['metal_type', 'currency', 'unit'], name='idx_kitco_lookup'),
        ),
        migrations.AddConstraint(
            model_name='kitcopricecache',
            constraint=models.UniqueConstraint(fields=('metal_type', 'currency', 'unit'), name='uniq_kitco_metal_ccy_unit'),
        ),
    ]
