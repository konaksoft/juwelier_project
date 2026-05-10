# ============================================================================
# DOSYA: apps/accounts/store_context.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v1 — FAZ 45.5: Aktif Şube Çözümleyici Helper
#
# AMAÇ:
#   Mevcut tek-şube view kodunda yer alan `request.user.store` desenini
#   kırmadan, çoklu şube erişimine GEÇİŞE HAZIR bir helper sağlar.
#
# DAVRANIŞ (FAZ 45 — DORMANT):
#   - Bu fonksiyon FAZ 45'te HİÇBİR view tarafından çağrılmıyor.
#   - Yalnızca tanımlıdır; FAZ 48 (Konsolide Patron Dashboard) kapsamında
#     view'lar kademeli olarak `request.user.store` → `get_active_store(request)`
#     dönüşümüne tabi tutulacaktır.
#
# DAVRANIŞ (Aktivasyon sonrası):
#   1. Kullanıcının session'ında `active_store_id` varsa:
#      a) Bu store_id için kullanıcının aktif UserStoreAccess kaydı var mı?
#         → Evet ise o şube döndürülür.
#         → Hayır ise (yetki revoke edilmiş) session temizlenir, fallback'e düşer.
#   2. Fallback: Kullanıcının is_primary=True olan UserStoreAccess kaydındaki şube.
#   3. Son fallback: Mevcut Users.store FK'si (geriye uyumluluk).
#
# GÜVENLİK:
#   - Session'daki active_store_id değeri, kullanıcının yetkisi olmayan bir
#     şubeye işaret edemez (her okuma UserStoreAccess.is_active=True kontrolü
#     yapar).
#   - SuperUser veya is_staff override YOKTUR; her kullanıcı yetkisine
#     bakılır. Bu Django Admin yasağıyla uyumludur (CLAUDE.md kuralı).
# ============================================================================

from typing import Optional


SESSION_KEY = 'active_store_id'


def get_active_store(request) -> Optional['stores.Stores']:
    """Kullanıcının o anki aktif şubesini çözer.

    DİKKAT: FAZ 45'te bu fonksiyon HENÜZ ÇAĞRILMAMAKTADIR. Mevcut tüm
    view'lar `request.user.store` üzerinden çalışmaya devam etmektedir.
    Bu helper, FAZ 48 aktivasyonu için hazırlık olarak yazılmıştır.

    Args:
        request: Django HttpRequest. user ve session erişilebilir olmalı.

    Returns:
        Stores instance veya None (kullanıcı anonim ise).

    Çözüm sırası:
        1. session['active_store_id'] varsa + UserStoreAccess yetkisi varsa → o şube
        2. UserStoreAccess(user=user, is_primary=True, is_active=True) → o şube
        3. user.store fallback (mevcut tek-şube davranışı)
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return None

    # 1. Session'daki aktif şube (çoklu şube kullanıcıları için)
    session_store_id = request.session.get(SESSION_KEY) if hasattr(request, 'session') else None
    if session_store_id:
        store = _resolve_session_store(user, session_store_id, request)
        if store is not None:
            return store

    # 2. UserStoreAccess primary kaydı
    primary_store = _resolve_primary_access_store(user)
    if primary_store is not None:
        return primary_store

    # 3. Geriye uyumluluk: mevcut Users.store FK
    return getattr(user, 'store', None)


def _resolve_session_store(user, session_store_id, request):
    """Session'daki store_id geçerliyse Stores instance döndürür.

    Yetki kontrolü: UserStoreAccess(user=user, store_id=session_store_id,
                                      is_active=True) var olmalı.
    Yetki yoksa session temizlenir ve None döner.
    """
    from apps.accounts.models import UserStoreAccess

    access = (
        UserStoreAccess.objects
        .filter(user=user, store_id=session_store_id, is_active=True)
        .select_related('store')
        .first()
    )
    if access is None:
        # Yetki revoke edilmiş veya hiç verilmemiş → session'ı temizle
        if hasattr(request, 'session'):
            request.session.pop(SESSION_KEY, None)
        return None
    return access.store


def _resolve_primary_access_store(user):
    """is_primary=True olan UserStoreAccess kaydındaki şubeyi döner."""
    from apps.accounts.models import UserStoreAccess

    access = (
        UserStoreAccess.objects
        .filter(user=user, is_primary=True, is_active=True)
        .select_related('store')
        .first()
    )
    return access.store if access else None


def set_active_store(request, store) -> bool:
    """Kullanıcının aktif şubesini değiştirir (yetki kontrollü).

    DİKKAT: Bu fonksiyon FAZ 48 aktivasyonunda kullanılacaktır. FAZ 45'te
    yalnızca tanımlıdır; hiçbir view çağırmaz.

    Args:
        request: HttpRequest (session kullanılabilir olmalı).
        store: Stores instance veya store.id (UUID/str).

    Returns:
        True: Başarıyla değiştirildi.
        False: Kullanıcının bu şubeye yetkisi yok.
    """
    from apps.accounts.models import UserStoreAccess

    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return False

    store_id = getattr(store, 'id', None) or store

    has_access = UserStoreAccess.objects.filter(
        user=user, store_id=store_id, is_active=True,
    ).exists()
    if not has_access:
        return False

    request.session[SESSION_KEY] = str(store_id)
    return True


def get_accessible_stores(user):
    """Kullanıcının erişebileceği tüm aktif şubelerin queryset'ini döner.

    UI'daki şube değiştirme dropdown'ı için kullanılır. FAZ 45'te
    çağrılmıyor; FAZ 48 ile aktif olur.
    """
    from apps.accounts.models import UserStoreAccess

    if user is None or not user.is_authenticated:
        from apps.stores.models import Stores
        return Stores.objects.none()

    access_qs = UserStoreAccess.objects.filter(user=user, is_active=True)
    store_ids = access_qs.values_list('store_id', flat=True)

    from apps.stores.models import Stores
    return (
        Stores.objects
        .filter(id__in=list(store_ids), is_active=True, is_deleted=False)
        .order_by('-branch_type', 'title')
    )
