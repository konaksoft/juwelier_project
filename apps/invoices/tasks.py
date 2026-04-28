# ============================================================================
# DOSYA: apps/invoices/tasks.py
# KONUM: Kuyum Plus projesi içinde
# AÇIKLAMA: Celery mesaj kuyruğu ile e-Süreç entegrasyon görevleri
#
# DÜZELTMEv2 — KRİTİK HATALAR GİDERİLDİ:
#   1. autoretry_for=(Exception,) KALDIRILDI.
#      → Her exception'da otomatik retry non-retryable hataları da tekrarlıyordu.
#      → Şimdi sadece retryable hatalarda (bağlantı, timeout, 429, 500+) retry.
#
#   2. Başarılı e-Süreç gönderiminde Invoice.status güncelleniyor.
#      → QUEUED → DRAFT (gib_status_code='10', e-Süreç taslağı oluşturuldu)
#      → Artık "İşleniyor" takılması olmaz.
#
#   3. Her hata durumunda (API hatası + exception) Invoice.status = ERROR
#      + gib_error alanı güncelleniyor.
#
#   4. send_invoice_to_gib_task ESUREC_ID yoksa önce taslak oluşturuyor.
#      → "Direkt GİB'e Gönder" artık tek tıkla çalışır.
#
# AKIŞ:
#   DRAFT → QUEUED (view tarafından) → Task başlar
#   Başarı  → DRAFT (gib_status_code='10')  [GİB gönderimini bekler]
#   Hata    → ERROR (gib_error dolu)
#   GİB OK  → SENT (gib_status_code='100')
#   GİB Err → ERROR (gib_error dolu)
# ============================================================================

import logging
import traceback
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from celery import shared_task
from django.db import transaction
from django.utils import timezone

log = logging.getLogger(__name__)


# ======================================================================
# NİHAİ TÜKETİCİ FALLBACK SABİTLERİ (KP-13)
# ======================================================================
# esurec_views.py ile senkron. Değiştirilecekse iki dosyada da güncellenmeli.
# (Tek kaynak yapmak için ileride apps/invoices/constants.py'e taşınabilir.)
# ======================================================================

NIHAI_TUKETICI_TCKN = '11111111111'
NIHAI_TUKETICI_VKN_YABANCI = '22222222222'
NIHAI_TUKETICI_THRESHOLD_TL = Decimal('30000.00')


def _invoice_total_for_threshold(invoice) -> Decimal:
    """Faturanın threshold kontrolü için toplam tutarı."""
    for field in ('grand_total', 'subtotal'):
        try:
            val = getattr(invoice, field, None)
            if val is not None:
                return Decimal(str(val))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return Decimal('0')


# ======================================================================
# YARDIMCI
# ======================================================================

# ── KP-07: ESUREC_ID artık InvoiceSyncLog üzerinden okunur/yazılır ──

def _extract_esurec_id_from_notes(notes: str) -> str:
    """Göç dönemi fallback — eski notes etiketinden oku."""
    for line in (notes or '').split('\n'):
        line = line.strip()
        if line.startswith('ESUREC_ID:'):
            return line.replace('ESUREC_ID:', '').strip()
    return ''


def _get_esurec_id(invoice) -> str:
    """InvoiceSyncLog'dan esurec_invoice_id çeker. Fallback: notes etiketi."""
    from apps.invoices.models import InvoiceSyncLog
    sync_log = InvoiceSyncLog.objects.filter(
        invoice=invoice,
        action=InvoiceSyncLog.Action.SEND_TO_ESUREC,
        status=InvoiceSyncLog.Status.SUCCESS,
        esurec_invoice_id__isnull=False,
    ).exclude(esurec_invoice_id='').order_by('-created_at').first()
    if sync_log:
        return sync_log.esurec_invoice_id
    return _extract_esurec_id_from_notes(getattr(invoice, 'notes', ''))


def _set_esurec_id(invoice, esurec_id: str, store=None):
    """InvoiceSyncLog'a esurec_invoice_id yazar. Notes'a dokunmaz."""
    from apps.invoices.models import InvoiceSyncLog
    if not esurec_id:
        return
    InvoiceSyncLog.objects.update_or_create(
        invoice=invoice,
        action=InvoiceSyncLog.Action.SEND_TO_ESUREC,
        defaults={
            'status': InvoiceSyncLog.Status.SUCCESS,
            'esurec_invoice_id': esurec_id,
            'store': store or invoice.store,
        }
    )


def _get_or_create_sync_log(invoice, store_id, action, task_id, attempt):
    """
    View tarafından oluşturulan QUEUED log'u bulur ve PROCESSING'e günceller.
    Eğer QUEUED log yoksa yeni bir PROCESSING log yaratır.
    """
    from apps.invoices.models import InvoiceSyncLog

    existing = InvoiceSyncLog.objects.filter(
        invoice=invoice,
        store_id=store_id,
        action=action,
        status__in=['QUEUED', 'RETRYING'],
    ).order_by('-created_at').first()

    if existing:
        existing.status = 'PROCESSING'
        existing.task_id = task_id
        existing.attempt = attempt
        existing.save(update_fields=['status', 'task_id', 'attempt', 'updated_at'])
        return existing

    return InvoiceSyncLog.objects.create(
        invoice=invoice,
        store_id=store_id,
        action=action,
        status='PROCESSING',
        task_id=task_id,
        attempt=attempt,
    )


def _sanitize_gib_error_task(raw_error: str) -> str:
    """
    Celery task'ları için hata mesajı sanitizer.
    Teknik hata mesajlarını kullanıcı dostu formata çevirir.
    Ham hata mesajı zaten logger tarafından ayrıca kaydedilir.
    """
    if not raw_error:
        return ''
    raw_lower = raw_error.lower()
    _patterns = [
        # ── Entegratör Seri / Belge Numarası Hataları (MySoft Error Codes) ──
        # [00019]: "Belge için uygun alternatif seri bulunamadı / Belge no üretilemedi"
        # Kök neden: e-Süreç tarafında bu dealer için aktif veya varsayılan InvoiceSeries
        # kaydı yok, ya da series_type rakamsal ('1'/'2') uyuşmazlığı var.
        ('00019', 'Entegratör seri tanımı eksik: Bu firma için e-Süreç panelinde E-Fatura/E-Arşiv serisi (varsayılan olarak) tanımlı değil. e-Süreç → Firma Ayarları → Fatura Serileri bölümünden kontrol edin.'),
        ('uygun alternatif seri', 'Entegratör seri tanımı eksik: Bu firma için e-Süreç panelinde E-Fatura/E-Arşiv serisi (varsayılan olarak) tanımlı değil.'),
        ('belge no üretilemedi', 'Entegratör belge numarası üretemedi. Seri tanımı eksik olabilir — e-Süreç → Firma Ayarları → Fatura Serileri.'),
        ('alternatif belge numarası', 'Entegratör alternatif seri bulamadı. Seri tanımı eksik olabilir.'),
        ('eski tarih', 'Fatura tarihi çok eski. "Düzenlemeye Al" ile tarihi güncelleyin.'),
        # DB constraint hataları
        ('null value in column', 'Veri gönderiminde eksik alan hatası. Fatura bilgilerini kontrol edin.'),
        ('violates not-null constraint', 'Veri gönderiminde eksik alan hatası. Fatura bilgilerini kontrol edin.'),
        ('violates unique constraint', 'Mükerrer kayıt engellendi.'),
        # Bağlantı / sunucu hataları
        ('connectionerror', 'e-Süreç sunucusuna bağlanılamadı. Tekrar denenecek.'),
        ('timeout', 'e-Süreç zaman aşımı. Tekrar denenecek.'),
        ('bağlanılamadı', 'e-Süreç sunucusuna bağlanılamadı. Tekrar denenecek.'),
        ('401', 'e-Süreç kimlik doğrulama hatası.'),
        ('429', 'Çok fazla istek. Tekrar denenecek.'),
        ('500', 'e-Süreç sunucu hatası. Tekrar denenecek.'),
        # Genel entegratör / numaratör hataları
        ('numaratör', 'Fatura numarası atanamadı. Seri tanımı kontrol edilmeli.'),
        ('prefix', 'Fatura seri ön eki tanımlı değil. Seri ayarlarını kontrol edin.'),
        ('seri bulunamadı', 'Uygun fatura serisi bulunamadı. e-Süreç → Firma Ayarları → Fatura Serileri.'),
        ('mysoft', 'Entegratör ile iletişim hatası.'),
        ('entegratör', 'Entegratör ile iletişim hatası.'),
    ]
    for pattern, friendly in _patterns:
        if pattern.lower() in raw_lower:
            return friendly
    if len(raw_error) <= 100 and 'traceback' not in raw_lower and 'sql' not in raw_lower:
        return raw_error
    return 'İşlem sırasında hata oluştu. Tekrar deneyin veya yöneticinize başvurun.'


