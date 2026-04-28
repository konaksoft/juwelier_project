## LiveBoardSettings Model

- `OneToOne → Stores`
- 3 fiyatlandırma modu: Chamber (active_pricing_chamber FK), Manual (use_manual_has_calculation), API (varsayılan)

## Dernek (Chamber) Modu

- Sadece `config.active_pricing_chamber IS NOT NULL` ise aktif
- Fallback olarak şirket derneklerine dönüş KALDIRILDI (tüm 3 view fonksiyonundan)
- `ChamberProductPrice` tablosundan fiyat okunur

## Has Altin İsimlendirmesi

- DB key: `'Has Altin 24 Ayar'` (Celery bu isimle günceller)
- Display name: `'Has Altin'` (frontend bekler)
- `filter_and_sort()`: `'Has Altin 24 Ayar'` → `'Has Altin'` dönüşümü
- `gold_targets` listesi `'Has Altin 24 Ayar'` kullanır

## is_currency Fiyatlandırma

- `is_currency=True` → `final_buy_tl = buy_price_tl` direkt (has×kur çarpımı YAPILMAZ)
- `CURRENCY_CODE_MAP`: döviz ürünleri için kod haritası
- `SPECIAL_NAME_MAP`: altın/gümüş isimlendirme için
- API modu: `store_product_map` sadece chamber/manual modlarda doldurulur, API modunda değil

## Canlı Veri Cache

- `_LIVE_DATA_CACHE_TTL = 4s`
- `cache_key = 'live_data:{store_id}'`
- setInterval ID saklanır; `visibilitychange` pause/resume; `beforeunload clearInterval`
- `update_products_from_api.delay()` — fast_index_view'da async

## Performans Optimizasyonları

- Fatura DataTables: `length=-1` → server-side cap 500 kayıt
- `start<0` → normalize to 0
- `pavo/views.py`: `select_related('store','customer','supplier','process')` + `prefetch_related('items','items__product')`
- `bulk_scan_for_count`: tek `transaction.atomic` içinde `bulk_create` + `bulk_update`
- Dashboard `_build_multi_material_stock_aggregate()`: ekstra SQL sorgusu SIFIR — mevcut aggregate'e conditional Sum eklenir
- NULL güvenliği: `Coalesce(Sum(...), ZERO)` ile NULL → 0 garantisi

## PDF Cache

- `render_invoice_pdf_task`: Celery'ye taşındı
- Depolama: `MEDIA_ROOT/Invoices/pdf_cache/`
- 3 async endpoint: POST `/invoices/<id>/pdf/async`, GET `/invoices/pdf/status/<task_id>`, GET `/invoices/<id>/pdf/result`

## daily_stock_integrity_check

