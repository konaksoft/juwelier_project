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
    """Sayıyı TR para formatına çevirir: 1.234,56"""
    try:
        return f"{Decimal(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "0,00"


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

    try:
        # Detay linki oluşturma
        detail_url = request.build_absolute_uri(reverse("process:detail", args=[process_no]))
    except Exception:
        detail_url = ""

    # Ödeme yönü metni
    direction_text = payments.get('direction_text', '')

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
            "total_tl": totals.get('total_sales_tl', '0,00'),
            "paid_tl": fmt_tl(payments.get('paid_total_tl', 0)),
            "balance_tl": fmt_tl(totals.get('balance_tl', 0)),
            "balance_tl_raw": totals.get('balance_tl', 0),
            "net_hs": totals.get('net_hs', '0,000'),
            "payment_text": direction_text,
            "note": summary_note,
            "has_debt": False
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
        net_amount_str = totals.get('net_tl_abs', '0,00')
        paid_amount_str = fmt_tl(payments.get('paid_total_tl', 0))
        status_text = direction_text if direction_text else 'Tamamlandı'

        def _send_wa_thread():
            try:
                # Store config kontrolü veya manuel WA ayarı burada yapılabilir
                # Ancak burada doğrudan WA servisi çağrılıyor.
                can_send, reason, chosen_lang = wa_preflight(store, "islem_ozeti_kp_min_v2", "tr_TR")
                if not can_send:
                    log.warning(f"WA Preflight engeli ({process_no}): {reason}")
                    return

                ok, code = send_whatsapp_template_guarded(
                    store=store,
                    user=user,
                    customer=customer,
                    to=customer.phone,
                    template="islem_ozeti_kp_min_v2",
                    language=chosen_lang,
                    header_params=[process_no],
                    body_params=[
                        customer_full_name,
                        date_str,
                        net_amount_str,
                        paid_amount_str,
                        status_text
                    ],
                    button_params=[process_no],
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