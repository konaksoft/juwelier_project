import logging
import threading
from decimal import Decimal
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.conf import settings

# WhatsApp Servisi
from apps.whatsapp.services import wa_preflight, send_whatsapp_template_guarded

# --- MERKEZİ E-POSTA SERVİSİ ---
from apps.settings.send_mail import EmailService

log = logging.getLogger(__name__)


# --- YARDIMCI FONKSİYONLAR ---

def fmt_tl(x):
    """
    Sayıyı TR/DE/CH ortak para formatına çevirir: 1.234,56
    (Almanya ve Türkiye'de aynı binlik ayraç+ondalık virgül kuralı geçerli.)

    Geriye uyumluluk için isim 'fmt_tl' kaldı; gerçekte para birimi bağımsız
    numerik formatlayıcıdır. Sembol eklemek için fmt_money() kullanın.
    """
    try:
        return f"{Decimal(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "0,00"


def fmt_money(x, symbol=''):
    """
    Sayıyı para formatına çevirir ve istenirse sembol ekler.
    Örn: fmt_money(1234.5, '€') → '1.234,50 €'
         fmt_money(1234.5)       → '1.234,50'
    """
    base = fmt_tl(x)
    if not symbol:
        return base
    return f"{base} {symbol}"


def get_safe_local_now_str(fmt="%d.%m.%Y %H:%M"):
    """
    Güvenli tarih formatlama: Naive veya Aware datetime fark etmeksizin çalışır.
    """
    now = timezone.now()
    if timezone.is_aware(now):
        now = timezone.localtime(now)
    return now.strftime(fmt)


def send_customer_operation_mail(*, customer, store, process_no, items, payments, custody, totals, direction_text,
                                 date_str, subject_suffix="İşlem Özeti", summary=None, detail_url=None):
    """
    Müşteriye işlem bildirim maili gönderir.
    Merkezi EmailService kullanır.
    Ekstra Güvenlik: is_email_verified kontrolü yapılır.
    """
    # 1. Güvenlik Kontrolü: E-posta var mı?
    if not (customer and getattr(customer, "email", None)):
        return

    # 2. Güvenlik Kontrolü: E-posta Onaylı mı?
    if not getattr(customer, "is_email_verified", False):
        log.warning(f"Mail gönderimi engellendi: {customer.email} onaylı değil.")
        return

    subject = f"{subject_suffix} – {process_no}"

    # Context hazırlığı
    ctx = {
        "subject": subject,
        "customer": customer,
        "store": store,
        "process_no": process_no,
        "items": items,
        "payments": payments,
        "custody": custody,
        "totals": totals,
        "summary": summary or {},
        "detail_url": detail_url,
        "message_intro": "Kuyum Plus üzerinde işlem(ler)iniz tamamlandı.",
        "date_str": date_str,
        "payment_text": direction_text
    }

    # --- MERKEZİ E-POSTA SERVİSİ ---
    # EmailService.send zaten kendi içinde Thread açar, render eder ve gönderir.
    # 'notify_email_ops': İşlem özetleri için kullanılan ayar anahtarı.
    EmailService.send(
        user=customer,
        subject=subject,
        template_name="management/mail_templates/customer_operation_mail.html",
        context=ctx,
        config_key='notify_email_ops'
    )


# --- TETİKLEYİCİ FONKSİYON ---