def _create_activity_log(invoice_id, store_id, level, event, user_message, trace_id=None):
    """InvoiceActivityLog kaydı oluşturur. Exception fırlatmaz."""
    try:
        from apps.invoices.models import InvoiceActivityLog
        InvoiceActivityLog.objects.create(
            trace_id=trace_id,
            invoice_id=invoice_id,
            store_id=store_id,
            level=level,
            event=event,
            user_message=(user_message or '')[:500],
        )
    except Exception as exc:
        log.warning(f"_create_activity_log yazılamadı: {exc}")


def _fail_invoice_and_log(invoice_id, error_msg, sync_log=None, error_detail=None):
    """
    Fatura statüsünü ERROR'a çeker ve sync_log'u FAILED yapar.
    Her hata durumunda çağrılır — duplicate-safe.
    Kullanıcı dostu hata mesajı gib_error'a yazılır, ham mesaj loglanır.
    InvoiceActivityLog kaydı oluşturulur (kullanıcıya gösterilecek).
    """
    from apps.invoices.models import Invoice
    import uuid as _uuid

    # Ham hatayı logla
    log.error(f"_fail_invoice_and_log: invoice={invoice_id}, raw_error={error_msg[:500]}")

    # Kullanıcı dostu mesajı DB'ye yaz
    friendly_msg = _sanitize_gib_error_task(error_msg or 'Bilinmeyen hata')[:500]

    store_id = None
    try:
        inv = Invoice.objects.filter(id=invoice_id).only('store_id').first()
        if inv:
            store_id = inv.store_id
        Invoice.objects.filter(id=invoice_id).update(
            status='ERROR',
            gib_error=friendly_msg,
        )
    except Exception as exc:
        log.error(f"_fail_invoice_and_log Invoice güncellenemedi ({invoice_id}): {exc}")

    if sync_log:
        try:
            sync_log.status = 'FAILED'
            sync_log.error_message = (error_msg or '')[:500]
            if error_detail:
                sync_log.error_detail = str(error_detail)[:2000]
            sync_log.save(update_fields=['status', 'error_message', 'error_detail', 'updated_at'])
        except Exception as exc:
            log.error(f"_fail_invoice_and_log SyncLog güncellenemedi: {exc}")

    # InvoiceActivityLog kaydı — kullanıcıya gösterilecek hata mesajı
    _create_activity_log(
        invoice_id=invoice_id,
        store_id=store_id,
        level='ERROR',
        event='GIB_ERROR',
        user_message=friendly_msg,
        trace_id=_uuid.uuid4(),
    )


def _mark_retrying(sync_log, error_msg):
    """Sync log'u RETRYING olarak işaretle (retry öncesi)."""
    if sync_log:
        try:
            sync_log.status = 'RETRYING'
            sync_log.error_message = (error_msg or '')[:500]
            sync_log.save(update_fields=['status', 'error_message', 'updated_at'])
        except Exception:
            pass


