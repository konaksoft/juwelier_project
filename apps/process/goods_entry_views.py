"""Birleşik Mal Girişi (Goods Entry) Router — UAT BULGU 2.

Tarih: 2026-04-29
Amaç:
    Toptan ve perakende ekranlarında parçalı duran hurda/bilezik/pırlanta
    giriş modallarını TEK bir UX akışı altında toplamak. Frontend tek
    bir endpoint'e POST eder; bu router, `entry_type` ve `channel`
    parametrelerine göre **mevcut handler'lara yönlendirir** — iş
    mantığı duplike edilmez, sadece tek-pencere UX için orkestrasyon
    katmanı eklenir.

Yönlendirme tablosu:
    entry_type=SCRAP    + channel=RETAIL    → add_scrap_to_process
    entry_type=SCRAP    + channel=WHOLESALE → add_scrap_to_wholesale_process
    entry_type=SCRAP    + channel=WHOLESALE_MULTI → add_scrap_multi_to_wholesale_process
    entry_type=BRACELET + channel=RETAIL    → add_bracelet_to_retail_process
    entry_type=BRACELET + channel=WHOLESALE → add_bracelet_to_wholesale_process

Frontend için tek modal:
    templates/management/process/_goods_entry_modal.html
    İçinde tip seçici (dropdown) ve dinamik alan grupları yer alır;
    seçilen tipe göre form bölümleri gösterilir/gizlenir.

Notlar:
  * Pırlanta için ileride buraya `entry_type=DIAMOND` dalı eklenir;
    şu an sistemde kendine ait giriş endpoint'i olmadığı için
    router 400 ile reddeder.
  * Permission ve store kontrolü çağrılan handler'lara devredilir
    (mükerrer doğrulama yapılmaz).
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST


# Dispatch tablosu — mevcut handler'lara işaret eder; lazy import
# ile circular dependency riski sıfırlanır.
def _resolve_handler(entry_type: str, channel: str):
    et = (entry_type or '').strip().upper()
    ch = (channel or '').strip().upper()

    if et == 'SCRAP':
        if ch == 'RETAIL':
            from apps.process.retail_views import add_scrap_to_process
            return add_scrap_to_process
        if ch == 'WHOLESALE':
            from apps.process.wholesale_views import add_scrap_to_wholesale_process
            return add_scrap_to_wholesale_process
        if ch == 'WHOLESALE_MULTI':
            from apps.process.wholesale_views import (
                add_scrap_multi_to_wholesale_process,
            )
            return add_scrap_multi_to_wholesale_process
        return None

    if et == 'BRACELET':
        if ch == 'RETAIL':
            from apps.process.retail_views import add_bracelet_to_retail_process
            return add_bracelet_to_retail_process
        if ch == 'WHOLESALE':
            from apps.process.wholesale_views import (
                add_bracelet_to_wholesale_process,
            )
            return add_bracelet_to_wholesale_process
        return None

    return None


@login_required(login_url='login')
@require_POST
def goods_entry_dispatch(request):
    """Birleşik Mal Girişi router endpoint.

    Beklenen POST alanları:
        entry_type: SCRAP | BRACELET (ileride DIAMOND vb. eklenecek)
        channel:    RETAIL | WHOLESALE | WHOLESALE_MULTI
        ... + ilgili handler'ın beklediği diğer alanlar
            (handler'a aynı request olarak iletilir)

    Dönüş:
        Handler'ın JsonResponse'u olduğu gibi döner.
        Geçersiz entry_type/channel için 400.
    """
    entry_type = request.POST.get('entry_type') or ''
    channel = request.POST.get('channel') or ''

    # Çoklu satır gönderimi JSON body olabilir — handler kendi parse eder.
    # Bu durumda entry_type/channel form-data değil JSON body'de olabilir.
    if not entry_type and request.content_type and 'application/json' in request.content_type:
        try:
            import json
            body = json.loads(request.body or b'{}')
            entry_type = body.get('entry_type') or entry_type
            channel = body.get('channel') or channel
        except (ValueError, TypeError):
            pass

    handler = _resolve_handler(entry_type, channel)
    if handler is None:
        return JsonResponse(
            {
                'result': False,
                'error_msg': (
                    f'Geçersiz mal giriş kombinasyonu: entry_type={entry_type!r}, '
                    f'channel={channel!r}. Desteklenen: SCRAP/BRACELET × '
                    f'RETAIL/WHOLESALE/WHOLESALE_MULTI'
                ),
            },
            status=400,
        )

    # Çağrı: handler kendi auth/permission/store doğrulamasını yapar.
    return handler(request)
