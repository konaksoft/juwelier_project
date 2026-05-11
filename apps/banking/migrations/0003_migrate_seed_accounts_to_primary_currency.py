# Generated manually 2026-05-11
# Veri migrasyonu — Mevcut "Merkez Nakit Kasa" / "Merkez Havale/EFT Hesabı" /
# "Merkez POS Kasası" gibi otomatik oluşturulmuş TRY hesaplarını mağazanın
# StoreConfiguration.primary_currency değerine taşır.
#
# GÜVENLİK:
#   - StoreConfiguration.primary_currency='TRY' olan mağazalarda HİÇBİR şey
#     yapmaz (Türkiye pazarı korunur).
#   - Hesabın bağlı bir Payment/CashboxLedger kaydı varsa migrasyon ATLANIR
#     (geçmiş bozulmaz). Tipik kurulum sonrası hesaplar boş olduğundan bu
#     güvence yalnızca "veri varsa dokunma" kuralını uygular.
#   - Aynı mağazada zaten primary_currency'de bir CASH/BANK/POS hesabı varsa
#     TRY hesabı deaktive edilir (yeni hesabı yarattığını varsay).
#   - İsim "Merkez Nakit Kasa" → "Merkez {EUR} Nakit Kasası" gibi yeniden
#     yazılır. Custom name (kullanıcı manuel ad verdiyse) korunur.

from django.db import migrations


SEED_NAME_MAP = {
    'Merkez Nakit Kasa': 'Merkez {cur} Nakit Kasası',
    'Merkez Havale/EFT Hesabı': 'Merkez {cur} Havale/EFT Hesabı',
    'Merkez POS Kasası': 'Merkez {cur} POS Kasası',
}


def migrate_seed_accounts(apps, schema_editor):
    BankAccount = apps.get_model('banking', 'BankAccount')
    StoreConfiguration = apps.get_model('settings', 'StoreConfiguration')
    Payment = apps.get_model('process', 'Payment')
    CashboxLedger = apps.get_model('banking', 'CashboxLedger')

    for cfg in StoreConfiguration.objects.select_related('store').all():
        primary = (getattr(cfg, 'primary_currency', None) or 'EUR').upper()
        if primary == 'TRY':
            continue

        store = cfg.store
        if not store:
            continue

        try_accounts = list(BankAccount.objects.filter(
            store=store,
            currency='TRY',
            is_deleted=False,
        ))

        for acc in try_accounts:
            has_payment = Payment.objects.filter(bank_account=acc).exists()
            has_ledger = CashboxLedger.objects.filter(cashbox=acc).exists()
            if has_payment or has_ledger:
                continue

            duplicate_exists = BankAccount.objects.filter(
                store=store,
                account_type=acc.account_type,
                currency=primary,
                is_deleted=False,
            ).exclude(pk=acc.pk).exists()

            if duplicate_exists:
                acc.is_active = False
                acc.save(update_fields=['is_active'])
                continue

            new_name = SEED_NAME_MAP.get(acc.name)
            if new_name:
                acc.name = new_name.format(cur=primary)
            acc.currency = primary
            acc.save(update_fields=['name', 'currency'])


def reverse(apps, schema_editor):
    # Geri alma kasıtlı no-op — TRY'a geri dönmek anlam taşımaz.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('banking', '0002_initial'),
        ('settings', '0003_storeconfiguration_primary_currency'),
        ('process', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(migrate_seed_accounts, reverse),
    ]