# ======================================================================
# 1. e-SÜREÇ'E TASLAK GÖNDER (ASYNC)
# ======================================================================

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    retry_backoff=True,
    acks_late=True,
    name='invoices.send_to_esurec'
)
def send_invoice_to_esurec_task(self, invoice_id: str, store_id: str):
    """
    Faturayı e-Süreç'e taslak olarak gönderir.

    autoretry_for KULLANILMAZ — sadece retryable hatalarda
    (bağlantı, timeout, 429, 500+) self.retry() çağrılır.

    Başarı  → Invoice: DRAFT, gib_status_code='10'
    Hata    → Invoice: ERROR, gib_error dolu
    """
    from apps.invoices.models import Invoice, InvoiceSyncLog
    from apps.invoices.esurec_client import ESurecClient
    from apps.invoices.esurec_serializers import (
        serialize_invoice_for_esurec,
        determine_invoice_scenario,
    )

    sync_log = None
    try:
        invoice = Invoice.objects.prefetch_related('items').get(
            id=invoice_id, is_deleted=False
        )

        # KP-08: Idempotency guard — aynı fatura için zaten PROCESSING varsa atla
        already_processing = InvoiceSyncLog.objects.filter(
            invoice=invoice,
            action='SEND_TO_ESUREC',
            status='PROCESSING',
        ).exists()
        if already_processing and self.request.retries == 0:
            log.warning(f"[ESUREC] Fatura {invoice_id} zaten PROCESSING, çift gönderim engellendi.")
            return {'result': True, 'skipped': True, 'reason': 'already_processing'}

        sync_log = _get_or_create_sync_log(
            invoice=invoice,
            store_id=store_id,
            action='SEND_TO_ESUREC',
            task_id=self.request.id,
            attempt=self.request.retries + 1,
        )

        # Zaten gönderilmişse atla
        existing_id = _get_esurec_id(invoice)
        if existing_id:
            sync_log.status = 'SKIPPED'
            sync_log.response_data = {'msg': 'Zaten gönderilmiş', 'esurec_id': existing_id}
            sync_log.save(update_fields=['status', 'response_data', 'updated_at'])
            # Statü düzelt (QUEUED'da kalmışsa)
            if invoice.status == 'QUEUED':
                Invoice.objects.filter(id=invoice_id).update(
                    status='DRAFT',
                    gib_status_code='10',
                    gib_status_desc='e-Süreç taslağı (zaten mevcut)',
                )
            return {'result': True, 'skipped': True, 'esurec_id': existing_id}

        client = ESurecClient()

        # Mükellef kontrolü: e-Fatura mı e-Arşiv mi?
        scenario, is_einvoice, gib_info = determine_invoice_scenario(invoice, esurec_client=client)

        with transaction.atomic():
            invoice.scenario = scenario
            invoice.doc_class = 'E_INVOICE' if is_einvoice else 'E_ARCHIVE'
            invoice.is_einvoice = is_einvoice
            invoice.save(update_fields=['scenario', 'doc_class', 'is_einvoice', 'updated_at'])

        # Serialize ve gönder
        payload = serialize_invoice_for_esurec(
            invoice, scenario=scenario, is_einvoice=is_einvoice, gib_info=gib_info
        )
        api_resp = client.send_invoice(payload)

        if api_resp.get('result'):
            # ---- BAŞARI ----
            esurec_id = api_resp.get('esurec_invoice_id', '')
            with transaction.atomic():
                inv = Invoice.objects.select_for_update().get(id=invoice_id)
                if esurec_id:
                    _set_esurec_id(inv, esurec_id)
                inv.status = 'DRAFT'
                inv.gib_status_code = str(api_resp.get('gib_status_code', '10'))
                inv.gib_status_desc = 'e-Süreç taslağı oluşturuldu'
                inv.gib_error = ''
                inv.save(update_fields=[
                    'status', 'gib_status_code', 'gib_status_desc',
                    'gib_error', 'updated_at',
                ])

            sync_log.status = 'SUCCESS'
            sync_log.response_data = api_resp
            sync_log.esurec_invoice_id = esurec_id
            sync_log.save(update_fields=[
                'status', 'response_data', 'esurec_invoice_id', 'updated_at',
            ])

            log.info(
                f"[ESUREC] Fatura {invoice.invoice_no} → e-Süreç gönderildi. "
                f"ESUREC_ID: {esurec_id}"
            )

            _create_activity_log(
                invoice_id=invoice_id,
                store_id=store_id,
                level='INFO',
                event='SEND_ATTEMPT',
                user_message=f'Fatura "{invoice.invoice_no}" e-Süreç taslağı oluşturuldu.',
            )

            return {'result': True, 'esurec_id': esurec_id}

        else:
            # ---- API HATASI ----
            error_msg = api_resp.get('error_msg') or api_resp.get('message') or 'Bilinmeyen hata'
            error_code = api_resp.get('error_code', '') or ''
            retryable = api_resp.get('retryable', False)

            if retryable and self.request.retries < self.max_retries:
                _mark_retrying(sync_log, error_msg)
                log.warning(
                    f"[ESUREC] Fatura {invoice.invoice_no} → retryable hata "
                    f"(deneme {self.request.retries + 1}/{self.max_retries}): {error_msg}"
                )
                raise self.retry(exc=Exception(error_msg))

            _fail_invoice_and_log(invoice_id, error_msg, sync_log)
            sync_log.response_data = api_resp
            sync_log.save(update_fields=['response_data', 'updated_at'])

            log.warning(
                f"[ESUREC] Fatura {invoice.invoice_no} → kalıcı hata "
                f"[{error_code}]: {error_msg}"
            )
            return {'result': False, 'error_msg': error_msg, 'error_code': error_code}

    except Invoice.DoesNotExist:
        if sync_log:
            sync_log.status = 'FAILED'
            sync_log.error_message = 'Fatura bulunamadı'
            sync_log.save(update_fields=['status', 'error_message', 'updated_at'])
        return {'result': False, 'error_msg': 'Fatura bulunamadı'}

    except Exception as e:
        error_str = str(e)[:500]
        error_detail = traceback.format_exc()

        is_retryable = any(
            kw in error_str.lower()
            for kw in ['timeout', 'bağlanılamadı', 'connection', 'connectionerror']
        )

        if is_retryable and self.request.retries < self.max_retries:
            _mark_retrying(sync_log, error_str)
            log.warning(
                f"[ESUREC] Fatura {invoice_id} → exception retry "
                f"(deneme {self.request.retries + 1}/{self.max_retries}): {error_str}"
            )
            raise self.retry(exc=e)

        _fail_invoice_and_log(invoice_id, error_str, sync_log, error_detail)
        log.exception(f"[ESUREC] Fatura {invoice_id} → kalıcı exception: {e}")
        return {'result': False, 'error_msg': error_str}


