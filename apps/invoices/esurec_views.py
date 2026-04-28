# ============================================================================
# DOSYA: apps/invoices/esurec_views.py
# KONUM: Kuyum Plus projesi içinde
# AÇIKLAMA: e-Süreç entegrasyon view'ları (kuyruk + önizleme destekli)
# ============================================================================

import base64
import json
import logging
import re
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST, require_GET

from apps.banking.models import EsurecTenantCredential
from apps.invoices.models import Invoice, InvoiceSyncLog, StoreEInvoiceSettings
from apps.invoices.esurec_client import ESurecClient
from apps.invoices.esurec_serializers import serialize_invoice_for_esurec

log = logging.getLogger(__name__)


# ======================================================================
# NİHAİ TÜKETİCİ FALLBACK SABİTLERİ (KP-13)
# ======================================================================
# GİB e-Arşiv mevzuatı + MASAK Kıymetli Maden Yönetmeliği birleşimi:
# Belirtilen tutarın ALTINDA kalan perakende satışlarda müşteri TCKN vermek
# istemezse "11111111111" (yabancı uyruklu → "22222222222") kullanılabilir.
# Eşiğin ÜSTÜNDE gerçek kimlik tespiti zorunludur (MASAK).
#
# NOT: Threshold burada modül-seviye sabit olarak tutuluyor. İleride firma
# bazlı ayar gerekirse StoreEInvoiceSettings'e taşınabilir (migration + UI).
# ======================================================================

NIHAI_TUKETICI_TCKN = '11111111111'   # Yerli nihai tüketici GİB standart TCKN'si
NIHAI_TUKETICI_VKN_YABANCI = '22222222222'  # Yabancı uyruklu nihai tüketici
NIHAI_TUKETICI_THRESHOLD_TL = Decimal('30000.00')  # MASAK kimlik tespiti alt sınırı (başlangıç değeri)


def _invoice_total_for_threshold(invoice) -> Decimal:
    """
    Faturanın threshold kontrolü için temel alınacak toplam tutarını döner.
    grand_total öncelikli; yoksa subtotal; o da yoksa Decimal('0').
    """
    for field in ('grand_total', 'subtotal'):
        try:
            val = getattr(invoice, field, None)
            if val is not None:
                return Decimal(str(val))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return Decimal('0')


# ======================================================================
# YARDIMCILAR
# ======================================================================

def _user_store(request):
    store = getattr(request.user, 'store', None)
    if not store:
        raise ValueError("Kullanıcıya bağlı mağaza bulunamadı.")
    return store


# ── KP-07 DÜZELTMESİ: ESUREC_ID artık notes etiketinde değil,
# InvoiceSyncLog.esurec_invoice_id üzerinden okunur/yazılır. ──
# Göç dönemi fallback: InvoiceSyncLog'da bulunamazsa notes'a da bak.

def _extract_esurec_id_from_notes(notes: str) -> str:
    """Göç dönemi fallback — eski notes etiketinden oku."""
    for line in (notes or '').split('\n'):
        line = line.strip()
        if line.startswith('ESUREC_ID:'):
            return line.replace('ESUREC_ID:', '').strip()
    return ''


def _get_esurec_id(invoice) -> str:
    """
    KP-07: InvoiceSyncLog'dan esurec_invoice_id'yi çeker.
    Bulunamazsa göç dönemi fallback olarak notes etiketine bakar.
    """
    sync_log = InvoiceSyncLog.objects.filter(
        invoice=invoice,
        action=InvoiceSyncLog.Action.SEND_TO_ESUREC,
        status=InvoiceSyncLog.Status.SUCCESS,
        esurec_invoice_id__isnull=False,
    ).exclude(esurec_invoice_id='').order_by('-created_at').first()

    if sync_log:
        return sync_log.esurec_invoice_id

    # Göç dönemi fallback: eski notes etiketinden oku
    return _extract_esurec_id_from_notes(getattr(invoice, 'notes', ''))


def _set_esurec_id(invoice, esurec_id: str, store=None):
    """
    KP-07: Başarılı gönderim sonrası InvoiceSyncLog kaydını oluşturur.
    Notes alanına artık yazılmaz.
    """
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


def _get_ids_from_request(request) -> list:
    try:
        body = json.loads(request.body.decode('utf-8'))
        ids = body.get('invoice_ids', [])
        if not ids:
            single = body.get('invoice_id')
            if single:
                ids = [single]
        return [i for i in ids if i]
    except Exception:
        ids = request.POST.getlist('invoice_ids[]') or [request.POST.get('invoice_id')]
        return [i for i in ids if i]


def _use_celery() -> bool:
    """Celery kullanılabilir mi kontrol et."""
    try:
        from django.conf import settings
        return 'django_celery_beat' in getattr(settings, 'INSTALLED_APPS', [])
    except Exception:
        return False


# ======================================================================
# HATA MESAJI SANİTİZASYONU
# ======================================================================

# Teknik hata kalıpları → kullanıcı dostu mesaj eşlemesi.
# Regex ile eşleşen ilk kalıp kullanılır.
_ERROR_PATTERNS = [
    # ── Entegratör Seri / Belge Numarası Hataları (MySoft Error Codes) ──
    # [00019]: "Belge için uygun alternatif seri bulunamadı / Belge no üretilemedi"
    # GERÇEK KÖK NEDEN: e-Süreç tarafında ilgili dealer için aktif/varsayılan
    # InvoiceSeries kaydı yok (ya da series_type rakamsal uyuşmazlığı var).
    # Tarih problemi DEĞİL — önceki çeviri kullanıcıyı yanlış yönlendiriyordu.
    ('00019', 'Entegratör seri tanımı eksik: Bu firma için e-Süreç panelinde E-Fatura/E-Arşiv serisi (varsayılan olarak) tanımlı değil. e-Süreç → Firma Ayarları → Fatura Serileri bölümünden kontrol edin veya yöneticinize başvurun.'),
    ('uygun alternatif seri', 'Entegratör seri tanımı eksik: Bu firma için e-Süreç panelinde E-Fatura/E-Arşiv serisi (varsayılan olarak) tanımlı değil.'),
    ('belge no üretilemedi', 'Entegratör belge numarası üretemedi. Büyük olasılıkla seri tanımı eksik — e-Süreç → Firma Ayarları → Fatura Serileri bölümünü kontrol edin.'),
    ('alternatif belge numarası', 'Entegratör alternatif seri bulamadı. Seri tanımı kontrolü gerekli.'),
    ('eski tarih', 'Fatura tarihi çok eski. Faturayı "Düzenlemeye Al" ile taslağa döndürüp tarihi güncelledikten sonra tekrar gönderin.'),
    # PostgreSQL constraint ihlalleri
    ('null value in column', 'Veri gönderiminde eksik alan hatası oluştu. Lütfen fatura bilgilerini kontrol edip tekrar deneyin.'),
    ('violates not-null constraint', 'Veri gönderiminde eksik alan hatası oluştu. Lütfen fatura bilgilerini kontrol edip tekrar deneyin.'),
    ('violates unique constraint', 'Bu fatura zaten sistemde kayıtlı. Mükerrer kayıt engellendi.'),
    ('violates foreign key constraint', 'İlişkili kayıt bulunamadı. Lütfen fatura verilerini kontrol edin.'),
    ('violates check constraint', 'Veri doğrulama hatası. Fatura bilgilerinde geçersiz değer tespit edildi.'),
    # Bağlantı hataları
    ('connectionerror', 'e-Süreç sunucusuna bağlanılamadı. Lütfen birkaç dakika sonra tekrar deneyin.'),
    ('timeout', 'e-Süreç sunucusu yanıt vermedi (zaman aşımı). Lütfen tekrar deneyin.'),
    ('bağlanılamadı', 'e-Süreç sunucusuna bağlanılamadı. Lütfen birkaç dakika sonra tekrar deneyin.'),
    # Kimlik doğrulama hataları
    ('401', 'e-Süreç kimlik doğrulama hatası. API anahtarlarını kontrol edin.'),
    ('403', 'e-Süreç erişim yetkisi hatası. Yöneticinize başvurun.'),
    # Rate limit
    ('429', 'Çok fazla istek gönderildi. Lütfen birkaç dakika bekleyin.'),
    ('rate limit', 'Çok fazla istek gönderildi. Lütfen birkaç dakika bekleyin.'),
    # Sunucu hataları
    ('500', 'e-Süreç sunucu hatası. Lütfen birkaç dakika sonra tekrar deneyin.'),
    ('502', 'e-Süreç sunucusu geçici olarak kullanılamıyor.'),
    ('503', 'e-Süreç sunucusu bakımda. Lütfen daha sonra deneyin.'),
    # Entegratör (Mysoft) hataları
    ('mysoft', 'Entegratör (Mysoft) ile iletişimde hata oluştu. Tekrar deneyin.'),
    ('entegratör hatası', 'Entegratör ile iletişimde hata oluştu. Tekrar deneyin.'),
    # Genel GİB hataları
    ('gib', 'GİB gönderim hatası oluştu. Fatura bilgilerini kontrol edip tekrar deneyin.'),
    # Numara/Seri hataları
    ('numaratör', 'Fatura numarası atanamadı. Numara serisi ayarlarını kontrol edin.'),
    ('prefix', 'Fatura seri ön eki bulunamadı. Numara serisi ayarlarını kontrol edin.'),
    ('seri bulunamadı', 'Uygun fatura serisi bulunamadı. e-Süreç → Firma Ayarları → Fatura Serileri bölümünü kontrol edin.'),
]


