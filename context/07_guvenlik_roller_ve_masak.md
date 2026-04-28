## Şirket-Mağaza-Kullanıcı Hiyerarşisi

- `Company → Stores → Users` — temel multi-tenancy yapısı
- `Company.tax_number`: unique constraint
- `Stores.is_active`: default=False (başlangıçta inaktif)
- `Stores.store_id`: 11 haneli okunabilir unique identifier
- `Stores.masak_public_token`: UUID unique
- `Users.store FK`: NULL → Konasoft personeli

## StoreConfiguration Varsayılanlar

- `enforce_cash_limit=True`
- `enforce_invoice_customer=False`
- `enforce_masak_identity=False`
- `post_save` signal: `create_store_config()` → StoreConfiguration otomatik oluşturulur

## Hybrid-Gate 3 Katmanlı Yetki Sistemi

- Katman 1 (ABC Menü): `RoleDetail` tablosu
- Katman 2 (Ana Sayfa İndeks): `MAIN_PAGE_TO_ABC_MAP` — ana sayfayı ABC koda eşler
- Katman 3 (Fonksiyonel): staff → RoleDetail doğrudan; store → effective permission pool
- `_get_effective_perm_codes()`: EXPIRED/SUSPENDED → boş set; DEMO → SaaSModule(slug='demo-access').permissions

## IDOR Koruması

- `customers/views.py` tüm fonksiyonlar: `store=request.user.store` filtresi zorunlu
- `board_settings_view`: `if not request.user.is_superuser → return 403`
- Cross-tenant erişim: `/masak/print/<id>/` → "Bu müşteri mağazanıza ait değil" + redirect

## Red Team Güvenlik Kuralları

- V2-A: `pavo_local_jewellery_sale` → `select_for_update()` + idempotency check (`paid_total >= grand_total`)
- V4-A: Frontend `safeMul()` helper; `toFixed(4)`; backend `Decimal(str(v))` kullanır
- V1-A: `_LIVE_DATA_CACHE_TTL=4s`, `cache_key='live_data:{store_id}'`
- V1-B: setInterval ID saklanır; `visibilitychange` pause/resume; `beforeunload clearInterval`
- V1-C: `update_products_from_api.delay()` async çağrı

## MASAK Yasal Eşikler

- 30,000 TL nakit — kimlik zorunluluğu
- 36,000 TL fatura/müşteri — kimlik zorunluluğu
- 185,000 TL — kimlik zorunluluğu (üst eşik)

## MASAK QR Sistemi

- `Stores.masak_public_token`: UUID, `editable=False`
- Public form: auth gerektirmez; CSRF korumalı; IP tabanlı throttle (3/60sn)
- Kimlik görselleri: `media/masak/` dizininde saklanır
- JS: max 1600px JPEG 85 boyutuna düşürülür; backend >5MB base64 reddeder

## MASAK Toggle IYS

- `POST /masak/toggle-iys`: `customer_id + approved` payload
- Tenant izolasyonu: `request.user.store` kontrolü
- `masak_declaration` yoksa 400 hata
- 3 kanal (sms/email/call) tek bool ile toplu güncellenir

## GROUP_LABELS / GROUP_ICONS

- Bu sözlükler Switch UI'yı yönetir
- DB'den `Permission.group` asla okunmaz; sözlükler statik

## Django Admin

- Production'da Django Admin YOKTUR (proje kuralı)