# ======================================================================
# 2. GİB'E GÖNDER (ASYNC) — İKİ ADIMLI AKIŞ
# ======================================================================

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    retry_backoff=True,
    acks_late=True,
    name='invoices.send_to_gib'
)
def send_invoice_to_gib_task(self, invoice_id: str, store_id: str):
    """
    e-Süreç'teki faturayı GİB'e gönderir.

    ESUREC_ID yoksa ÖNCE taslak oluşturur (iki adımlı akış):
      1. POST /api/v1/external/invoice/send/       → taslak oluştur → esurec_id al
      2. POST /api/v1/external/invoice/send-to-gib/ → GİB'e gönder

    autoretry_for KULLANILMAZ — sadece retryable hatalarda self.retry().

    Başarı  → Invoice: SENT, gib_status_code='100'
    Hata    → Invoice: ERROR, gib_error dolu
    """
    from apps.invoices.models import Invoice, InvoiceSyncLog, StoreEInvoiceSettings
    from apps.invoices.esurec_client import ESurecClient
    from apps.invoices.esurec_serializers import (
        serialize_invoice_for_esurec,
        determine_invoice_scenario,
    )

    sync_log = None
    try:
        invoice = Invoice.objects.prefetch_related('items').select_related(
            'customer', 'supplier',
        ).get(id=invoice_id, is_deleted=False)

        # ── KRİTİK: Müşteri VKN/TCKN validasyonu (KP-13 threshold-aware) ──
        # Nihai tüketici fallback kuralı:
        #   * Müşteri yok / VKN boş + tutar ≤ threshold → 11111111111 ile gönder
        #     (serializer `_build_buyer()` otomatik fallback yapar)
        #   * Müşteri yok / VKN boş + tutar > threshold → MASAK nedeniyle bloke
        #   * Tedarikçi (B2B) + VKN boş → her durumda bloke (fallback uygulanmaz)
        invoice_total = _invoice_total_for_threshold(invoice)
        customer_vkn = ''
        if invoice.customer:
            customer_vkn = (invoice.customer.identification_number or '').strip()
        elif invoice.supplier:
            customer_vkn = (invoice.supplier.tax_number or '').strip()

        # B2B akış: Tedarikçi seçilmiş ama VKN yoksa → kesin bloke
        if invoice.supplier and not customer_vkn:
            err_msg = (
                'Tedarikçi faturası için VKN/TCKN zorunludur. '
                'Lütfen tedarikçi kaydına vergi numarası ekleyin.'
            )
            _fail_invoice_and_log(invoice_id, err_msg, sync_log=None)
            return {'result': False, 'error_msg': err_msg}

        # Nihai tüketici akışı: Müşteri yok VEYA müşteri var ama VKN boş
        if (not invoice.customer and not invoice.supplier) or (invoice.customer and not customer_vkn):
            if invoice_total > NIHAI_TUKETICI_THRESHOLD_TL:
                err_msg = (
                    f'Fatura tutarı {invoice_total:.2f} TL, yasal nihai tüketici '
                    f'sınırının ({NIHAI_TUKETICI_THRESHOLD_TL:.0f} TL) üzerinde. '
                    f'MASAK mevzuatı gereği kimlik bilgisi (TCKN/VKN) zorunludur.'
                )
                _fail_invoice_and_log(invoice_id, err_msg, sync_log=None)
                return {'result': False, 'error_msg': err_msg}
            # Threshold altı → serializer 11111111111 fallback kullanacak
            log.info(
                "[KP-13] Celery nihai tüketici fallback: fatura=%s tutar=%s",
                invoice.invoice_no, invoice_total,
            )

        esurec_id = _get_esurec_id(invoice)

        sync_log = _get_or_create_sync_log(
            invoice=invoice,
            store_id=store_id,
            action='SEND_TO_GIB',
            task_id=self.request.id,
            attempt=self.request.retries + 1,
        )
        if esurec_id:
            sync_log.esurec_invoice_id = esurec_id
            sync_log.save(update_fields=['esurec_invoice_id', 'updated_at'])

        # Zaten GİB sürecindeyse atla
        if invoice.gib_status_code and str(invoice.gib_status_code) in [
            '100', '1000', '1100', '1200', '1300'
        ]:
            sync_log.status = 'SKIPPED'
            sync_log.response_data = {
                'msg': f'Zaten GİB sürecinde (Kod: {invoice.gib_status_code})'
            }
            sync_log.save(update_fields=['status', 'response_data', 'updated_at'])
            return {'result': True, 'skipped': True}

        client = ESurecClient()

        # ── ADIM 1: ESUREC_ID yoksa önce taslak oluştur ──
        if not esurec_id:
            log.info(
                f"[GIB] Fatura {invoice.invoice_no} → ESUREC_ID yok, "
                f"önce e-Süreç taslağı oluşturuluyor..."
            )

            scenario, is_einvoice, gib_info = determine_invoice_scenario(
                invoice, esurec_client=client
            )

            with transaction.atomic():
                invoice.scenario = scenario
                invoice.doc_class = 'E_INVOICE' if is_einvoice else 'E_ARCHIVE'
                invoice.is_einvoice = is_einvoice
                invoice.save(update_fields=[
                    'scenario', 'doc_class', 'is_einvoice', 'updated_at',
                ])

            payload = serialize_invoice_for_esurec(
                invoice, scenario=scenario, is_einvoice=is_einvoice, gib_info=gib_info
            )
            draft_resp = client.send_invoice(payload)

            if not draft_resp.get('result'):
                error_msg = draft_resp.get('error_msg') or draft_resp.get('message') or 'Taslak oluşturulamadı'
                retryable = draft_resp.get('retryable', False)

                if retryable and self.request.retries < self.max_retries:
                    _mark_retrying(sync_log, error_msg)
                    raise self.retry(exc=Exception(error_msg))

                _fail_invoice_and_log(invoice_id, error_msg, sync_log)
                return {'result': False, 'error_msg': error_msg}

            esurec_id = draft_resp.get('esurec_invoice_id', '')
            if not esurec_id:
                _fail_invoice_and_log(
                    invoice_id,
                    'e-Süreç taslak oluşturuldu ama esurec_invoice_id dönmedi.',
                    sync_log,
                )
                return {'result': False, 'error_msg': 'esurec_invoice_id alınamadı'}

            with transaction.atomic():
                inv = Invoice.objects.select_for_update().get(id=invoice_id)
                _set_esurec_id(inv, esurec_id)
                inv.gib_status_code = '10'
                inv.gib_status_desc = 'e-Süreç taslağı oluşturuldu'
                inv.save(update_fields=[
                    'gib_status_code', 'gib_status_desc', 'updated_at',
                ])

            sync_log.esurec_invoice_id = esurec_id
            sync_log.save(update_fields=['esurec_invoice_id', 'updated_at'])

            log.info(
                f"[GIB] Fatura {invoice.invoice_no} → e-Süreç taslağı oluşturuldu: {esurec_id}"
            )

        # ── ADIM 2: GİB'e gönder ──
        api_resp = client.send_to_gib(esurec_id)

        if api_resp.get('result'):
            # ---- GİB BAŞARI ----
            with transaction.atomic():
                inv = Invoice.objects.select_for_update().get(id=invoice_id)
                inv.gib_status_code = str(api_resp.get('gib_status_code', '100'))
                inv.gib_status_desc = 'GİB\'e gönderildi (e-Süreç)'
                inv.status = 'SENT'
                inv.gib_error = ''
                doc_no = api_resp.get('invoice_number')
                if doc_no:
                    inv.document_number = doc_no
                inv.save(update_fields=[
                    'gib_status_code', 'gib_status_desc', 'status',
                    'gib_error', 'document_number', 'updated_at',
                ])

            # Fatura kontör düşümü kaldırıldı (Nisan 2026)
            # try:
            #     s, _ = StoreEInvoiceSettings.objects.get_or_create(store_id=store_id)
            #     if s.enabled:
            #         s.consume(1)
            # except Exception:
            #     pass

            sync_log.status = 'SUCCESS'
            sync_log.response_data = api_resp
            sync_log.save(update_fields=['status', 'response_data', 'updated_at'])

            log.info(
                f"[GIB] Fatura {invoice.invoice_no} → GİB'e gönderildi. "
                f"Kod: {inv.gib_status_code}"
            )

            _create_activity_log(
                invoice_id=invoice_id,
                store_id=store_id,
                level='INFO',
                event='GIB_SUCCESS',
                user_message=f'Fatura "{invoice.invoice_no}" GİB\'e başarıyla gönderildi.',
            )

            return {'result': True, 'gib_status_code': inv.gib_status_code}

        else:
            # ---- GİB API HATASI ----
            error_msg = api_resp.get('error_msg') or api_resp.get('message') or 'GİB gönderimi başarısız'
            error_code = api_resp.get('error_code', '') or ''
            retryable = api_resp.get('retryable', False)

            if retryable and self.request.retries < self.max_retries:
                _mark_retrying(sync_log, error_msg)
                log.warning(
                    f"[GIB] Fatura {invoice.invoice_no} → retryable GİB hatası "
                    f"(deneme {self.request.retries + 1}/{self.max_retries}): {error_msg}"
                )
                raise self.retry(exc=Exception(error_msg))

            # Kalıcı hata (örn. [00019]) → _fail_invoice_and_log + gib_status_code
            # senkronu (e-Süreç tarafı 1400 atadı; Kuyum Plus da aynısını yansıtır).
            _fail_invoice_and_log(invoice_id, error_msg, sync_log)

            # e-Süreç'ten gelen gib_status_code/gib_status_description alanlarını
            # da yansıt (api_resp'de varsa) — takılmayı önler.
            try:
                srv_code = api_resp.get('gib_status_code')
                srv_desc = api_resp.get('gib_status_description') or ''
                if srv_code or srv_desc:
                    _update_fields = ['updated_at']
                    inv = Invoice.objects.get(id=invoice_id)
                    if srv_code:
                        inv.gib_status_code = str(srv_code)
                        _update_fields.append('gib_status_code')
                    if srv_desc:
                        inv.gib_status_desc = srv_desc[:500]
                        _update_fields.append('gib_status_desc')
                    inv.save(update_fields=_update_fields)
            except Exception as _e:
                log.warning(f"[GIB] gib_status_code propagation başarısız: {_e}")

            sync_log.response_data = api_resp
            sync_log.save(update_fields=['response_data', 'updated_at'])

            log.warning(
                f"[GIB] Fatura {invoice.invoice_no} → kalıcı GİB hatası "
                f"[{error_code}]: {error_msg}"
            )
            return {'result': False, 'error_msg': error_msg, 'error_code': error_code}

    except Invoice.DoesNotExist:
        if sync_log:
            sync_log.status = 'FAILED'
            sync_log.error_message = 'Fatura bulunamadı'
            sync_log.save(update_fields=['status', 'error_message', 'updated_at'])
        return {'result': False, 'error_msg': 'Fatura bulunamadı'}

    except Exception as e:
        error_str = str(e)[:500]
        error_detail = traceback.format_exc()

        is_retryable = any(
            kw in error_str.lower()
            for kw in ['timeout', 'bağlanılamadı', 'connection', 'connectionerror']
        )

        if is_retryable and self.request.retries < self.max_retries:
            _mark_retrying(sync_log, error_str)
            log.warning(
                f"[GIB] Fatura {invoice_id} → exception retry "
                f"(deneme {self.request.retries + 1}/{self.max_retries}): {error_str}"
            )
            raise self.retry(exc=e)

        _fail_invoice_and_log(invoice_id, error_str, sync_log, error_detail)
        log.exception(f"[GIB] Fatura {invoice_id} → kalıcı exception: {e}")
        return {'result': False, 'error_msg': error_str}