- StockSnapshot vs StockLedger kümülatif toplamı karşılaştırır
- `is_currency=True` ürünleri HARIÇ tutulur (para birimi ürünlerin stok takibi Payment SSOT'a taşındı)
- Celery görevi; tutarsızlık tespit edilince log/alarm

## Sayım RFID Buffer

- `BUFFER_MAX = 50`, `FLUSH_DELAY_MS = 500ms`
- `scanBuffer[]` biriktirir; `processedBarcodes` Set mükerrer önler
- DocumentFragment ile toplu DOM ekleme (tek reflow)

## FAZ 21 Sonrası UI Hotfix (2026-04-27)

FAZ 21 sonrası 3 zincirleme UI bug'ı tespit edildi ve düzeltildi:

### Bug 1 — "Kurlar yüklenemedi" Uyarısı (Toptan)

**İlk yama (yüzeysel — yetersiz):**

- `wholesale_index.html` `PARITY` async fetch'inde `.catch()` yoktu.
- `loadParity()` retry + `_parityLoaded` flag eklendi. Ama gerçek problemi maskelemiyordu.

**Gerçek kök neden (Hotfix Revizyon 3 — 2026-04-27):**

- Harem Altın API USDTRY için `"buy": null, "sell": 44.72` döndürüyor (asimetrik).
- `tasks.py`'de `if buy_price > 0:` guard'ı `buy_price_tl` assignment'ını atlatıyor
  ama `update_fields` listesinde `"buy_price_tl"` her zaman kalıyordu →
  in-memory 0 değeri save ile DB'ye geri yazılıyor → `USDTRY.buy_price_tl = 0.00` sonsuza dek korunuyor.
- `get_parities()` (wholesale_views.py): `buy = r['buy_price_tl'] or 0` → `PARITY.USD.tl_buy = 0`.
- JS: `rateTL('USD') = 0` → "USD kuru sistemde tanımlı değil" uyarısı.

**Backend Çözüm (B + A katmanlı):**

- **B (`tasks.py`):** `effective_buy = buy_price if buy_price > 0 else sell_price` —
  eksik tarafı diğeriyle doldur. `update_fields` koşullu inşa edilir; assignment yapılmadıysa
  field listeye girmez (in-memory 0 yazma sorunu yok). HS hesabı `effective_*` ile yapılır.
- **A (`get_parities()`):** İkinci savunma hattı — `buy = raw_buy or raw_sell or 0`
  ve `sell = raw_sell or raw_buy or 0`. Mevcut bozuk DB verisi düzelene kadar koruma.
- Mimari notu: Fiziksel FX (Payment SSOT) ile referans kur (dönüşüm modalı) hâlâ aynı
  `buy/sale_price_tl` alanlarını paylaşıyor; spread anlamı kaybolur ama dönüşüm matematiği çalışır.

**Hotfix Revizyon 4 — Decimal Precision Hatası (2026-04-27):**

- **Belirti:** `update_products_from_api` save sırasında ValidationError:
  `'sale_price_tl': ['2 ondalık basamaktan daha fazla olmadığından emin olun.']`
- **Kök neden:** Harem Altın API bazı dövizler için 2'den fazla ondalıklı string döndürüyor
  ("44.7234"). Model alan tanımları:
  - `buy_price_tl / sale_price_tl` → `decimal_places=2`
  - `buy_price_hs / sale_price_hs` → `decimal_places=3`
  - `profit` → `decimal_places=3`
  `Products.save()` her zaman `full_clean()` çağırır → ValidationError fırlar.
- **Çözüm:** `tasks.py`'ye `_quant(value, quant)` helper'ı eklendi (ROUND_HALF_UP ile).
  Tüm `_tl` atamaları `_TL_QUANT = 0.01`, `_hs` atamaları `_HS_QUANT = 0.001`,
  `profit` ataması `_RATE_QUANT = 0.001` ile quantize ediliyor.
  Hem `existing_product` update branch'inde hem `create_kwargs` branch'inde uygulandı.

**Hotfix Revizyon 5 — CurrencyChoices Eksik Seçenekler (2026-04-27):**

- **Belirti:** `update_products_from_api` save sırasında ValidationError:
  `'price_currency': ["'OMR' değeri geçerli bir seçim değil."]`
- **Kök neden:** `Products.price_currency` alanı `CurrencyChoices.choices`'a bağlı, ama
  liste sadece HS/HG/TRY/USD/EUR/CAD/QAR içeriyordu. DB'de OMR/SAR/AED/GBP gibi
  kayıtlar (eski seed veya migration ile) zaten mevcuttu; full_clean() yeni save
  yolunda devreye girince validation patladı.
- **Çözüm:** `CurrencyChoices`'a `CURRENCY_CODE_MAP`'teki 21 dövizin tamamı eklendi
  (GBP, CHF, JPY, SAR, AED, AUD, KWD, OMR, RUB, BGN, NOK, SEK, DKK, CNY, ILS, MAD, JOD).
  Migration GEREKİR — `python manage.py makemigrations products && migrate` (sadece
  choices değişikliği; DB şeması bozulmaz, no-op migration).

### Bug 2 — Toptan Ziynet "Birim Has" 0/Boş

- **Kaynak:** `openProduct()` enforceCustomPrice kontrolü `"0.000"` (string truthy) yüzünden
  hatalı sonuç veriyordu; sert RETURN kullanıcıyı kilitliyordu.
- **Çözüm:** `Number()` normalize + WAC fallback zinciri:
  `custom_*_price_hs → weighted_buy_price_hs → buy/sale_price_hs → product_mileage/1000`.
- **Çözüm:** Sert RETURN kaldırıldı; popup yine açılır, eksik fiyat için toast uyarı.
- Perakende `openPopup` (Revizyon 2) ile birebir simetri sağlandı.

### Bug 3 — Perakende "Alış Has" Gelmiyor

- **Kaynak:** `openPopup()` içindeki `hsPerGram` bloğu sadece `#sale_price_hs`'i fallback
  zinciriyle dolduruyordu; `#buy_price_hs` için simetrik fallback yoktu.
- **Çözüm:** `hsPerGramBuy` değişkeni eklendi. ALIŞ Has için zinciri:
  `buyPriceHs → salePriceHs → product_mileage/1000`.
- Asimetri kaynağı: `tasks.py`'de `if buy_price > 0` guard'ı API null döndürdüğünde
  `buy_price_hs`'i güncellemiyor, ama `sell_price > 0` ise `sale_price_hs` güncelleniyordu.
  UI fallback'i bu veri asimetrisini perdeliyor.