def _sanitize_gib_error(raw_error: str) -> str:
    """
    Teknik hata mesajını kullanıcı dostu formata çevirir.

    1. Ham hata mesajı logger.error() ile backend'e yazılır.
    2. Bilinen kalıplar kontrol edilir → eşleşen kullanıcı dostu mesaj döner.
    3. Hiçbir kalıp eşleşmezse genel bir mesaj döner.

    Kullanım:
        gib_error = _sanitize_gib_error(api_resp.get('error_msg', ''))
        invoice.gib_error = gib_error[:500]
    """
    if not raw_error:
        return ''

    raw_lower = raw_error.lower()

    for pattern, friendly_msg in _ERROR_PATTERNS:
        if pattern.lower() in raw_lower:
            return friendly_msg

    # KP-02: HTML etiketlerini sıyır (XSS önlemi — SweetAlert .text() ile kullanılsa bile
    # gib_error DB'ye yazılıyor ve başka yerlerde render edilebilir)
    clean = re.sub(r'<[^>]+>', '', raw_error).strip()

    # Eğer mesaj zaten kısa ve sade ise (100 karakter altı, SQL/traceback içermiyorsa) olduğu gibi döndür
    if len(clean) <= 100 and 'traceback' not in raw_lower and 'sql' not in raw_lower:
        return clean

    return 'Beklenmeyen bir GİB hatası oluştu. Lütfen tekrar deneyin veya destek alın.'


def _validate_tckn(tckn: str) -> bool:
    """
    KP-04: 11 haneli TCKN checksum doğrulaması.
    Algoritma: T.C. Kimlik Numarası doğrulama kuralları.
    """
    if len(tckn) != 11 or not tckn.isdigit() or tckn[0] == '0':
        return False
    digits = [int(d) for d in tckn]
    # 10. hane: (toplam(tek pozisyonlar) * 7 - toplam(çift pozisyonlar)) mod 10
    odd_sum = sum(digits[i] for i in range(0, 9, 2))
    even_sum = sum(digits[i] for i in range(1, 8, 2))
    if (odd_sum * 7 - even_sum) % 10 != digits[9]:
        return False
    # 11. hane: ilk 10 hanenin toplamı mod 10
    if sum(digits[:10]) % 10 != digits[10]:
        return False
    return True


def _validate_vkn(vkn: str) -> bool:
    """KP-04: 10 haneli VKN format doğrulaması."""
    return len(vkn) == 10 and vkn.isdigit()


def _validate_customer_for_esurec(invoice) -> str | None:
    """
    Faturanın müşteri bilgilerini (VKN/TCKN) kontrol eder.
    e-Fatura/e-Arşiv gönderiminde geçerli müşteri kimliği zorunludur, ANCAK
    nihai tüketici (perakende) faturalarında yasal sınır altında fallback
    uygulanabilir.

    KP-04: Format doğrulaması (10 hane VKN, 11 hane TCKN checksum).
    KP-13: Nihai tüketici fallback — boş VKN/TCKN durumlarında threshold
           altı perakende satışlarda 11111111111 kullanılmasına izin verilir.
           Serializer bu durumu zaten destekliyor (esurec_serializers._build_buyer).

    Returns:
        None  → Müşteri geçerli (veya fallback uygulanabilir), işleme devam.
        str   → Hata mesajı, doğrudan JsonResponse error_msg'ye yazılabilir.

    Bloke edilen durumlar:
      - VKN/TCKN FORMAT hatalı (yanlış uzunluk, checksum hatası)
      - Tutar threshold'un ÜSTÜNDE ve kimlik bilgisi boş (MASAK zorunluluğu)

    İzin verilen durumlar (fallback ile):
      - Müşteri atanmamış + tutar ≤ threshold → 'Muhtelif Müşteri' + 11111111111
      - Müşteri atanmış ama VKN boş + tutar ≤ threshold → 11111111111
    """
    invoice_total = _invoice_total_for_threshold(invoice)

    # ── DURUM 1: Müşteri de Tedarikçi de atanmamış ──
    if not invoice.customer and not invoice.supplier:
        if invoice_total > NIHAI_TUKETICI_THRESHOLD_TL:
            return (
                f'Fatura {invoice.invoice_no}: Tutar '
                f'{invoice_total:.2f} TL, yasal nihai tüketici sınırının '
                f'({NIHAI_TUKETICI_THRESHOLD_TL:.0f} TL) üzerinde. '
                f'MASAK mevzuatı gereği kimlik tespiti yapılmış bir müşteri '
                f'atanması zorunludur.'
            )
        # Threshold altı → serializer "Muhtelif Müşteri" + 11111111111 kullanır.
        log.info(
            "[KP-13] Nihai tüketici fallback (müşteri yok): fatura=%s tutar=%s",
            invoice.invoice_no, invoice_total,
        )
        return None

    # ── DURUM 2: Müşteri atanmış ──
    if invoice.customer:
        vkn = (invoice.customer.identification_number or '').strip()
        customer_name = f'{invoice.customer.first_name or ""} {invoice.customer.last_name or ""}'.strip()

        if not vkn:
            # KP-13: Threshold altı nihai tüketici → fallback'e izin ver
            if invoice_total > NIHAI_TUKETICI_THRESHOLD_TL:
                return (
                    f'Fatura {invoice.invoice_no}: Müşteri "{customer_name}" için '
                    f'TCKN/VKN tanımlı değil ve tutar '
                    f'{invoice_total:.2f} TL yasal sınırın '
                    f'({NIHAI_TUKETICI_THRESHOLD_TL:.0f} TL) üzerinde. '
                    f'MASAK mevzuatı gereği bu tutardaki işlem için kimlik '
                    f'numarası zorunludur.'
                )
            log.info(
                "[KP-13] Nihai tüketici fallback: fatura=%s müşteri=%s tutar=%s",
                invoice.invoice_no, customer_name or '(isim yok)', invoice_total,
            )
            return None

        # VKN/TCKN dolu → FORMAT doğrulaması (her durumda bloke)
        # Nihai tüketici sabitleri (11111111111 / 22222222222) format kontrolünden muaf
        if vkn in (NIHAI_TUKETICI_TCKN, NIHAI_TUKETICI_VKN_YABANCI):
            return None

        if len(vkn) == 11:
            if not _validate_tckn(vkn):
                return (
                    f'Fatura {invoice.invoice_no}: Müşteri "{customer_name}" için '
                    f'girilen TCKN ({vkn}) geçersiz. '
                    f'11 haneli T.C. Kimlik Numarası doğrulama algoritmasına uymuyor.'
                )
        elif len(vkn) == 10:
            if not _validate_vkn(vkn):
                return (
                    f'Fatura {invoice.invoice_no}: Müşteri "{customer_name}" için '
                    f'girilen VKN ({vkn}) geçersiz. VKN 10 haneli rakam olmalıdır.'
                )
        else:
            return (
                f'Fatura {invoice.invoice_no}: Müşteri "{customer_name}" için '
                f'girilen numara ({vkn}) geçersiz. '
                f'10 haneli VKN veya 11 haneli TCKN girilmelidir.'
            )

    # ── DURUM 3: Tedarikçi atanmış (B2B — fallback uygulanmaz) ──
    elif invoice.supplier:
        vkn = (invoice.supplier.tax_number or '').strip()
        supplier_name = (invoice.supplier.company_name or '').strip()
        if not vkn:
            # Tedarikçi faturaları (PURCHASE/Gider Pusulası) için VKN zorunludur.
            # B2B senaryoda nihai tüketici fallback'i anlamsız olur.
            return (
                f'Fatura {invoice.invoice_no}: Tedarikçi "{supplier_name}" için '
                f'VKN bilgisi tanımlı değil. '
                f'E-Fatura/E-Arşiv göndermek için geçerli bir vergi numarası zorunludur.'
            )
        # KP-04: Tedarikçi VKN format doğrulaması
        if not _validate_vkn(vkn) and not _validate_tckn(vkn):
            return (
                f'Fatura {invoice.invoice_no}: Tedarikçi "{supplier_name}" için '
                f'girilen VKN/TCKN ({vkn}) geçersiz formatdadır.'
            )

    return None