# ======================================================================
# 3. TOPLU DURUM SORGULAMA (PERİYODİK)
# ======================================================================

@shared_task(name='invoices.check_esurec_statuses')
def check_esurec_statuses_task():
    """
    KP-03: GİB'e gönderilmiş ama henüz onaylanmamış faturaların durumunu toplu sorgular.
    Celery Beat ile günde 4× (her 6 saatte bir) çalıştırılması önerilir.

    KP-08: 24 saatten fazla PROCESSING'de kalan InvoiceSyncLog kayıtlarını FAILED'a çeker.
    KP-12: GİB red/hata mesajını Invoice.gib_error alanına kalıcı olarak yazar.

    KRİTİK: 1300 → ISSUED (Fatura Kesildi), diğer kodlar SENT/QUEUED kalır.
    """
    from apps.invoices.models import Invoice, InvoiceSyncLog
    from apps.invoices.esurec_client import ESurecClient

    # ── KP-08: PROCESSING timeout guard ──
    stale_cutoff = timezone.now() - timedelta(hours=24)
    stale_logs = InvoiceSyncLog.objects.filter(
        status='PROCESSING',
        updated_at__lt=stale_cutoff,
    )
    stale_count = stale_logs.count()
    if stale_count > 0:
        stale_invoice_ids = list(stale_logs.values_list('invoice_id', flat=True).distinct())
        stale_logs.update(
            status='FAILED',
            error_message='24 saat zaman aşımı — PROCESSING durumunda takılı kaldı.',
        )
        Invoice.objects.filter(
            id__in=stale_invoice_ids,
            status__in=['QUEUED', 'SENT'],
        ).update(
            status='ERROR',
            gib_error='GİB durum güncellemesi 24 saat içinde tamamlanamadı.',
        )
        log.warning(f"[ESUREC] KP-08: {stale_count} PROCESSING kaydı → FAILED")

    # ── KP-03: InvoiceSyncLog tabanlı GİB durum sorgulaması ──
    esurec_id_pairs = list(
        InvoiceSyncLog.objects.filter(
            action='SEND_TO_ESUREC',
            status='SUCCESS',
        ).exclude(esurec_invoice_id='').values_list('invoice_id', 'esurec_invoice_id')
    )
    esurec_id_map = {str(pid): eid for pid, eid in esurec_id_pairs}

    # NOT: '1400' da listeye eklendi — e-Süreç tarafı _try_stuck_recovery ile
    # faturaları '1400'e çekebilir; bu durumda Kuyum Plus'ın da durumu sorgulayıp
    # gib_error alanını doldurması gerekir ki kullanıcı hata mesajını görsün.
    pending_invoices = Invoice.objects.filter(
        id__in=[pid for pid, _ in esurec_id_pairs],
        is_deleted=False,
        gib_status_code__in=['100', '1000', '1100', '1200', '1400'],
        updated_at__gte=timezone.now() - timedelta(days=7),
    )

    if not pending_invoices.exists():
        return {'checked': 0, 'stale_cleaned': stale_count}

    client = ESurecClient()
    updated = 0
    errors = 0

    for invoice in pending_invoices[:200]:
        try:
            esurec_id = esurec_id_map.get(str(invoice.id)) or _get_esurec_id(invoice)
            if not esurec_id:
                continue

            resp = client.check_status(esurec_id)
            if resp.get('result'):
                new_code = resp.get('gib_status_code')
                new_desc = resp.get('gib_status_description', '') or ''

                code_changed = new_code and str(new_code) != str(invoice.gib_status_code)
                desc_changed = new_desc and new_desc != (invoice.gib_status_desc or '')

                # KRİTİK: Kod değişmese bile description değişmişse (MySoft hata
                # detayı geç geldiyse) güncelle — böylece [00019] gibi hatalar
                # 1400'e geçmeden de gib_error alanına düşer.
                if code_changed or desc_changed:
                    if code_changed:
                        invoice.gib_status_code = str(new_code)
                    invoice.gib_status_desc = new_desc

                    status_map = {
                        '1300': 'ISSUED',
                        '1230': 'CANCELED',
                        '1200': 'SENT',
                        '1100': 'SENT',
                        '1000': 'QUEUED',
                        '100':  'SENT',
                        '1400': 'ERROR',
                        '1500': 'REJECTED',
                    }
                    effective_code = str(new_code) if code_changed else str(invoice.gib_status_code)
                    new_status = status_map.get(effective_code)
                    if new_status:
                        invoice.status = new_status

                    # KP-12: GİB red/hata mesajını kalıcı sakla
                    if effective_code in ('1400', '1500'):
                        invoice.gib_error = _sanitize_gib_error_task(
                            new_desc or 'GİB hatası'
                        )[:500]
                    elif code_changed:
                        # Sadece kod başarılı bir duruma geçtiyse hatayı temizle
                        invoice.gib_error = ''

                    doc_no = resp.get('invoice_number')
                    if doc_no:
                        invoice.document_number = doc_no

                    invoice.save(update_fields=[
                        'gib_status_code', 'gib_status_desc', 'status',
                        'gib_error', 'document_number', 'updated_at',
                    ])
                    updated += 1
        except Exception as e:
            log.warning(f"Durum sorgulama hatası ({invoice.id}): {type(e).__name__}")
            errors += 1

    log.info(f"[ESUREC] Toplu durum: {updated} güncellendi, {errors} hata, {stale_count} timeout")
    return {'checked': pending_invoices.count(), 'updated': updated, 'errors': errors, 'stale_cleaned': stale_count}


# ======================================================================
# 4. E-GİDER PUSULASI — e-SÜREÇ'E TASLAK GÖNDER (ASYNC)
# ======================================================================