def trigger_transaction_notifications(
        request,
        process_no: str,
        customer,
        items: list,
        payments: dict,
        totals: dict,
        custody: dict = None,
        summary_note: str = None
):
    """
    Hem Perakende hem Hızlı İşlem için ortak bildirim tetikleyicisi.
    Email ve WhatsApp gönderimlerini yapar.
    """

    # 1. Ortak Veri Hazırlığı
    store = request.user.store
    user = request.user
    date_str = get_safe_local_now_str()

    # ─────────────────────────────────────────────────────────────────────
    # FAZ 68: Public Token + Canonical Summary (SSOT)
    #   - Müşteriye giden link KRIPTOGRAFIK İMZALI TOKEN üzerinden public
    #     view'a yönlendirilir (login gerektirmez).
    #   - E-posta özet verileri (toplam tutar, bakiye) doğrudan veritabanından
    #     taze hesaplanan calc_process_summary çıktısından beslenir; caller'ın
    #     totals dict'i fallback amaçlı kalır.
    #   - Lazy import ile sirküler bağımlılık önlenir.
    # ─────────────────────────────────────────────────────────────────────
    from apps.process.views import make_public_process_token, calc_process_summary

    try:
        public_token = make_public_process_token(process_no)
    except Exception:
        log.exception(f"Public token üretilemedi: {process_no}")
        public_token = ""

    try:
        canonical_summary = calc_process_summary(process_no) or {}
    except Exception:
        log.exception(f"calc_process_summary hatası: {process_no}")
        canonical_summary = {}

    try:
        # Müşteriye gönderilen detay linki: TOKEN'lı public URL (login gerektirmez).
        # Token üretilemediyse boş string döner (mailde buton render edilmemeli).
        if public_token:
            detail_url = request.build_absolute_uri(
                reverse("process:public-detail", args=[public_token])
            )
        else:
            detail_url = ""
    except Exception:
        log.exception(f"public-detail URL üretilemedi: {process_no}")
        detail_url = ""

    # Ödeme yönü metni
    direction_text = payments.get('direction_text', '')

    # SSOT — canonical_summary değerleri varsa onları, yoksa caller totals fallback
    _balance_eur_raw = canonical_summary.get('balance_eur_raw', totals.get('balance_eur', 0))
    _total_tl_str = canonical_summary.get(
        'net_total', totals.get('total_sales_eur', totals.get('net_tl_abs', '0,00'))
    )
    _paid_tl_str = canonical_summary.get('paid_total', fmt_tl(payments.get('paid_total_tl', 0)))
    _balance_eur_str = canonical_summary.get(
        'balance_eur', fmt_tl(abs(totals.get('balance_eur', 0) or 0))
    )
    _net_hs_str = canonical_summary.get('net_hs', totals.get('net_hs', '0,000'))
    try:
        _has_debt = bool(_balance_eur_raw is not None and Decimal(str(_balance_eur_raw)) > Decimal('0'))
    except Exception:
        _has_debt = False

    # E-posta Context Verileri
    email_context = {
        "customer": customer,
        "store": store,
        "process_no": process_no,
        "date_str": date_str,
        "items": items,
        "payments": payments,
        "totals": totals,
        "custody": custody,
        "direction_text": direction_text,
        "summary": {
            "total_tl": _total_tl_str,
            "paid_tl": _paid_tl_str,
            "balance_eur": _balance_eur_str,
            "balance_eur_raw": _balance_eur_raw,
            "net_hs": _net_hs_str,
            "payment_text": direction_text,
            "note": summary_note,
            "has_debt": _has_debt,
        },
        "detail_url": detail_url,
        "subject_suffix": "İşlem Özeti"
    }

    # ---------------------------------------------------------
    # 1. E-POSTA GÖNDERİMİ
    # ---------------------------------------------------------
    # Not: transaction.on_commit kullanarak, veritabanı işlemi
    # kesinleştikten sonra mail gönderilmesini sağlıyoruz.
    if customer and getattr(customer, "email", None) and getattr(customer, "is_email_verified", False):
        transaction.on_commit(lambda: send_customer_operation_mail(**email_context))
    else:
        if customer:
            log.info(f"Mail gönderilmedi. Onay durumu: {getattr(customer, 'is_email_verified', False)}")

    # ---------------------------------------------------------
    # 2. WHATSAPP GÖNDERİMİ (Thread)
    # ---------------------------------------------------------
    if customer and getattr(customer, "phone", None) and getattr(customer, "is_phone_verified", False):

        customer_full_name = f"{customer.first_name} {customer.last_name}".strip()
        # SSOT — canonical_summary varsa onu, yoksa caller totals fallback
        net_amount_str = canonical_summary.get('net_total', totals.get('net_tl_abs', '0,00'))
        paid_amount_str = canonical_summary.get(
            'paid_total', fmt_tl(payments.get('paid_total_tl', 0))
        )
        status_text = direction_text if direction_text else 'Tamamlandı'
        # FAZ 68: WhatsApp URL butonu için kriptografik imzalı token
        # (Önceden process_no gönderiliyordu → public-detail view "Geçersiz Bağlantı"
        #  ya da private detay view'a düşüp login zorunluluğu yaratıyordu.)
        wa_button_token = public_token

        def _send_wa_thread(_token=wa_button_token):
            try:
                # WhatsApp gönderiminden önce token üretiminin başarılı olduğunu
                # garanti et — token yoksa müşteriye kırık link gönderme.
                if not _token:
                    log.warning(
                        f"WA gönderimi atlandı ({process_no}): public_token üretilemedi"
                    )
                    return

                # Store config kontrolü veya manuel WA ayarı burada yapılabilir
                # Ancak burada doğrudan WA servisi çağrılıyor.
                can_send, reason, chosen_lang = wa_preflight(store, "islem_ozeti_kp_min_v3", "tr_TR")
                if not can_send:
                    log.warning(f"WA Preflight engeli ({process_no}): {reason}")
                    return

                ok, code = send_whatsapp_template_guarded(
                    store=store,
                    user=user,
                    customer=customer,
                    to=customer.phone,
                    template="islem_ozeti_kp_min_v3",
                    language=chosen_lang,
                    header_params=[process_no],
                    body_params=[
                        customer_full_name,
                        date_str,
                        net_amount_str,
                        paid_amount_str,
                        status_text
                    ],
                    button_params=[_token],
                    validate=False,
                    return_reason=True
                )

                if not ok:
                    log.warning(f"WA Gönderim Başarısız ({process_no}): {code}")

            except Exception as e:
                log.exception(f"WA Thread Hatası {process_no}: {e}")

        # WhatsApp işlemi de transaction commit olduktan sonra başlasın
        transaction.on_commit(lambda: threading.Thread(target=_send_wa_thread, daemon=True).start())

    return True