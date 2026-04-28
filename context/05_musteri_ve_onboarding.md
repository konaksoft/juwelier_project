## TCKN/VKN Doğrulama

- `validate_tckn(value)`: resmi 11 haneli matematiksel algoritma (10. basamak kontrolü + 11. basamak kontrolü)
- `validate_vkn(value)`: 10 haneli format kontrolü
- `validate_identification_number(value)`: dispatcher (11 hane→TCKN, 10 hane→VKN, diğer→reddet)
- Konum: `apps/customers/validators.py`

## StoreConfiguration Müşteri Alanları

- `enforce_customer_always`: varsayılan `False` — True ise müşterisiz işlem her durumda bloke
- `require_customer_phone`: varsayılan `True`
- `require_customer_tckn`: varsayılan `False`
- "İkisi birden kapanamaz" kuralı: `require_customer_phone` VE `require_customer_tckn` ikisi birden False olamaz
- Backend: her ikisi False ise `require_customer_phone=True` zorla set edilir
- Migration: `apps/settings/migrations/0010_storeconfiguration_enforce_customer_always_and_more.py`

## _check_legal_limits() Kural 0

- `enforce_customer_always=True` + `customer=None` → tutardan bağımsız olarak reddet

## MASAK Eşikleri

- 30,000 TL nakit — kimlik zorunluluğu
- 36,000 TL fatura/müşteri — kimlik zorunluluğu
- 185,000 TL — kimlik zorunluluğu (üst eşik)
- `_normalize_net_total_to_tl()`: FX fiyatlı ürün toplamlarını MASAK kontrolü için TL'ye çevirir
- `_check_legal_limits_retail()` ve `_check_legal_limits()`: `max(orig, tl_total)` ile güncellendi

## MASAK QR

- `Stores.masak_public_token`: UUID, `editable=False`
- `CustomerMasakDeclaration` model
- Public form: auth gerektirmez; CSRF koruması var; IP tabanlı throttle (3 istek / 60 saniye)
- Kimlik görselleri: `media/masak/` dizininde saklanır
- JS: tarayıcı tarafında max 1600px JPEG 85 boyutuna düşürülür; backend >5MB base64 reddeder

## CustomerMasakDeclaration Model

- `customer_type`: BIREYSEL / KURUMSAL (varsayılan BIREYSEL)
- Bireysel alanlar: `first_name`, `last_name`, `identity_number`, `nationality`, `document_type`, `document_number`, `birth_place`, `birth_date`, `address`, `email`, `phone`, `occupation`, `mother_name`, `father_name`, `id_front_image`, `id_back_image`
- Kurumsal alanlar: `company_title`, `tax_office`, `tax_number`, `mersis_number`, `trade_registry_number`, `activity_field`, `company_address`
- Yetkili temsilci: `rep_first_name`, `rep_last_name`, `rep_identity_number`, `rep_title`
- Gerçek faydalanıcı: `beneficial_owner_name`, `beneficial_owner_identity`, `beneficial_owner_share`
- `consent_iys_sms`, `consent_iys_email`, `consent_iys_call` — IYS kanal bazlı ayrı tutulur
- `submitted_at`, `updated_at`, `ip_address`, `user_agent`
- `display_name` property: kurumsal→company_title, bireysel→first_name+last_name
- `display_identity` property: kurumsal→tax_number, bireysel→identity_number

## MASAK Form Kuralları

- `CustomerMasakPublicForm.clean()`: customer_type'a göre dallanır
- Bireysel zorunlu: `first_name`, `last_name`, `identity_number`, `birth_place`, `birth_date`, `address`, `phone`
- Kurumsal zorunlu: `company_title`, `tax_office`, `tax_number`, `company_address`, `phone`, `rep_first_name`, `rep_last_name`, `rep_identity_number`, `rep_title`
- Her iki tipte zorunlu: `consent_kvkk`, `consent_acik_riza`
- TCKN checksum: bireysel identity_number + kurumsal rep_identity_number için

## MASAK Kimlik Görselleri

- Kimlik fotoğrafları SADECE bireysel tipte kaydedilir; kurumsal tipte boş bırakılır
- `masak_toggle_iys_consent` endpoint: `POST /masak/toggle-iys`; 3 kanalı (sms/email/call) tek bool ile toplu günceller
- `masak_declaration` yoksa IYS toggle display:none; backend 400 döner

## MASAK Public Form

- Base.html extend etmez — standalone Bootstrap 5 CDN
- Standalone sayfa olduğundan Bootstrap 5 JS CDN manuel eklendi
- `masak` uygulaması `EXCLUDED_APPS`'te — yeni view'lara rol eklemek için EXCLUDED_APPS'ten çıkarılmalı
- `Customers.store` M2M → `customer.store.add(store)` kullanılır (`customer.store_id = ...` değil)
- Print URL: `/masak/print/<uuid>/?auto=1` → sayfa yüklenince `window.print()`
- Kurumsal kayıtta `first_name` = şirket ünvanı, `last_name` boş (Customers modelini kırmamak için)

## Onboarding Mimarisi

- `Stores.status`: DEMO, PENDING_PAYMENT, ACTIVE, EXPIRED, SUSPENDED
- `Stores.demo_expires_at`, `Stores.demo_converted_at`
- `Stores.onboarding_source`: corporate / fast_track / manual
- `Packages.is_demo`: BooleanField

## Demo Mağaza Akışı

- `create_demo_store()`: 8 adımlı akış; `transaction.atomic()`
- Company phone dedup kontrolü
- Shadow Lead (status='won', channel='fast_track') + shadow Proposal (title='[DEMO] ...', status='accepted') oluşturulur
- `DEMO_MAX_USERS = 3` (is_staff için bypass)
- DEMO SaaSModule: `slug='demo-access'`, `is_core=False` (ZORUNLU False)
- DEMO Package: `code='demo'`, `is_demo=True`, PackageModules BAĞLANMAZ

## Demo → Aktif Dönüşüm

- `convert_demo_to_active()`: `slug='demo-access'` StoreModule kaldırılır; gerçek paket atanır; `status='ACTIVE'`
- EXPIRED mağaza → yeniden aktifleştirme (yeni mağaza oluşturulmaz, eski veri korunur)

## Demo Süre Bitimi

- `expire_demo_stores()`: cron servisi; `status='DEMO'` + `demo_expires_at < now()` → EXPIRED + `is_active=False`
- `_get_effective_perm_codes()`: EXPIRED/SUSPENDED → boş set; DEMO → SaaSModule(slug='demo-access').permissions
- Management command: `expire_demos` (`--dry-run`, `--verbose`); cron: `0 3 * * *`

## Mağaza Sinyaller

- `post_save` on Stores: `create_default_bank_accounts` → 3 varsayılan BankAccount + POSCommissionRate oluşturur
- `post_save` on Stores: `create_store_config()` → otomatik StoreConfiguration oluşturur
- `_add_protected_products_to_inventory()`: `Products.is_protected=True` → `StockSnapshot.bulk_create()`

## Seed Komutu

- `seed_demo_assets`: idempotent `get_or_create` ile SaaSModule + Package oluşturur