def _get_seller_vkn_from_invoice(invoice):
    """Düzenleyen (mağaza/şirket) VKN'sini çözer."""
    store = getattr(invoice, 'store', None)
    if store is None:
        return ''
    company = getattr(store, 'company', None)
    return (
        getattr(company, 'tax_number', '') or
        getattr(store, 'tax_number', '') or ''
    ).strip()


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    retry_backoff=True,
    acks_late=True,
    name='invoices.send_expense_to_esurec'
)
def send_expense_voucher_to_esurec_task(self, invoice_id: str, store_id: str):
    """
    Gider pusulasını e-Süreç'e taslak olarak gönderir (Celery asenkron).

    Akış: DRAFT → QUEUED → (Task) → DRAFT (gib_status_code='10') OR ERROR

    Başarı → gib_status_code='10', 'e-Süreç Taslağı'
    Hata   → status=ERROR, gib_error dolu
    """
    from apps.invoices.models import Invoice, InvoiceSyncLog
    from apps.invoices.esurec_client import ESurecClient
    from apps.invoices.esurec_expense_views import _serialize_expense_voucher

    sync_log = None
    try:
        invoice = Invoice.objects.select_related(
            'customer', 'supplier', 'store', 'store__company',
        ).prefetch_related('items').get(
            id=invoice_id, is_deleted=False,
            invoice_type=Invoice.Type.PURCHASE,
        )

        # KP-08: Idempotency guard — aynı belge için PROCESSING varsa atla
        already_processing = InvoiceSyncLog.objects.filter(
            invoice=invoice,
            action='SEND_TO_ESUREC',
            status='PROCESSING',
        ).exists()
        if already_processing and self.request.retries == 0:
            log.warning(
                f"[EV-ESUREC] Gider pusulası {invoice_id} zaten PROCESSING — "
                f"çift gönderim engellendi."
            )
            return {'result': True, 'skipped': True, 'reason': 'already_processing'}

        sync_log = _get_or_create_sync_log(
            invoice=invoice,
            store_id=store_id,
            action='SEND_TO_ESUREC',
            task_id=self.request.id,
            attempt=self.request.retries + 1,
        )

        # Zaten gönderilmişse atla
        existing_id = _get_esurec_id(invoice)
        if existing_id:
            sync_log.status = 'SKIPPED'
            sync_log.response_data = {'msg': 'Zaten gönderilmiş', 'esurec_id': existing_id}
            sync_log.save(update_fields=['status', 'response_data', 'updated_at'])
            if invoice.status == 'QUEUED':
                Invoice.objects.filter(id=invoice_id).update(
                    status='DRAFT',
                    gib_status_code='10',
                    gib_status_desc='e-Süreç taslağı (zaten mevcut)',
                )
            return {'result': True, 'skipped': True, 'esurec_id': existing_id}

        client = ESurecClient()
        payload = _serialize_expense_voucher(invoice)
        api_resp = client.send_expense_voucher(payload)

        if api_resp.get('result'):
            # ---- BAŞARI ----
            esurec_id = (
                api_resp.get('esurec_voucher_id', '') or
                api_resp.get('voucher_id', '') or
                api_resp.get('id', '')
            )
            with transaction.atomic():
                inv = Invoice.objects.select_for_update().get(id=invoice_id)
                if esurec_id:
                    _set_esurec_id(inv, esurec_id, store=inv.store)
                inv.status = 'DRAFT'
                inv.gib_status_code = str(api_resp.get('gib_status_code', '10'))
                inv.gib_status_desc = 'e-Süreç taslağı oluşturuldu (Gider Pusulası)'
                inv.gib_error = ''
                inv.save(update_fields=[
                    'status', 'gib_status_code', 'gib_status_desc',
                    'gib_error', 'updated_at',
                ])

            sync_log.status = 'SUCCESS'
            sync_log.response_data = api_resp
            sync_log.esurec_invoice_id = esurec_id
            sync_log.save(update_fields=[
                'status', 'response_data', 'esurec_invoice_id', 'updated_at',
            ])

            log.info(
                f"[EV-ESUREC] Gider pusulası {invoice.invoice_no} → e-Süreç gönderildi. "
                f"ESUREC_ID: {esurec_id}"
            )

            _create_activity_log(
                invoice_id=invoice_id,
                store_id=store_id,
                level='INFO',
                event='SEND_ATTEMPT',
                user_message=f'Gider pusulası "{invoice.invoice_no}" e-Süreç taslağı oluşturuldu.',
            )

            return {'result': True, 'esurec_id': esurec_id}

        else:
            # ---- API HATASI ----
            error_msg = api_resp.get('error_msg') or api_resp.get('msg') or 'Bilinmeyen hata'
            retryable = api_resp.get('retryable', False)

            if retryable and self.request.retries < self.max_retries:
                _mark_retrying(sync_log, error_msg)
                log.warning(
                    f"[EV-ESUREC] Gider pusulası {invoice.invoice_no} → retryable hata "
                    f"(deneme {self.request.retries + 1}/{self.max_retries}): {error_msg}"
                )
                raise self.retry(exc=Exception(error_msg))

            _fail_invoice_and_log(invoice_id, error_msg, sync_log)
            sync_log.response_data = api_resp
            sync_log.save(update_fields=['response_data', 'updated_at'])

            return {'result': False, 'error_msg': error_msg}

    except Invoice.DoesNotExist:
        if sync_log:
            sync_log.status = 'FAILED'
            sync_log.error_message = 'Gider pusulası bulunamadı'
            sync_log.save(update_fields=['status', 'error_message', 'updated_at'])
        return {'result': False, 'error_msg': 'Gider pusulası bulunamadı'}

    except Exception as e:
        error_str = str(e)[:500]
        error_detail = traceback.format_exc()

        is_retryable = any(
            kw in error_str.lower()
            for kw in ['timeout', 'bağlanılamadı', 'connection', 'connectionerror']
        )

        if is_retryable and self.request.retries < self.max_retries:
            _mark_retrying(sync_log, error_str)
            raise self.retry(exc=e)

        _fail_invoice_and_log(invoice_id, error_str, sync_log, error_detail)
        log.exception(f"[EV-ESUREC] Gider pusulası {invoice_id} → kalıcı exception: {e}")
        return {'result': False, 'error_msg': error_str}