def _check_esurec_activation(store):
    """
    Mağaza için aktif e-Süreç aktivasyonu (EsurecTenantCredential) var mı kontrol eder.

    Returns:
        None  → Aktivasyon mevcut, işleme devam edilebilir.
        dict  → Aktivasyon yok veya hata; dönen dict doğrudan JsonResponse'a verilebilir.
    """
    try:
        cred = EsurecTenantCredential.objects.filter(
            store=store, is_active=True,
        ).first()
        if not cred:
            log.error(
                "e-Süreç aktivasyon kontrolü başarısız: store=%s — aktif credential kaydı yok.",
                store.id,
            )
            return {
                'result': False,
                'error_msg': 'Bu mağaza için e-Süreç aktivasyonu bulunmamaktadır. '
                             'Lütfen yönetici ile iletişime geçin.',
            }
        return None
    except Exception as e:
        log.exception(
            "e-Süreç aktivasyon kontrolü sırasında beklenmeyen hata: store=%s hata=%s",
            store.id, e,
        )
        return {
            'result': False,
            'error_msg': 'Bu mağaza için e-Süreç aktivasyonu bulunmamaktadır. '
                         'Lütfen yönetici ile iletişime geçin.',
        }


# ======================================================================
# 1. e-SÜREÇ'E TASLAK GÖNDER
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_send_invoice(request):
    """
    Fatura(ları) e-Süreç'e taslak olarak gönderir.

    Celery varsa → kuyruğa ekler, anında "Kuyruğa alındı" döner.
    Celery yoksa → senkron olarak gönderir (fallback).

    Durum akışı: DRAFT → QUEUED (e-Süreç'e gönderilince)
    GİB onayı gelmedikçe fatura "Kesildi (ISSUED)" olarak işaretlenmez.
    """
    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        invoice_ids = _get_ids_from_request(request)

        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Gönderilecek fatura seçilmedi.'})

        # SY-02: PavoPOS faturalarını filtrele — ÖKC zaten GİB'e gönderiyor,
        # e-Süreç üzerinden tekrar gönderim çift fatura riskine yol açar.
        pavo_invoices = Invoice.objects.filter(
            id__in=invoice_ids, store=store, is_deleted=False,
        ).exclude(pavo_sale_number='').values_list('invoice_no', flat=True)
        if pavo_invoices:
            pavo_list = ', '.join(pavo_invoices[:5])
            return JsonResponse({
                'result': False,
                'error_msg': f'PavoPOS faturaları e-Süreç üzerinden gönderilemez '
                             f'(ÖKC zaten GİB entegrasyonunu yapmaktadır): {pavo_list}',
            })

        use_queue = _use_celery()

        if use_queue:
            from apps.invoices.tasks import send_invoice_to_esurec_task

            sent_count = 0
            skipped_count = 0
            validation_errors = []

            for inv_id in invoice_ids:
                try:
                    invoice = Invoice.objects.select_related(
                        'customer', 'supplier',
                    ).get(id=inv_id, store=store, is_deleted=False)

                    # ── KRİTİK: Müşteri VKN/TCKN validasyonu ──
                    customer_err = _validate_customer_for_esurec(invoice)
                    if customer_err:
                        validation_errors.append(customer_err)
                        continue

                    if _get_esurec_id(invoice):
                        skipped_count += 1
                        continue

                    InvoiceSyncLog.objects.create(
                        invoice=invoice,
                        store=store,
                        action='SEND_TO_ESUREC',
                        status='QUEUED',
                    )

                    if invoice.status == Invoice.Status.DRAFT:
                        invoice.status = Invoice.Status.QUEUED
                        invoice.save(update_fields=['status', 'updated_at'])

                    send_invoice_to_esurec_task.delay(str(inv_id), str(store.id))
                    sent_count += 1

                except Invoice.DoesNotExist:
                    continue

            msg_parts = []
            if sent_count:
                msg_parts.append(f'{sent_count} fatura gönderiliyor')
            if skipped_count:
                msg_parts.append(f'{skipped_count} fatura zaten gönderilmiş')
            if validation_errors:
                msg_parts.append(f'{len(validation_errors)} fatura müşteri bilgisi eksik')

            # Tüm faturalar müşteri eksik ise hata döndür
            if validation_errors and sent_count == 0 and skipped_count == 0:
                return JsonResponse({
                    'result': False,
                    'error_msg': validation_errors[0],
                    'validation_errors': validation_errors[:5],
                    'mode': 'async',
                })

            return JsonResponse({
                'result': sent_count > 0,
                'msg': ', '.join(msg_parts) + '.',
                'queued': sent_count,
                'skipped': skipped_count,
                'validation_errors': validation_errors[:5] if validation_errors else [],
                'mode': 'async',
            })

        else:
            # ---- SENKRON MODU (Celery yoksa fallback) ----
            return _sync_send_to_esurec(store, invoice_ids)

    except ValueError as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})
    except Exception as e:
        # KP-11: str(e) yerine type(e).__name__
        log.exception(f"esurec_send_invoice hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


def _sync_send_to_esurec(store, invoice_ids):
    """
    Senkron gönderim (Celery olmadığında).
    Başarı → DRAFT (gib_status_code='10', e-Süreç taslağı)
    Hata   → ERROR (gib_error dolu)
    """
    from apps.invoices.esurec_serializers import determine_invoice_scenario

    client = ESurecClient()
    results = []
    success_count = 0

    for inv_id in invoice_ids:
        try:
            invoice = Invoice.objects.prefetch_related('items').select_related(
                'customer', 'supplier',
            ).get(id=inv_id, store=store, is_deleted=False)

            # ── KRİTİK: Müşteri VKN/TCKN validasyonu ──
            customer_err = _validate_customer_for_esurec(invoice)
            if customer_err:
                results.append({
                    'invoice_no': invoice.invoice_no, 'result': False,
                    'error_msg': customer_err,
                })
                continue

            existing_id = _get_esurec_id(invoice)
            if existing_id:
                if invoice.status == Invoice.Status.QUEUED:
                    invoice.status = Invoice.Status.DRAFT
                    invoice.gib_status_code = '10'
                    invoice.gib_status_desc = 'e-Süreç taslağı (zaten mevcut)'
                    invoice.save(update_fields=[
                        'status', 'gib_status_code', 'gib_status_desc', 'updated_at',
                    ])
                results.append({
                    'invoice_no': invoice.invoice_no, 'result': True,
                    'msg': 'Zaten gönderilmiş.', 'esurec_id': existing_id,
                })
                success_count += 1
                continue

            scenario, is_einvoice, gib_info = determine_invoice_scenario(
                invoice, esurec_client=client
            )

            with transaction.atomic():
                invoice.scenario = scenario
                invoice.doc_class = 'E_INVOICE' if is_einvoice else 'E_ARCHIVE'
                invoice.is_einvoice = is_einvoice
                invoice.save(update_fields=['scenario', 'doc_class', 'is_einvoice', 'updated_at'])

            payload = serialize_invoice_for_esurec(
                invoice, scenario=scenario, is_einvoice=is_einvoice, gib_info=gib_info
            )
            api_resp = client.send_invoice(payload)

            if api_resp.get('result'):
                esurec_id = api_resp.get('esurec_invoice_id', '')
                doc_label = 'e-Fatura' if is_einvoice else 'e-Arşiv'
                with transaction.atomic():
                    if esurec_id:
                        _set_esurec_id(invoice, esurec_id)
                    invoice.status = Invoice.Status.DRAFT
                    invoice.gib_status_code = str(api_resp.get('gib_status_code', '10'))
                    invoice.gib_status_desc = 'e-Süreç taslağı oluşturuldu'
                    invoice.gib_error = ''
                    invoice.save(update_fields=[
                        'status', 'gib_status_code',
                        'gib_status_desc', 'gib_error', 'updated_at',
                    ])

                results.append({
                    'invoice_no': invoice.invoice_no, 'result': True,
                    'esurec_id': esurec_id,
                    'msg': f'{doc_label} olarak e-Süreç\'e gönderildi.',
                    'doc_type': doc_label,
                })
                success_count += 1
            else:
                # e-Süreç JSON hata yakalama ({"status":"error","message":"..."} dahil)
                raw_error = (
                    api_resp.get('error_msg')
                    or api_resp.get('message')
                    or 'Bilinmeyen hata'
                )
                # Teknik hata detayını backend loguna yaz
                log.error(
                    f"esurec_send_invoice hata: invoice={invoice.invoice_no}, "
                    f"raw_error={raw_error[:500]}"
                )
                # Kullanıcı dostu mesajı DB'ye kaydet
                friendly_error = _sanitize_gib_error(raw_error)
                with transaction.atomic():
                    invoice.status = Invoice.Status.ERROR
                    invoice.gib_error = friendly_error[:500]
                    invoice.save(update_fields=['status', 'gib_error', 'updated_at'])

                results.append({
                    'invoice_no': invoice.invoice_no, 'result': False,
                    'error_msg': friendly_error,
                })

        except Invoice.DoesNotExist:
            results.append({'invoice_id': inv_id, 'result': False, 'error_msg': 'Fatura bulunamadı.'})
        except Exception as e:
            raw_exc = str(e)[:500]
            log.exception(f"esurec_send_invoice exception: invoice_id={inv_id}, error={raw_exc}")
            friendly = _sanitize_gib_error(raw_exc)
            results.append({'invoice_id': inv_id, 'result': False, 'error_msg': friendly})

    return JsonResponse({
        'result': success_count > 0,
        'msg': f'{success_count}/{len(invoice_ids)} fatura e-Süreç\'e gönderildi.',
        'details': results,
        'mode': 'sync',
    })


# ======================================================================
# 2. GİB'E GÖNDER
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_send_to_gib(request):
    """
    GİB'e gönder — kuyruk varsa async, yoksa senkron.

    Bu işlem sonucunda fatura statüsü SENT olur.
    GİB'den 1300 (onay) kodu gelene kadar fatura ISSUED olmaz.
    """
    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        invoice_ids = _get_ids_from_request(request)

        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Fatura seçilmedi.'})

        use_queue = _use_celery()

        if use_queue:
            from apps.invoices.tasks import send_invoice_to_gib_task

            queued = 0
            errors = []

            for inv_id in invoice_ids:
                try:
                    invoice = Invoice.objects.select_related(
                        'customer', 'supplier',
                    ).get(id=inv_id, store=store, is_deleted=False)

                    # ── KRİTİK: Müşteri VKN/TCKN validasyonu ──
                    customer_err = _validate_customer_for_esurec(invoice)
                    if customer_err:
                        errors.append(customer_err)
                        continue

                    esurec_id = _get_esurec_id(invoice)

                    if invoice.gib_status_code and str(invoice.gib_status_code) in [
                        '100', '1000', '1100', '1200', '1300'
                    ]:
                        errors.append(f'{invoice.invoice_no}: Zaten GİB sürecinde.')
                        continue

                    InvoiceSyncLog.objects.create(
                        invoice=invoice, store=store,
                        action='SEND_TO_GIB', status='QUEUED',
                        esurec_invoice_id=esurec_id or '',
                    )

                    if invoice.status == Invoice.Status.DRAFT:
                        invoice.status = Invoice.Status.QUEUED
                        invoice.save(update_fields=['status', 'updated_at'])

                    send_invoice_to_gib_task.delay(str(inv_id), str(store.id))
                    queued += 1

                except Invoice.DoesNotExist:
                    errors.append(f'{inv_id}: Fatura bulunamadı.')

            msg = f'{queued} fatura GİB\'e gönderiliyor.'
            if errors:
                msg += f' ({len(errors)} atlandı: {"; ".join(errors[:3])})'

            return JsonResponse({'result': queued > 0, 'msg': msg, 'mode': 'async'})

        else:
            return _sync_send_to_gib(store, invoice_ids)

    except Exception as e:
        log.exception(f"esurec_send_to_gib hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


def _sync_send_to_gib(store, invoice_ids):
    """
    Senkron GİB gönderim (iki adımlı akış).
    ESUREC_ID yoksa önce taslak oluşturur, sonra GİB'e gönderir.
    Başarı → SENT (gib_status_code='100')
    Hata   → ERROR (gib_error dolu)
    """
    from apps.invoices.esurec_serializers import (
        determine_invoice_scenario,
    )

    client = ESurecClient()
    results = []
    success_count = 0

    for inv_id in invoice_ids:
        try:
            invoice = Invoice.objects.prefetch_related('items').select_related(
                'customer', 'supplier',
            ).get(id=inv_id, store=store, is_deleted=False)

            # ── KRİTİK: Müşteri VKN/TCKN validasyonu ──
            customer_err = _validate_customer_for_esurec(invoice)
            if customer_err:
                results.append({
                    'invoice_no': invoice.invoice_no, 'result': False,
                    'error_msg': customer_err,
                })
                continue

            esurec_id = _get_esurec_id(invoice)

            if invoice.gib_status_code and str(invoice.gib_status_code) in [
                '100', '1000', '1100', '1200', '1300'
            ]:
                results.append({
                    'invoice_no': invoice.invoice_no, 'result': False,
                    'error_msg': f'Zaten GİB sürecinde (Kod: {invoice.gib_status_code})',
                })
                continue

            # ADIM 1: ESUREC_ID yoksa önce taslak oluştur
            if not esurec_id:
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
                    raw_error = draft_resp.get('error_msg', 'Taslak oluşturulamadı')
                    log.error(
                        f"esurec_send_to_gib taslak hata: invoice={invoice.invoice_no}, "
                        f"raw_error={raw_error[:500]}"
                    )
                    friendly_error = _sanitize_gib_error(raw_error)
                    with transaction.atomic():
                        invoice.status = Invoice.Status.ERROR
                        invoice.gib_error = friendly_error[:500]
                        invoice.save(update_fields=['status', 'gib_error', 'updated_at'])
                    results.append({
                        'invoice_no': invoice.invoice_no, 'result': False,
                        'error_msg': friendly_error,
                    })
                    continue

                esurec_id = draft_resp.get('esurec_invoice_id', '')
                if not esurec_id:
                    results.append({
                        'invoice_no': invoice.invoice_no, 'result': False,
                        'error_msg': 'e-Süreç taslak oluşturuldu ama ID dönmedi.',
                    })
                    continue

                with transaction.atomic():
                    _set_esurec_id(invoice, esurec_id)
                    invoice.gib_status_code = '10'
                    invoice.gib_status_desc = 'e-Süreç taslağı oluşturuldu'
                    invoice.save(update_fields=[
                        'gib_status_code', 'gib_status_desc', 'updated_at',
                    ])

            # ADIM 2: GİB'e gönder
            api_resp = client.send_to_gib(esurec_id)

            if api_resp.get('result'):
                with transaction.atomic():
                    invoice.gib_status_code = str(api_resp.get('gib_status_code', '100'))
                    invoice.gib_status_desc = 'GİB\'e gönderildi (e-Süreç)'
                    invoice.status = Invoice.Status.SENT
                    invoice.gib_error = ''
                    doc_no = api_resp.get('invoice_number')
                    if doc_no:
                        invoice.document_number = doc_no
                    invoice.save(update_fields=[
                        'gib_status_code', 'gib_status_desc', 'status',
                        'gib_error', 'document_number', 'updated_at',
                    ])

                # Fatura kontör düşümü kaldırıldı (Nisan 2026)
                # try:
                #     s, _ = StoreEInvoiceSettings.objects.get_or_create(store=store)
                #     if s.enabled:
                #         s.consume(1)
                # except Exception:
                #     pass

                results.append({
                    'invoice_no': invoice.invoice_no, 'result': True,
                    'gib_status_code': invoice.gib_status_code,
                })
                success_count += 1
            else:
                # e-Süreç JSON hata yakalama ({"status":"error","message":"..."} dahil)
                raw_error = (
                    api_resp.get('error_msg')
                    or api_resp.get('message')
                    or 'GİB gönderimi başarısız'
                )
                log.error(
                    f"esurec_send_to_gib GİB hata: invoice={invoice.invoice_no}, "
                    f"raw_error={raw_error[:500]}"
                )
                friendly_error = _sanitize_gib_error(raw_error)
                with transaction.atomic():
                    invoice.status = Invoice.Status.ERROR
                    invoice.gib_error = friendly_error[:500]
                    invoice.save(update_fields=['status', 'gib_error', 'updated_at'])

                results.append({
                    'invoice_no': invoice.invoice_no, 'result': False,
                    'error_msg': friendly_error,
                })

        except Invoice.DoesNotExist:
            results.append({
                'invoice_id': inv_id, 'result': False, 'error_msg': 'Fatura bulunamadı.',
            })
        except Exception as e:
            # Beklenmeyen exception durumunda da faturayı ERROR yap
            raw_exc = str(e)[:500]
            log.exception(f"esurec_send_to_gib exception: invoice_id={inv_id}, error={raw_exc}")
            friendly = _sanitize_gib_error(raw_exc)
            try:
                inv_for_err = Invoice.objects.filter(id=inv_id, is_deleted=False).first()
                if inv_for_err:
                    inv_for_err.status = Invoice.Status.ERROR
                    inv_for_err.gib_error = friendly[:500]
                    inv_for_err.save(update_fields=['status', 'gib_error', 'updated_at'])
            except Exception:
                pass
            results.append({
                'invoice_id': inv_id, 'result': False, 'error_msg': friendly,
            })

    return JsonResponse({
        'result': success_count > 0,
        'msg': f'{success_count}/{len(invoice_ids)} fatura GİB\'e gönderildi.',
        'details': results, 'mode': 'sync',
    })


# ======================================================================
# 3. DURUM SORGULA
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_check_status(request):
    """
    e-Süreç üzerinden GİB durumunu sorgular.

    KRİTİK KURAL: Fatura yalnızca GİB'den 1300 kodu (onay) geldiğinde
    ISSUED (Fatura Kesildi) statüsüne alınır. Diğer durumlarda SENT kalır.

    Özel parametre:
        sync_all: true  → Seçili fatura olmadan, GİB'e gönderilmiş
                          SENT/QUEUED/APPROVED tüm faturaları senkronize eder.
    """
    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        client = ESurecClient()

        # sync_all modu: Tüm GİB sürecindeki faturaları toplu senkronize et
        try:
            body = json.loads(request.body.decode('utf-8'))
            sync_all = body.get('sync_all', False)
        except Exception:
            sync_all = False

        invoice_ids = _get_ids_from_request(request)

        if sync_all and not invoice_ids:
            # SENT, QUEUED, APPROVED durumundaki tüm faturaları bul
            # KP-07: notes yerine InvoiceSyncLog'da esurec_invoice_id olan faturaları bul
            synced_invoice_ids = InvoiceSyncLog.objects.filter(
                store=store,
                action=InvoiceSyncLog.Action.SEND_TO_ESUREC,
                status=InvoiceSyncLog.Status.SUCCESS,
            ).exclude(esurec_invoice_id='').values_list('invoice_id', flat=True)

            sync_invoices = Invoice.objects.filter(
                id__in=synced_invoice_ids,
                store=store,
                is_deleted=False,
                status__in=[
                    Invoice.Status.SENT,
                    Invoice.Status.QUEUED,
                    Invoice.Status.APPROVED,
                ],
            ).values_list('id', flat=True)[:100]  # Max 100 fatura
            invoice_ids = [str(i) for i in sync_invoices]

            if not invoice_ids:
                return JsonResponse({
                    'result': True,
                    'msg': 'Senkronize edilecek fatura bulunamadı (GİB sürecinde fatura yok).',
                    'details': [],
                })

        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Fatura seçilmedi.'})

        results = []
        success_count = 0

        for inv_id in invoice_ids:
            try:
                invoice = Invoice.objects.get(id=inv_id, store=store, is_deleted=False)
                esurec_id = _get_esurec_id(invoice)

                if not esurec_id:
                    results.append({'invoice_no': invoice.invoice_no, 'result': False, 'error_msg': 'e-Süreç ID yok.'})
                    continue

                api_resp = client.check_status(esurec_id)

                if api_resp.get('result'):
                    new_code = api_resp.get('gib_status_code')
                    new_desc = api_resp.get('gib_status_description', '')

                    if new_code:
                        invoice.gib_status_code = str(new_code)
                    if new_desc:
                        invoice.gib_status_desc = new_desc

                    # ── KRİTİK: gib_error'u TEMİZLE ──
                    # Başarılı durum güncellemesinde eski hata mesajı kalmamalı.
                    # Hata kodları (1400, 1500) için new_desc zaten hata detayını içerir.
                    invoice.gib_error = ''

                    # KRİTİK: Sadece 1300 (GİB onayı) geldiğinde ISSUED (Fatura Kesildi) olur.
                    # Diğer durumlar: QUEUED, SENT kalır. 1400+ hata/red → ERROR/REJECTED.
                    status_map = {
                        '1300': Invoice.Status.ISSUED,    # GİB onayladı → Fatura Kesildi
                        '1230': Invoice.Status.CANCELED,  # İptal edildi
                        '1200': Invoice.Status.SENT,      # GİB'e iletildi, bekliyor
                        '1100': Invoice.Status.SENT,      # İşleniyor
                        '1000': Invoice.Status.QUEUED,    # Kuyrukta
                        '100':  Invoice.Status.SENT,      # Kabul edildi, işleniyor
                        '1400': Invoice.Status.ERROR,     # Hata
                        '1500': Invoice.Status.REJECTED,  # Reddedildi
                    }
                    new_status = status_map.get(str(new_code))
                    if new_status:
                        invoice.status = new_status

                    doc_no = api_resp.get('invoice_number')
                    if doc_no:
                        invoice.document_number = doc_no

                    invoice.save(update_fields=[
                        'gib_status_code', 'gib_status_desc', 'gib_error',
                        'status', 'document_number', 'updated_at',
                    ])

                    # Sync log kaydını güncelle
                    InvoiceSyncLog.objects.filter(
                        invoice=invoice,
                        action='SEND_TO_GIB',
                        status__in=['QUEUED', 'PROCESSING'],
                    ).update(
                        status='SUCCESS' if new_status == Invoice.Status.ISSUED else 'PROCESSING',
                    )

                    results.append({'invoice_no': invoice.invoice_no, 'result': True,
                                    'gib_status_code': invoice.gib_status_code,
                                    'new_status': invoice.status})
                    success_count += 1
                else:
                    # e-Süreç sorgulama başarısız — hatayı DB'ye yaz
                    raw_error = api_resp.get('error_msg', 'Sorgulama başarısız')
                    log.error(
                        f"esurec_check_status hata: invoice={invoice.invoice_no}, "
                        f"raw_error={raw_error[:500]}"
                    )
                    friendly_error = _sanitize_gib_error(raw_error)
                    invoice.gib_error = friendly_error[:500]
                    invoice.save(update_fields=['gib_error', 'updated_at'])

                    results.append({'invoice_no': invoice.invoice_no, 'result': False,
                                    'error_msg': friendly_error})

            except Invoice.DoesNotExist:
                results.append({'invoice_id': inv_id, 'result': False, 'error_msg': 'Fatura bulunamadı.'})

        return JsonResponse({
            'result': success_count > 0,
            'msg': f'{success_count} faturanın durumu güncellendi.',
            'details': results,
        })

    except Exception as e:
        log.exception(f"esurec_check_status hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


# ======================================================================
# 4. KUYRUK DURUMUNU SORGULA (Frontend polling için)
# ======================================================================

@login_required(login_url='login')
@require_GET
def esurec_queue_status(request):
    """
    Kuyruktaki işlemlerin durumunu döndürür.
    Frontend bunu 3-5 saniyede bir polling yaparak sorgular.

    GET /invoices/api/esurec/queue-status/?invoice_ids=id1,id2,...
    """
    try:
        store = _user_store(request)
        ids_param = request.GET.get('invoice_ids', '')
        invoice_ids = [i.strip() for i in ids_param.split(',') if i.strip()]

        if not invoice_ids:
            return JsonResponse({'result': True, 'items': []})

        # Her fatura için en son log kaydını getir
        logs = InvoiceSyncLog.objects.filter(
            invoice_id__in=invoice_ids, store=store
        ).order_by('invoice_id', '-created_at').distinct('invoice_id')

        items = []
        seen = set()
        for log_entry in logs:
            if str(log_entry.invoice_id) in seen:
                continue
            seen.add(str(log_entry.invoice_id))

            items.append({
                'invoice_id': str(log_entry.invoice_id),
                'action': log_entry.action,
                'status': log_entry.status,
                'error_message': log_entry.error_message or '',
                'esurec_invoice_id': log_entry.esurec_invoice_id or '',
                'attempt': log_entry.attempt,
                'updated_at': log_entry.updated_at.strftime('%d/%m/%Y %H:%M:%S'),
            })

        # Tamamlanmamış iş var mı?
        pending = any(i['status'] in ('QUEUED', 'PROCESSING', 'RETRYING') for i in items)

        return JsonResponse({
            'result': True,
            'items': items,
            'has_pending': pending,
        })

    except Exception as e:
        log.exception(f"esurec_queue_status hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


# ======================================================================
# 5. CANLI PDF PROXY (e-Süreç'ten PDF çekip doğrudan tarayıcıya sunma)
# ======================================================================

@login_required(login_url='login')
def esurec_get_pdf(request):
    """
    Canlı PDF Proxy — e-Süreç'ten PDF alıp doğrudan tarayıcıya sunar.

    MİMARİ:
        Kuyum Plus → ESurecClient → e-Süreç ExternalInvoicePDFView → MySoft → PDF
        Dönen PDF Base64'ten decode edilip HttpResponse ile stream edilir.
        Kuyum Plus modelinde (pdf_file, xml_file) HİÇBİR ŞEY KAYDEDİLMEZ.

    Kullanım:
        GET /invoices/api/esurec/pdf/?invoice_id=xxx
        → Başarılı: HttpResponse(pdf_bytes, content_type='application/pdf')
        → Başarısız: HTML hata sayfası (iframe içinde gösterilir)
    """
    inv_id = request.GET.get('invoice_id') or request.POST.get('invoice_id')

    if not inv_id:
        return HttpResponse(
            '<html><body style="font-family:sans-serif;padding:40px;text-align:center;">'
            '<h3 style="color:#d9534f;">Hata</h3>'
            '<p>Fatura ID belirtilmedi.</p></body></html>',
            content_type='text/html; charset=utf-8',
            status=400,
        )

    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            msg = activation_error.get('error_msg', 'e-Süreç aktivasyonu bulunamadı.')
            return HttpResponse(
                f'<html><body style="font-family:sans-serif;padding:40px;text-align:center;">'
                f'<h3 style="color:#d9534f;">Aktivasyon Hatası</h3>'
                f'<p>{msg}</p></body></html>',
                content_type='text/html; charset=utf-8',
                status=403,
            )

        invoice = get_object_or_404(Invoice, id=inv_id, store=store, is_deleted=False)
        esurec_id = _get_esurec_id(invoice)

        if not esurec_id:
            return HttpResponse(
                '<html><body style="font-family:sans-serif;padding:40px;text-align:center;">'
                '<h3 style="color:#d9534f;">PDF Bulunamadı</h3>'
                '<p>Bu fatura henüz e-Süreç\'e gönderilmemiş. Mühürlü PDF alınamıyor.</p>'
                '</body></html>',
                content_type='text/html; charset=utf-8',
                status=404,
            )

        client = ESurecClient()
        resp = client.get_pdf(esurec_id)

        if resp.get('result'):
            pdf_b64 = resp.get('pdf_base64')

            if pdf_b64:
                pdf_bytes = base64.b64decode(pdf_b64)
                safe_name = (invoice.invoice_no or 'fatura').replace('"', '')
                response = HttpResponse(pdf_bytes, content_type='application/pdf')
                response['Content-Disposition'] = f'inline; filename="fatura_{safe_name}.pdf"'
                response['Content-Length'] = len(pdf_bytes)
                # LOKAL KAYIT YOK: pdf_file alanına dokunulmaz
                return response

            # pdf_base64 yoksa ama pdf_url varsa → yönlendir
            pdf_url = resp.get('pdf_url')
            if pdf_url:
                from django.shortcuts import redirect
                return redirect(pdf_url)

            return HttpResponse(
                '<html><body style="font-family:sans-serif;padding:40px;text-align:center;">'
                '<h3 style="color:#d9534f;">PDF Verisi Alınamadı</h3>'
                '<p>e-Süreç PDF döndü ancak içerik boş.</p></body></html>',
                content_type='text/html; charset=utf-8',
                status=502,
            )

        # e-Süreç'ten hata döndü
        error_msg = resp.get('error_msg', 'e-Süreç\'ten PDF alınamadı.')
        log.warning(
            "esurec_get_pdf proxy hatası: invoice_id=%s, esurec_id=%s, error=%s",
            inv_id, esurec_id, error_msg,
        )
        return HttpResponse(
            f'<html><body style="font-family:sans-serif;padding:40px;text-align:center;">'
            f'<h3 style="color:#d9534f;">PDF Alınamadı</h3>'
            f'<p>{error_msg}</p>'
            f'<p style="color:#999;font-size:12px;">Tekrar denemek için sayfayı yenileyin.</p>'
            f'</body></html>',
            content_type='text/html; charset=utf-8',
            status=502,
        )

    except Exception as e:
        # KP-11: str(e) yerine type(e).__name__ — kullanıcıya gösterilecek HTML'de
        # API anahtarları veya yanıt detayları sızabilir
        log.exception("esurec_get_pdf beklenmeyen hata: %s", type(e).__name__)
        return HttpResponse(
            '<html><body style="font-family:sans-serif;padding:40px;text-align:center;">'
            '<h3 style="color:#d9534f;">Sistem Hatası</h3>'
            '<p>PDF alınırken beklenmeyen hata oluştu. Lütfen tekrar deneyin.</p></body></html>',
            content_type='text/html; charset=utf-8',
            status=500,
        )


# ======================================================================
# 6. CANLI XML PROXY (e-Süreç'ten XML çekip doğrudan tarayıcıya sunma)
# ======================================================================

@login_required(login_url='login')
def esurec_get_xml(request):
    """
    Canlı XML Proxy — e-Süreç'ten UBL XML alıp doğrudan tarayıcıya sunar.

    MİMARİ:
        Kuyum Plus → ESurecClient → e-Süreç ExternalInvoiceXMLView → XML
        Dönen XML Base64'ten decode edilip HttpResponse ile stream edilir.
        Kuyum Plus modelinde (xml_file) HİÇBİR ŞEY KAYDEDİLMEZ.

    Kullanım:
        GET /invoices/api/esurec/xml/?invoice_id=xxx
        → Başarılı: HttpResponse(xml_bytes, content_type='application/xml')
                    Content-Disposition: attachment (tarayıcı indirir)
        → Başarısız: JsonResponse (frontend AJAX blob handler'ı yakalar)
    """
    inv_id = request.GET.get('invoice_id') or request.POST.get('invoice_id')
    if not inv_id:
        return JsonResponse({'result': False, 'error_msg': 'Fatura ID gerekli.'})

    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        invoice = get_object_or_404(Invoice, id=inv_id, store=store, is_deleted=False)
        esurec_id = _get_esurec_id(invoice)

        if not esurec_id:
            return JsonResponse({
                'result': False,
                'error_msg': 'Bu fatura henüz e-Süreç\'e gönderilmemiş. XML alınamıyor.',
            })

        client = ESurecClient()
        resp = client.get_xml(esurec_id)

        if resp.get('result'):
            xml_b64 = resp.get('xml_base64')

            if xml_b64:
                xml_bytes = base64.b64decode(xml_b64)
                safe_name = (invoice.invoice_no or 'fatura').replace('"', '')
                response = HttpResponse(
                    xml_bytes, content_type='application/xml; charset=utf-8',
                )
                response['Content-Disposition'] = (
                    f'attachment; filename="fatura_{safe_name}.xml"'
                )
                response['Content-Length'] = len(xml_bytes)
                # LOKAL KAYIT YOK: xml_file alanına dokunulmaz
                return response

            return JsonResponse({
                'result': False,
                'error_msg': 'e-Süreç XML döndü ancak içerik boş.',
            })

        # e-Süreç'ten hata döndü
        error_msg = resp.get('error_msg', 'e-Süreç\'ten XML alınamadı.')
        log.warning(
            "esurec_get_xml proxy hatası: invoice_id=%s, esurec_id=%s, error=%s",
            inv_id, esurec_id, error_msg,
        )
        return JsonResponse({'result': False, 'error_msg': error_msg})

    except Exception as e:
        log.exception("esurec_get_xml beklenmeyen hata: %s", type(e).__name__)
        return JsonResponse({
            'result': False,
            'error_msg': 'XML alınırken beklenmeyen hata oluştu. Lütfen tekrar deneyin.',
        })


# ======================================================================
# 7. İPTAL
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_cancel_invoice(request):
    try:
        body = json.loads(request.body.decode('utf-8'))
        inv_id = body.get('invoice_id')
        reason = body.get('reason', '')
    except Exception:
        inv_id = request.POST.get('invoice_id')
        reason = request.POST.get('reason', '')

    if not inv_id:
        return JsonResponse({'result': False, 'error_msg': 'Fatura ID gerekli.'})

    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        invoice = get_object_or_404(Invoice, id=inv_id, store=store, is_deleted=False)
        esurec_id = _get_esurec_id(invoice)

        if not esurec_id:
            return JsonResponse({'result': False, 'error_msg': 'e-Süreç\'e gönderilmemiş.'})

        client = ESurecClient()
        resp = client.cancel_invoice(esurec_id, reason)

        if resp.get('result'):
            invoice.status = Invoice.Status.CANCELED
            invoice.gib_status_code = '1230'
            invoice.gib_status_desc = f'İptal: {reason}' if reason else 'İptal'
            invoice.save(update_fields=['status', 'gib_status_code', 'gib_status_desc', 'updated_at'])

        return JsonResponse(resp)

    except Exception as e:
        log.exception(f"esurec_cancel_invoice hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


# ======================================================================
# 8. GİB MÜKELLEFİYET SORGULA
# ======================================================================

@login_required(login_url='login')
def esurec_check_gib_user(request):
    vkn = request.GET.get('vkn', '').strip()
    if not vkn:
        return JsonResponse({'result': False, 'error_msg': 'VKN gerekli.'})
    try:
        client = ESurecClient()
        return JsonResponse(client.check_gib_user(vkn))
    except Exception as e:
        log.exception(f"esurec_check_gib_user hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


# ======================================================================
# 9-11. GİDER PUSULASI — esurec_expense_views.py'e taşındı (2026-04-18)
# ======================================================================
# Yeni modülde: payload nested yapı, esurec_voucher_id alan adı düzeltmesi,
# tenant izolasyonu (seller_vkn), InvoiceActivityLog kayıtları, reset_to_draft,
# cancel ve asenkron Celery gönderim view'ları.
#
# Geri-uyumluluk: eski import yolunu kullanan yerler için re-export.
# ======================================================================

from apps.invoices.esurec_expense_views import (  # noqa: F401
    esurec_send_expense,
    esurec_send_expense_to_gib,
    esurec_expense_status,
    esurec_cancel_expense,
    esurec_reset_expense_to_draft,
    esurec_expense_async_send,
)


def _legacy_serialize_expense_voucher(invoice) -> dict:
    """
    DEPRECATED 2026-04-18: yeni yapı için
    apps.invoices.esurec_expense_views._serialize_expense_voucher kullanın.
    Bu fonksiyon backward compatibility için tutuluyor; çağrıldığında
    yeni serializer'a yönlendirir.
    """
    from apps.invoices.esurec_expense_views import _serialize_expense_voucher as _new_ser
    return _new_ser(invoice)


# ======================================================================
# 12. HATA → TASLAK SIFIRLAMA (Düzenlemeye Al)
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_reset_to_draft(request):
    """
    ERROR statüsündeki bir faturayı DRAFT'a döndürür.

    Güvenlik Kuralları:
      - Sadece ERROR statüsündekiler reset edilebilir.
      - GİB'e ulaşmış (1000, 1100, 1200, 1300) faturalar reset edilemez.
      - e-Süreç'teki taslak varsa cancel edilir (best-effort).
      - İlgili InvoiceSyncLog kayıtları SKIPPED'a çekilir.
      - InvoiceActivityLog kaydı oluşturulur.

    POST body: {"invoice_id": "<uuid>"}
    Dönüş: {"result": true/false, "msg": "...", "invoice_no": "..."}
    """
    from apps.invoices.models import InvoiceActivityLog
    import uuid as _uuid

    try:
        store = _user_store(request)
        invoice_ids = _get_ids_from_request(request)

        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Fatura seçilmedi.'})

        results = []
        success_count = 0

        for inv_id in invoice_ids:
            try:
                invoice = Invoice.objects.get(
                    id=inv_id, store=store, is_deleted=False,
                )

                # ── GÜVENLİK KONTROL 1: Sadece ERROR statüsü ──
                if invoice.status != Invoice.Status.ERROR:
                    results.append({
                        'invoice_no': invoice.invoice_no, 'result': False,
                        'error_msg': (
                            f'Fatura "{invoice.invoice_no}" şu an '
                            f'"{invoice.get_status_display()}" durumunda. '
                            f'Yalnızca "Hata Aldı" durumundaki faturalar düzenlemeye alınabilir.'
                        ),
                    })
                    continue

                # ── GÜVENLİK KONTROL 2: GİB'e ulaşmış faturalar reset edilemez ──
                gib_sent_codes = ['1000', '1100', '1200', '1300']
                if str(invoice.gib_status_code or '') in gib_sent_codes:
                    results.append({
                        'invoice_no': invoice.invoice_no, 'result': False,
                        'error_msg': (
                            f'Fatura "{invoice.invoice_no}" GİB sürecine girmiş '
                            f'(Kod: {invoice.gib_status_code}). '
                            f'GİB\'e ulaşmış faturalar düzenlemeye alınamaz.'
                        ),
                    })
                    continue

                # ── ADIM 1: e-Süreç'teki taslağı iptal et (best-effort) ──
                esurec_id = _get_esurec_id(invoice)
                cancel_note = ''
                if esurec_id:
                    try:
                        client = ESurecClient()
                        cancel_resp = client.cancel_invoice(esurec_id)
                        if cancel_resp.get('result'):
                            cancel_note = ' e-Süreç taslağı iptal edildi.'
                        else:
                            cancel_note = ' e-Süreç taslak iptali başarısız (önemsiz — yeni taslak oluşturulacak).'
                            log.warning(
                                "esurec_reset_to_draft cancel başarısız: "
                                "invoice=%s esurec_id=%s resp=%s",
                                invoice.invoice_no, esurec_id,
                                str(cancel_resp)[:200],
                            )
                    except Exception as cancel_exc:
                        cancel_note = ' e-Süreç taslak iptali atlandı (bağlantı hatası).'
                        log.warning(
                            "esurec_reset_to_draft cancel exception: "
                            "invoice=%s error=%s",
                            invoice.invoice_no, type(cancel_exc).__name__,
                        )

                # ── ADIM 2: InvoiceSyncLog temizliği ──
                InvoiceSyncLog.objects.filter(
                    invoice=invoice,
                    status__in=['FAILED', 'QUEUED', 'PROCESSING', 'RETRYING'],
                ).update(status='SKIPPED')

                # ── ADIM 3: Fatura sıfırlama ──
                trace_id = _uuid.uuid4()

                with transaction.atomic():
                    invoice.status = Invoice.Status.DRAFT
                    invoice.gib_status_code = ''
                    invoice.gib_status_desc = ''
                    invoice.gib_error = ''
                    invoice.save(update_fields=[
                        'status', 'gib_status_code', 'gib_status_desc',
                        'gib_error', 'updated_at',
                    ])

                # ── ADIM 4: ActivityLog kaydı ──
                user_msg = (
                    f'Fatura "{invoice.invoice_no}" düzenleme moduna alındı.'
                    f'{cancel_note} '
                    f'Tarihi ve bilgileri güncelleyerek tekrar GİB\'e gönderebilirsiniz.'
                )
                InvoiceActivityLog.objects.create(
                    trace_id=trace_id,
                    invoice=invoice,
                    store=store,
                    level=InvoiceActivityLog.Level.INFO,
                    event=InvoiceActivityLog.Event.DRAFT_RESET,
                    user_message=user_msg[:500],
                )

                log.info(
                    "[RESET] Fatura %s → DRAFT'a döndürüldü. trace_id=%s",
                    invoice.invoice_no, trace_id,
                )

                results.append({
                    'invoice_no': invoice.invoice_no,
                    'result': True,
                    'msg': user_msg,
                })
                success_count += 1

            except Invoice.DoesNotExist:
                results.append({
                    'invoice_id': inv_id, 'result': False,
                    'error_msg': 'Fatura bulunamadı.',
                })

        if len(invoice_ids) == 1:
            return JsonResponse(results[0] if results else {
                'result': False, 'error_msg': 'İşlem yapılamadı.',
            })

        return JsonResponse({
            'result': success_count > 0,
            'msg': f'{success_count}/{len(invoice_ids)} fatura düzenlemeye alındı.',
            'details': results,
        })

    except ValueError as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})
    except Exception as e:
        log.exception(f"esurec_reset_to_draft hatası: {type(e).__name__}")
        return JsonResponse({
            'result': False,
            'error_msg': 'Fatura düzenlemeye alınırken hata oluştu. Tekrar deneyin.',
        })
