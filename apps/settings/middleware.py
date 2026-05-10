from django.utils import translation
from django.conf import settings
from django.core.cache import cache


STORE_LANG_CACHE_KEY = "store_lang_{store_id}"
STORE_LANG_CACHE_TTL = 60 * 60  # 1 saat


class StoreLanguageMiddleware:
    """
    Mağaza bazlı dil zorlama middleware'i.

    KONUM (MIDDLEWARE sırası kritik):
        AuthenticationMiddleware    <- request.user BURADA set edilir
        >>> StoreLanguageMiddleware <<<  <- DB'den gelen dil ile override
        MessageMiddleware

    Davranış:
      - Giriş yapmış kullanıcının mağazasına ait StoreConfiguration.language_code'u okur.
      - translation.activate() ile Django'nun çeviri sistemini bu dile ayarlar.
      - Dil cookie'sini yanıta yazar; LocaleMiddleware / tarayıcı cookie'yi gelecek
        isteklerde gönderir → sunucu-taraflı aktivasyonla tutarlı davranış.
      - Anonim kullanıcılar bypass edilir (settings.LANGUAGE_CODE devreye girer).
      - Cache: Redis'te 1 saat tutulur; dil değişince signal cache'i temizler.

    NOT: Bu proje i18n URL pattern kullanmıyor. Bu nedenle URL prefix yönlendirme
    mantığı YOKTUR — sadece translation.activate + cookie yazma yapılır.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_lang = None

        if hasattr(request, 'user') and request.user.is_authenticated:
            request_lang = self._get_store_language(request.user)
            if request_lang:
                translation.activate(request_lang)
                request.LANGUAGE_CODE = request_lang

        response = self.get_response(request)

        # Response aşamasında dili tekrar oku (dil güncelleme isteği sonrası
        # signal cache'i temizlemiş olabilir; yeni dili cookie'ye yaz).
        if hasattr(request, 'user') and request.user.is_authenticated:
            cookie_lang = self._get_store_language(request.user)
            if cookie_lang:
                response['Content-Language'] = cookie_lang
                response.set_cookie(
                    settings.LANGUAGE_COOKIE_NAME,
                    cookie_lang,
                    max_age=settings.LANGUAGE_COOKIE_AGE,
                    path=settings.LANGUAGE_COOKIE_PATH,
                    domain=settings.LANGUAGE_COOKIE_DOMAIN,
                    secure=settings.LANGUAGE_COOKIE_SECURE,
                    httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
                    samesite=settings.LANGUAGE_COOKIE_SAMESITE,
                )

        return response

    def _get_store_language(self, user):
        """
        Kullanıcının mağazasına ait dil kodunu döner.
        Önce Redis cache; miss durumunda DB sorgusu + cache'e yaz.
        """
        store_id = getattr(user, 'store_id', None)
        if not store_id:
            return None

        cache_key = STORE_LANG_CACHE_KEY.format(store_id=store_id)
        lang = cache.get(cache_key)
        if lang is not None:
            return lang

        try:
            from apps.settings.models import StoreConfiguration
            config = StoreConfiguration.objects.only('language_code').get(store_id=store_id)
            lang = config.language_code
        except Exception:
            lang = settings.LANGUAGE_CODE

        cache.set(cache_key, lang, STORE_LANG_CACHE_TTL)
        return lang