# ======================================================================
# 5. E-GİDER PUSULASI — GİB'E GÖNDER (ASYNC, İKİ ADIMLI)
# ======================================================================

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    retry_backoff=True,
    acks_late=True,
    name='invoices.send_expense_to_gib'
)
def send_expense_voucher_to_gib_task(self, invoice_id: str, store_id: str):
    """
    Gider pusulasını GİB'e gönderir (Celery asenkron).
    ESUREC_ID yoksa önce taslak oluşturur. Tenant izolasyonu için seller_vkn
    zorunludur (otomatik mağaza VKN'sinden çözülür).
    """
    from apps.invoices.models import Invoice, InvoiceSyncLog
    from apps.invoices.esurec_client import ESurecClient
    from apps.invoices.esurec_expense_views import _serialize_expense_voucher

    sync_log = None
    try:
        invoice = Invoice.objects.select_related(
            'customer', 'supplier', 'store', 'store__company',
        ).prefetch_related('items').get(
            id=invoice_id, is_deleted=False,
            invoice_type=Invoice.Type.PURCHASE,
        )

        seller_vkn = _get_seller_vkn_from_invoice(invoice)
        if not seller_vkn:
            err_msg = 'Mağaza/firma VKN tanımlı değil; tenant izolasyonu sağlanamıyor.'
            _fail_invoice_and_log(invoice_id, err_msg, sync_log=None)
            return {'result': False, 'error_msg': err_msg}

        esurec_id = _get_esurec_id(invoice)

        sync_log = _get_or_create_sync_log(
            invoice=invoice,
            store_id=store_id,
            action='SEND_TO_GIB',
            task_id=self.request.id,
            attempt=self.request.retries + 1,
        )
        if esurec_id:
            sync_log.esurec_invoice_id = esurec_id
            sync_log.save(update_fields=['esurec_invoice_id', 'updated_at'])

        # Zaten GİB sürecinde ise atla
        if invoice.gib_status_code and str(invoice.gib_status_code) in [
            '100', '1000', '1100', '1200', '1300'
        ]:
            sync_log.status = 'SKIPPED'
            sync_log.response_data = {
                'msg': f'Zaten GİB sürecinde (Kod: {invoice.gib_status_code})',
            }
            sync_log.save(update_fields=['status', 'response_data', 'updated_at'])
            return {'result': True, 'skipped': True}

        client = ESurecClient()

        # ── ADIM 1: ESUREC_ID yoksa önce taslak oluştur ──
        if not esurec_id:
            log.info(
                f"[EV-GIB] Gider pusulası {invoice.invoice_no} → ESUREC_ID yok, "
                f"önce e-Süreç taslağı oluşturuluyor..."
            )

            payload = _serialize_expense_voucher(invoice)
            draft_resp = client.send_expense_voucher(payload)

            if not draft_resp.get('result'):
                error_msg = (
                    draft_resp.get('error_msg') or
                    draft_resp.get('msg') or
                    'Taslak oluşturulamadı'
                )
                retryable = draft_resp.get('retryable', False)
                if retryable and self.request.retries < self.max_retries:
                    _mark_retrying(sync_log, error_msg)
                    raise self.retry(exc=Exception(error_msg))
                _fail_invoice_and_log(invoice_id, error_msg, sync_log)
                return {'result': False, 'error_msg': error_msg}

            esurec_id = (
                draft_resp.get('esurec_voucher_id', '') or
                draft_resp.get('voucher_id', '') or
                draft_resp.get('id', '')
            )
            if not esurec_id:
                _fail_invoice_and_log(
                    invoice_id,
                    'e-Süreç taslak oluşturuldu ama esurec_voucher_id dönmedi.',
                    sync_log,
                )
                return {'result': False, 'error_msg': 'esurec_voucher_id alınamadı'}

            with transaction.atomic():
                inv = Invoice.objects.select_for_update().get(id=invoice_id)
                _set_esurec_id(inv, esurec_id, store=inv.store)
                inv.gib_status_code = '10'
                inv.gib_status_desc = 'e-Süreç taslağı oluşturuldu (Gider Pusulası)'
                inv.save(update_fields=[
                    'gib_status_code', 'gib_status_desc', 'updated_at',
                ])

            sync_log.esurec_invoice_id = esurec_id
            sync_log.save(update_fields=['esurec_invoice_id', 'updated_at'])

        # ── ADIM 2: GİB'e gönder ──
        api_resp = client.send_expense_voucher_to_gib(esurec_id, seller_vkn=seller_vkn)

        if api_resp.get('result'):
            with transaction.atomic():
                inv = Invoice.objects.select_for_update().get(id=invoice_id)
                inv.gib_status_code = str(api_resp.get('gib_status_code', '100'))
                inv.gib_status_desc = (
                    api_resp.get('gib_status_description') or "GİB'e gönderildi (Gider Pusulası)"
                )[:500]
                inv.status = 'SENT'
                inv.gib_error = ''
                inv.save(update_fields=[
                    'gib_status_code', 'gib_status_desc', 'status',
                    'gib_error', 'updated_at',
                ])

            sync_log.status = 'SUCCESS'
            sync_log.response_data = api_resp
            sync_log.save(update_fields=['status', 'response_data', 'updated_at'])

            _create_activity_log(
                invoice_id=invoice_id,
                store_id=store_id,
                level='INFO',
                event='GIB_SUCCESS',
                user_message=f'Gider pusulası "{invoice.invoice_no}" GİB\'e gönderildi.',
            )
            return {'result': True, 'gib_status_code': inv.gib_status_code}

        else:
            error_msg = api_resp.get('error_msg') or api_resp.get('msg') or 'GİB gönderimi başarısız'
            retryable = api_resp.get('retryable', False)
            if retryable and self.request.retries < self.max_retries:
                _mark_retrying(sync_log, error_msg)
                raise self.retry(exc=Exception(error_msg))

            _fail_invoice_and_log(invoice_id, error_msg, sync_log)

            # e-Süreç'ten gelen gib_status_code'u da yansıt
            try:
                srv_code = api_resp.get('gib_status_code')
                srv_desc = api_resp.get('gib_status_description') or ''
                if srv_code or srv_desc:
                    _update_fields = ['updated_at']
                    inv = Invoice.objects.get(id=invoice_id)
                    if srv_code:
                        inv.gib_status_code = str(srv_code)
                        _update_fields.append('gib_status_code')
                    if srv_desc:
                        inv.gib_status_desc = srv_desc[:500]
                        _update_fields.append('gib_status_desc')
                    inv.save(update_fields=_update_fields)
            except Exception as _e:
                log.warning(f"[EV-GIB] gib_status_code propagation başarısız: {_e}")

            sync_log.response_data = api_resp
            sync_log.save(update_fields=['response_data', 'updated_at'])

            return {'result': False, 'error_msg': error_msg}

    except Invoice.DoesNotExist:
        if sync_log:
            sync_log.status = 'FAILED'
            sync_log.error_message = 'Gider pusulası bulunamadı'
            sync_log.save(update_fields=['status', 'error_message', 'updated_at'])
        return {'result': False, 'error_msg': 'Gider pusulası bulunamadı'}

    except Exception as e:
        error_str = str(e)[:500]
        error_detail = traceback.format_exc()
        is_retryable = any(
            kw in error_str.lower()
            for kw in ['timeout', 'bağlanılamadı', 'connection', 'connectionerror']
        )
        if is_retryable and self.request.retries < self.max_retries:
            _mark_retrying(sync_log, error_str)
            raise self.retry(exc=e)
        _fail_invoice_and_log(invoice_id, error_str, sync_log, error_detail)
        log.exception(f"[EV-GIB] Gider pusulası {invoice_id} → kalıcı exception: {e}")
        return {'result': False, 'error_msg': error_str}


# ======================================================================
# 6. GİDER PUSULASI — TOPLU DURUM SORGULAMA (PERİYODİK)
# ======================================================================

