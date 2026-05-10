"""FAZ 27 — Hata 3B düzeltmesi.

CustomerLedger satırları (append-only) eklendiğinde / güncellendiğinde
ilgili Customer'ın legacy `receivable_hs` / `payable_hs` alanlarını
canlı `balance_hs` property'sinden türetilen değerle senkronlar.

Bu alanlar geriye uyum için modelde korunuyor (apps/customers/models.py
satır 32–36) ancak hiçbir servis tarafından mutate edilmiyordu. Sonuç:
`templates/management/customers/detail.html` reconciliation banner'ı
(`staticSigned = receivable_hs - payable_hs` ↔ AJAX'tan gelen
`balance_hs`) her ledger hareketinden sonra "DİKKAT: Bakiye Tutmuyor"
hatası veriyordu.

Tasarım Kararları:
  • Append-only mimari etkilenmez — yalnız Customer üzerinde stored
    cache alanları güncellenir; CustomerLedger satırlarına
    dokunulmaz.
  • `transaction.on_commit` kullanılır — atomic blok rollback olursa
    cache eskiyi tutar (consistency korunur).
  • `update_fields` ile sadece iki alan yazılır — diğer Customer
    alanlarına yan etki yok.
  • `balance_hs` property'si zaten REVERSAL, CORRECTION, OFFSET,
    is_active filtresi gibi tüm append-only kuralları dahili olarak
    uyguluyor; signal sadece sonucu cache'liyor.

Anti-Loop:
  Sinyal Customer.save çağırıyor; Customer'ın kendi save signal'ı
  yok; ledger save bizim hedefimiz. Recursion riski yok.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from apps.customers.models import Customers, CustomerLedger


def _refresh_customer_balance_cache(customer_id):
    """Verilen Customer için stored receivable_hs / payable_hs alanlarını
    canlı balance_hs üzerinden günceller.

    Çağrılma noktası: CustomerLedger post_save / post_delete sonrası
    transaction.on_commit içinde."""
    try:
        customer = Customers.objects.get(pk=customer_id)
    except Customers.DoesNotExist:
        return

    balance = customer.balance_hs  # property — append-only kuralları dahil
    if balance > 0:
        new_receivable = balance
        new_payable = Decimal('0.000')
    elif balance < 0:
        new_receivable = Decimal('0.000')
        new_payable = -balance
    else:
        new_receivable = Decimal('0.000')
        new_payable = Decimal('0.000')

    # Quantize — DecimalField max 3 ondalık (Customer modelindeki tanım).
    Q3 = Decimal('0.001')
    new_receivable = new_receivable.quantize(Q3)
    new_payable = new_payable.quantize(Q3)

    # No-op kısa yolu: değişim yoksa yazma (her ledger save için
    # gereksiz UPDATE engellenir).
    if (customer.receivable_hs == new_receivable
            and customer.payable_hs == new_payable):
        return

    customer.receivable_hs = new_receivable
    customer.payable_hs = new_payable
    customer.save(update_fields=['receivable_hs', 'payable_hs'])


@receiver(post_save, sender=CustomerLedger)
def sync_customer_balance_on_ledger_save(sender, instance, **kwargs):
    """CustomerLedger satırı eklendiğinde / güncellendiğinde Customer'ın
    legacy stored bakiye alanlarını günceller."""
    cust_id = instance.customer_id
    if not cust_id:
        return
    transaction.on_commit(lambda: _refresh_customer_balance_cache(cust_id))


@receiver(post_delete, sender=CustomerLedger)
def sync_customer_balance_on_ledger_delete(sender, instance, **kwargs):
    """Hard-delete (idari operasyon / data migration) durumunda da
    cache'i tazele. Üretim akışında ledger satırları silinmez (REVERSAL
    kullanılır), bu yalnız savunma amaçlı bir hook'tur."""
    cust_id = instance.customer_id
    if not cust_id:
        return
    transaction.on_commit(lambda: _refresh_customer_balance_cache(cust_id))
