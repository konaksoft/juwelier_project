import random
import re
import string
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator as token_generator
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.html import strip_tags
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import Users, OtpCode, ContactConsent
from apps.helpers.AESCipher import AESCipher
from apps.stores.models import Stores
from apps.whatsapp.services import wa_preflight, send_whatsapp_template_guarded

# Loglama için
log = logging.getLogger(__name__)


# --- MERKEZİ E-POSTA SERVİSİ ---

class EmailService:
    """
    Tüm e-posta gönderimlerini yöneten, ayar kontrollü merkezi sınıf.
    """

    @staticmethod
    def _get_store_config(user):
        """Kullanıcının mağaza konfigürasyonunu (varsa) getirir."""
        if not user: return None
        store = getattr(user, 'store', None)
        if store and hasattr(store, 'config'):
            return store.config
        return None

    @staticmethod
    def send(user, subject, template_name, context, config_key=None):
        """
        Genel E-posta Gönderim Fonksiyonu (Thread Safe)

        Args:
            user (Users): Alıcı kullanıcı nesnesi.
            subject (str): E-posta konusu.
            template_name (str): HTML şablon yolu.
            context (dict): Şablon değişkenleri.
            config_key (str, optional): 'notify_email_2fa' gibi mağaza ayarı anahtarı.
                                        Belirtilirse bu ayar False ise gönderim yapılmaz.
        """
        if not user or not user.email:
            log.warning(f"EmailService: Kullanıcı veya e-posta yok. (User ID: {getattr(user, 'id', 'None')})")
            return False

        # --- 1. AYAR KONTROLÜ ---
        if config_key:
            config = EmailService._get_store_config(user)
            if config:
                # Ayar var ama False ise gönderme
                if not getattr(config, config_key, True):
                    log.info(f"EmailService: Gönderim engellendi ({config_key} kapalı). User: {user.email}")
                    return False
            else:
                # Mağaza/Config yoksa varsayılan olarak gönder (veya politikaya göre False yapabilirsiniz)
                pass

                # --- 2. GÖNDERİM (THREAD) ---

        def _send_thread():
            try:
                html_message = render_to_string(template_name, context)
                plain_message = strip_tags(html_message)
                from_email = getattr(settings, "EMAIL_FROM_ADDRESS", None) or settings.DEFAULT_FROM_EMAIL

                send_mail(
                    subject,
                    plain_message,
                    from_email,
                    [user.email],
                    html_message=html_message,
                    fail_silently=False
                )
                log.info(f"EmailService: '{subject}' gönderildi -> {user.email}")
            except Exception as e:
                log.exception(f"EmailService Hatası ({user.email}): {e}")

        # Arka planda gönder (Kullanıcıyı bekletmemek için)
        threading.Thread(target=_send_thread, daemon=True).start()
        return True