@shared_task(name='invoices.check_expense_voucher_statuses')
def check_expense_voucher_statuses_task():
    """
    GİB'e gönderilmiş ama henüz onaylanmamış gider pusulalarının durumunu
    toplu sorgular. Celery Beat ile her 4 saatte bir çalıştırılması önerilir.

    - 24 saat PROCESSING'de takılı kalan InvoiceSyncLog kayıtlarını FAILED yapar
    - 1400 dönerse gib_error'a kullanıcı dostu mesaj yazar
    - Tenant izolasyonu: her çağrı seller_vkn ile yapılır
    """
    from apps.invoices.models import Invoice, InvoiceSyncLog
    from apps.invoices.esurec_client import ESurecClient

    # ── PROCESSING timeout guard ──
    stale_cutoff = timezone.now() - timedelta(hours=24)
    stale_logs = InvoiceSyncLog.objects.filter(
        status='PROCESSING',
        action__in=['SEND_TO_ESUREC', 'SEND_TO_GIB'],
        updated_at__lt=stale_cutoff,
        invoice__invoice_type=Invoice.Type.PURCHASE,
    )
    stale_count = stale_logs.count()
    if stale_count > 0:
        stale_ids = list(stale_logs.values_list('invoice_id', flat=True).distinct())
        stale_logs.update(
            status='FAILED',
            error_message='24 saat zaman aşımı — PROCESSING durumunda takılı kaldı.',
        )
        Invoice.objects.filter(
            id__in=stale_ids,
            invoice_type=Invoice.Type.PURCHASE,
            status__in=['QUEUED', 'SENT'],
        ).update(
            status='ERROR',
            gib_error='Gider pusulası durumu 24 saat içinde güncellenemedi.',
        )
        log.warning(f"[EV-STATUS] {stale_count} PROCESSING gider pusulası → FAILED")

    # ── Bekleyen belgeleri bul (sadece PURCHASE tipindeki Invoice'lar) ──
    esurec_pairs = list(
        InvoiceSyncLog.objects.filter(
            action='SEND_TO_ESUREC',
            status='SUCCESS',
            invoice__invoice_type=Invoice.Type.PURCHASE,
            invoice__is_deleted=False,
        ).exclude(esurec_invoice_id='').values_list(
            'invoice_id', 'esurec_invoice_id',
        )
    )
    esurec_map = {str(pid): eid for pid, eid in esurec_pairs}

    pending = Invoice.objects.select_related('store', 'store__company').filter(
        id__in=[pid for pid, _ in esurec_pairs],
        invoice_type=Invoice.Type.PURCHASE,
        is_deleted=False,
        gib_status_code__in=['100', '1000', '1100', '1200', '1400'],
        updated_at__gte=timezone.now() - timedelta(days=7),
    )

    if not pending.exists():
        return {'checked': 0, 'stale_cleaned': stale_count}

    client = ESurecClient()
    updated = 0
    errors = 0

    _status_map = {
        '1300': ('ISSUED', 'GİB Onayladı'),
        '1230': ('CANCELED', 'İptal'),
        '1220': ('SENT', 'Entegratöre İletildi (Cevap Bekliyor)'),
        '1210': ('SENT', 'Zarflandı-İmzalandı'),
        '1200': ('SENT', 'Zarflandı'),
        '1100': ('SENT', 'İşleniyor'),
        '1000': ('QUEUED', 'Kuyrukta'),
        '100':  ('SENT', 'Gönderildi'),
        '1400': ('ERROR', 'GİB Hatası'),
        '1500': ('REJECTED', 'Reddedildi'),
        'CANCELLED': ('CANCELED', 'İptal'),
    }

    for invoice in pending[:200]:
        try:
            esurec_id = esurec_map.get(str(invoice.id)) or _get_esurec_id(invoice)
            if not esurec_id:
                continue

            seller_vkn = _get_seller_vkn_from_invoice(invoice)
            if not seller_vkn:
                log.warning(
                    f"[EV-STATUS] {invoice.invoice_no}: seller_vkn yok, atlandı."
                )
                continue

            resp = client.check_expense_voucher_status(esurec_id, seller_vkn=seller_vkn)
            if not resp.get('result'):
                errors += 1
                continue

            new_code = str(resp.get('gib_status_code', '') or '').strip()
            new_desc = str(resp.get('gib_status_description', '') or '').strip()

            code_changed = new_code and str(new_code) != str(invoice.gib_status_code or '')
            desc_changed = new_desc and new_desc != (invoice.gib_status_desc or '')

            if code_changed or desc_changed:
                update_fields = ['gib_status_code', 'gib_status_desc', 'updated_at']
                if code_changed:
                    invoice.gib_status_code = str(new_code)
                if desc_changed:
                    invoice.gib_status_desc = new_desc

                effective_code = str(new_code) if code_changed else str(invoice.gib_status_code)
                mapped = _status_map.get(effective_code)
                if mapped:
                    invoice.status = mapped[0]
                    update_fields.append('status')

                if effective_code in ('1400', '1500'):
                    invoice.gib_error = _sanitize_gib_error_task(new_desc or 'GİB hatası')[:500]
                    update_fields.append('gib_error')
                elif code_changed:
                    invoice.gib_error = ''
                    update_fields.append('gib_error')

                invoice.save(update_fields=update_fields)
                updated += 1

        except Exception as e:
            log.warning(f"[EV-STATUS] Sorgulama hatası ({invoice.id}): {type(e).__name__}")
            errors += 1

    log.info(
        f"[EV-STATUS] Toplu durum: {updated} güncellendi, {errors} hata, "
        f"{stale_count} timeout"
    )
    return {
        'checked': pending.count(),
        'updated': updated,
        'errors': errors,
        'stale_cleaned': stale_count,
    }


# ============================================================================
# PERF-04: Asenkron PDF Üretimi (Zero Downtime)
# ============================================================================
# Gün içinde sunucuyu kilitleyen fatura PDF indirme işlemi Celery'ye alındı.
# Eski sync endpoint `/invoices/<id>/download` HİÇ DEĞİŞTİRİLMEDİ — geriye uyum.
# Yeni asenkron akış:
#   1. POST /invoices/<id>/pdf/async         → task'ı kuyruğa al, task_id döndür
#   2. GET  /invoices/pdf/status/<task_id>   → durum (PENDING/PROGRESS/SUCCESS/FAILURE)
#   3. GET  /invoices/<id>/pdf/result        → hazır PDF dosyasını stream et
#
# Üretilen PDF MEDIA_ROOT/Invoices/pdf_cache/<invoice_id>.pdf yoluna yazılır.
# DB'deki pdf_file FK alanına DOKUNULMAZ (e-Süreç akışını bozmamak için).
# ============================================================================

@shared_task(name="invoices.render_invoice_pdf", max_retries=2, default_retry_delay=10)
def render_invoice_pdf_task(invoice_id: str):
    """
    Fatura HTML'ini render edip PDF'i MEDIA altına yazar.
    Dönüş: {'ok': True, 'path': <MEDIA rel>, 'url': <MEDIA URL>, 'invoice_no': ...}
    Hata: {'ok': False, 'error': ...}
    """
    import os
    from io import BytesIO
    from django.conf import settings
    from django.template.loader import render_to_string
    from django.contrib.staticfiles import finders
    from xhtml2pdf import pisa
    from apps.invoices.models import Invoice

    try:
        inv = (
            Invoice.objects
            .select_related('customer', 'supplier', 'store', 'store__company')
            .prefetch_related('items')
            .get(id=invoice_id, is_deleted=False)
        )

        if inv.invoice_type == Invoice.Type.PURCHASE:
            template_name = "management/invoices/expense_note_detail.html"
        else:
            template_name = "management/invoices/detail.html"

        # Template request-agnostic — request=None ile render edilebiliyor (Grep ile doğrulandı).
        html = render_to_string(template_name, {"record": inv, "request": None})

        def _link_callback(uri, rel):
            if settings.MEDIA_URL and uri.startswith(settings.MEDIA_URL):
                path = os.path.join(settings.MEDIA_ROOT, uri.replace(settings.MEDIA_URL, ""))
            elif settings.STATIC_URL and uri.startswith(settings.STATIC_URL):
                static_path = uri.replace(settings.STATIC_URL, "")
                path = finders.find(static_path)
                if not path:
                    path = os.path.join(settings.STATIC_ROOT or '', static_path)
            else:
                path = uri
            if not path or not os.path.isfile(path):
                return None
            return path

        pdf_io = BytesIO()
        pisa_status = pisa.CreatePDF(src=html, dest=pdf_io, link_callback=_link_callback)
        if pisa_status.err:
            return {'ok': False, 'error': f'pisa_err={pisa_status.err}'}

        cache_dir = os.path.join(settings.MEDIA_ROOT, 'Invoices', 'pdf_cache')
        os.makedirs(cache_dir, exist_ok=True)
        filename = f"{invoice_id}.pdf"
        abs_path = os.path.join(cache_dir, filename)

        with open(abs_path, 'wb') as f:
            f.write(pdf_io.getvalue())

        rel = f"Invoices/pdf_cache/{filename}"
        return {
            'ok': True,
            'path': rel,
            'url': f"{settings.MEDIA_URL}{rel}",
            'invoice_no': inv.invoice_no,
        }

    except Invoice.DoesNotExist:
        return {'ok': False, 'error': 'invoice_not_found'}
    except Exception as e:
        log.warning(f"[PDF] render hatası invoice={invoice_id}: {type(e).__name__}: {e}")
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
