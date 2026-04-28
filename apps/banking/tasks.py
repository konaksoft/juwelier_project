# ============================================================================
# DOSYA: apps/banking/tasks.py
# KONUM: Kuyum Plus (jewelery_project)
#
# Celery görevleri — Banka hareketleri otomatik delta fetch.
#
# İÇERİK:
#   fetch_latest_bank_transactions_task:
#       - Her mağaza için lokal DB'deki en son hareket tarihini bulur.
#       - O tarihten bugüne kadar olan aralığı e-Süreç'ten çeker (delta fetch).
#       - DB boşsa son 7 günü çeker.
#       - Celery Beat ile her 30 dakikada bir çalıştırılır.
# ============================================================================

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

log = logging.getLogger(__name__)


@shared_task(
    name='banking.fetch_latest_bank_transactions',
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    soft_time_limit=300,
    time_limit=360,
    acks_late=True,
)
def fetch_latest_bank_transactions_task(self):
    """
    Delta Fetch: Her mağaza için en son hareket tarihinden bugüne kadar
    olan banka hareketlerini e-Süreç'ten çeker.

    Mantık:
      1. Aktif e-Süreç entegrasyonu olan tüm mağazaları bul
      2. Her mağaza için:
         a. BankTransaction.objects.filter(store=store).order_by('-doc_date').first()
            ile en son hareketin tarihini al
         b. Eğer DB boşsa → son 7 günü çek (start_date = today - 7)
         c. Eğer kayıt varsa → start_date = son hareket tarihi
         d. end_date = bugün
      3. ESurecBankingClient.fetch_transactions() ile e-Süreç'i çağır
      4. Çekilen hareketler için otomatik cari eşleştirme yap

    Celery Beat Schedule: Her 30 dakikada bir
    """
    from apps.banking.models import BankTransaction, EsurecTenantCredential
    from apps.banking.services import ESurecBankingClient, CariMatchingService

    # Aktif entegrasyonu olan mağazaları bul
    active_creds = EsurecTenantCredential.objects.filter(
        is_active=True,
    ).select_related('store')

    if not active_creds.exists():
        log.info("[Banking Task] Aktif e-Süreç entegrasyonu olan mağaza bulunamadı. Görev atlanıyor.")
        return {'status': 'skipped', 'reason': 'no_active_stores'}

    today = timezone.now().date()
    end_date_str = today.strftime('%Y-%m-%d')

    total_synced = 0
    total_matched = 0
    total_reconciled = 0
    store_results = []

    client = ESurecBankingClient()

    for cred in active_creds:
        store = cred.store
        if not store:
            continue

        # KP-13: Aktif kullanıcısı olmayan mağazaları atla
        from apps.accounts.models import Users
        authorized_user = Users.objects.filter(store=store, is_active=True).first()
        if not authorized_user:
            log.warning(f"[Banking Task] {store}: Aktif kullanıcı yok, mağaza atlanıyor.")
            continue

        store_label = getattr(store, 'name', '') or str(store.id)

        try:
            # Son hareketin tarihini bul
            last_txn = (
                BankTransaction.objects
                .filter(store=store)
                .order_by('-doc_date')
                .values_list('doc_date', flat=True)
                .first()
            )

            if last_txn:
                # Son hareket tarihinden itibaren çek
                start_date = last_txn.date() if hasattr(last_txn, 'date') else last_txn
                start_date_str = start_date.strftime('%Y-%m-%d')
            else:
                # DB boş — son 7 günü çek
                start_date_str = (today - timedelta(days=7)).strftime('%Y-%m-%d')

            log.info(
                "[Banking Task] Delta fetch başlıyor: store=%s, start=%s, end=%s",
                store_label, start_date_str, end_date_str,
            )

            # e-Süreç'ten çek
            sync_result = client.fetch_transactions(
                store=store,
                start_date=start_date_str,
                end_date=end_date_str,
                iban='',
                only_new=False,
            )

            synced_count = sync_result.get('count', 0)
            total_synced += synced_count

            # Başarılı çekimden sonra otomatik eşleştirme
            matched_count = 0
            if sync_result.get('result') and synced_count > 0:
                try:
                    matcher = CariMatchingService(store=store)
                    match_result = matcher.auto_match_pending()
                    matched_count = match_result.get('matched', 0)
                    total_matched += matched_count
                except Exception as match_err:
                    log.warning(
                        "[Banking Task] Otomatik eşleştirme hatası (store=%s): %s",
                        store_label, match_err,
                    )

            # NOT: Mutabakat motoru uyku modunda (Faz 6 karari).
            # ReconciliationService.reconcile_all_pending() cagrilmiyor.
            # Gerektiginde tekrar aktif edilebilir.

            store_results.append({
                'store': store_label,
                'synced': synced_count,
                'matched': matched_count,
                'reconciled': 0,
                'success': sync_result.get('result', False),
                'msg': sync_result.get('msg', ''),
            })

            if not sync_result.get('result'):
                log.warning(
                    "[Banking Task] Fetch başarısız (store=%s): %s",
                    store_label, sync_result.get('msg', ''),
                )

        except Exception as e:
            log.exception(
                "[Banking Task] Store işleme hatası (store=%s): %s",
                store_label, e,
            )
            store_results.append({
                'store': store_label,
                'synced': 0,
                'matched': 0,
                'success': False,
                'msg': f'{type(e).__name__}: İşleme hatası',
            })

    log.info(
        "[Banking Task] Tamamlandı: %d mağaza, %d hareket çekildi, %d otomatik eşleştirildi, %d mutabakat.",
        len(store_results), total_synced, total_matched, total_reconciled,
    )

    return {
        'status': 'completed',
        'total_stores': len(store_results),
        'total_synced': total_synced,
        'total_matched': total_matched,
        'total_reconciled': total_reconciled,
        'details': store_results,
    }
