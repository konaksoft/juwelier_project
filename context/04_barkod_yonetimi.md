## ProductCategory Model

- `store + barcode_prefix`: `unique_together` constraint

## Barkod Üretme Algoritması

- `generate_barcode()`: Gap-filling (boşluk doldurma) algoritması
- Format: `PREFIX-NNNNN` (prefix + 5 haneli numara)
- Tüm mevcut barkodlar `set()` içine alınır → O(1) lookup
- 1'den başlayarak ilk kullanılmayan numara bulunur
- Silinmiş barkodlar: `_del_` + timestamp eklenerek serbest bırakılır

## Soft Delete

- Ürün silindiğinde barkod alanı `barcode_prefix + '_del_' + timestamp` formatına dönüştürülür
- Bu sayede barkod numarası yeni ürünler için serbest kalır

## BarcodeTemplate Model

- `template_name`, `jewelry_type`, `gold_rate`
- `product_mileage`, `labor_mileage`, `piece_labor`
- `supplier`
- `profit` — migration gerektirir

## Etiket Hesaplama Formülü

- `totalHas = ((product_mileage + labor_mileage) / 1000) * gram + piece_labor`

## Layout Modları

- `STANDARD`: Standart tek bölüm
- `REFLECTED`: Kelebek 4-bölüm
- `SIDE_BY_SIDE`: Yan yana

## Etiket Alt Bilgi Ayarı

- `label_bottom_left_type`: `MILYEM` veya `MALIYET`

## StoreLabelSettings

- Config JSON: `{**defaults, **saved}` ile birleştirilir (kayıtlı değerler varsayılanları override eder)

## Frontend Kuralları

- Bootstrap focus trap düzeltmesi: `data-bs-focus="false"` ve `aria-hidden="true"`; `$(document).off('focusin.bs.modal')`
- `resetFormForContinuousEntry()`: modal açık kalır, arka planda DataTable yenilenir, aktif template yeniden uygulanır

## Etiket Ayarları Kategori Kartları (UI State) — 2026-04-27

**Dosya:** `templates/management/stores/store_detail.html`

Sayfa: Mağaza Detay → "Etiket Tasarımı & Boyutlandırma" kartı → "Ürün Kategorisi" satırı (Altın / Pırlanta / Saat).

**Çözülen UI Bug (BÖLÜM 4):**
1. **(E-01) Görsel state senkronizasyonu:** Kategori kartına tıklandığında `active` CSS class artık tüm sibling kartlardan kaldırılıp seçilen karta atanıyor. Önceden Altın'da hardcoded kalıyor, Pırlanta/Saat tıklansa bile yeşil seçim halkası geçmiyordu.
2. **(E-03) URL state persistence:** Kategori değişince URL `?label_category=GOLD/DIAMOND/WATCH` olarak güncelleniyor (`history.replaceState` — geri tuşunu kirletmiyor). Sayfa yenilense de aynı kategori açılıyor. Hydration kodu `_hydrateLabelCategoryFromUrl()` IIFE içinde.
3. **(E-04) Toast bildirim:** SweetAlert2 toast (`position: 'top-end'`, 1.8s) kategori değişimini doğruluyor. İlk URL hydration'da `_suppressToast = true` ile susturuluyor.

**Kapsam Garantisi:**
- Backend `update_label_settings` view'ında HİÇBİR DEĞİŞİKLİK YOK — yalnızca template/JS değişti.
- Altın etiket tasarımı kayıt mantığı (DESIGN_FIELD_MAP['GOLD']) ve barkod üretim algoritması etkilenmedi.
- Hardcoded `active` CSS class'ı Altın label'ında korundu (URL parametresi yokken default GOLD ile uyumlu).

## Pırlanta Ekleme Formu — Taş Tablosu UX (BÖLÜM 1-2) — 2026-04-27

**Dosya:** `templates/management/gold_purchases/index.html`

Sayfa: Ürün Ekle modal → "Pırlanta" tab → "Pırlanta Taşları" tablosu.

**Uygulanan İyileştirmeler:**

1. **(P-03) Vitrin / Gelişmiş Sütun Ayrımı:** Tablo sütunları iki kategoriye ayrıldı.
   - **Vitrin (her zaman görünür):** Rol, Sıra, Karat, Renk, Berraklık.
   - **Gelişmiş (`.col-advanced` class):** Şekil, Kesim, Sertifika Lab, Sertifika No.
   - Tablo `<table class="stones-table hide-advanced">` ile yüklenir; CSS `.hide-advanced .col-advanced { display: none }` ile gizlenir.
   - Tablo başlığında "Gelişmiş sütunlar" toggle checkbox'ı bulunur.
   - Tercih `localStorage['kuyumPlus_diaStonesShowAdvanced']` ('0' veya '1') olarak persist edilir.

2. **(P-06) Auto-focus & Enter Navigation:**
   - `addStoneRow()` her çağrıda yeni satırın Karat input'una odaklanır (`focusLastStoneCarat()`).
   - `stone_carat[]` input'unda **Enter** tuşu:
     - Eğer son satır ise → yeni taş eklenir (`addStoneRow()` çağrılır).
     - Değilse → bir sonraki satırın Karat alanına atlanır.
   - `e.preventDefault()` ile form submit engellenir.

**Kapsam Garantisi:**
- Form input name'leri (`stone_role[]`, `stone_position[]`, `stone_carat[]`, `stone_shape[]`, `stone_color[]`, `stone_clarity[]`, `stone_cut[]`, `stone_cert_lab[]`, `stone_cert_no[]`) **DEĞİŞMEDİ**. Backend `multi_material_add` endpoint'i ve `DiamondStone` model alanları aynı.
- Gizli sütunlardaki input'lar form submit'e dahil olmaya devam eder (CSS display:none submit'i etkilemez).
- Altın ve Saat tab'larına HİÇBİR DOKUNUŞ YOK; tüm değişiklikler `#tab-pane-diamond` ve onun JS handler'ları içinde.
- Mevcut "şablon kaydet" akışı (`DIAMOND_TEMPLATE_FIELDS`) etkilenmedi.

**Atlanan Görevler (Önerildi ama uygulanmadı):**
- **P-01 (3 Bölge Modeli):** Form zaten bu yapıdaydı (Kimlik / Montür+Fiyat / Taşlar bölümleri net ayrılmış).
- **P-02 (Resim sağ kolona):** Üretim kullanıcısının muscle-memory'si sol-resim layout'u; geri adım sayılırdı.
- **P-04 (Accordion + tabindex):** Stones table toggle çözümü zaten "advanced" alanları akıştan çıkarıyor; Accordion overengineering olurdu.

## DiamondDetail.supplier_ref + Gelişmiş Bilgiler Accordion (BÖLÜM 1 / P-07) — 2026-04-27

**Dosyalar:**
- `apps/products/models.py` — `DiamondDetail.supplier_ref` alanı eklendi.
- `apps/gold_purchases/views.py` — `multi_material_add` POST handler'ı `diamond_supplier_ref` okur.
- `templates/management/gold_purchases/index.html` — "Gelişmiş Bilgiler" accordion'u + rozet JS.

**Model Alanı:**
```
supplier_ref = CharField(max_length=64, null=True, blank=True)
```
- **Amaç:** R2 etiketinde görülen `RAI5.61-27` formatlı üretici/tedarikçi referans kodu.
- **OPSİYONEL** — boş bırakılabilir; barkod üretimini engellemez.
- Form input name: `diamond_supplier_ref` → backend `request.POST.get('diamond_supplier_ref')`.

**Frontend Accordion (Bootstrap 5 collapse):**
- Form içinde Section 3 (Satış Fiyatı) ile Section 4 (Taşlar) arasına yerleştirildi.
- Default kapalı, `tabindex="-1"` ile vitrin Tab zincirinden çıkarıldı.
- `[data-advanced-field="1"]` attribute'u — JS rozet sayacının izlediği bayrak.
- `#diamondAdvancedFilledBadge` rozet — dolu alan sayısını gösterir; 0 ise gizli.
- `diamondForm reset` event'inde rozet de sıfırlanır.

**MIGRATION GEREKLİ:**
Yeni `supplier_ref` alanı eklendiği için kullanıcı şu komutları çalıştırmalı:
```
python manage.py makemigrations products
python manage.py migrate
```

## P-08 — Pırlanta Submit Validation Zinciri (2026-04-27)

**Dosya:** `templates/management/gold_purchases/index.html` → `addDiamondForm` submit handler.

**Validation Sırası (lock SET edilmeden ÖNCE):**
1. **Takı Tipi seçili mi?** Boşsa hard block — barkod prefix'i bu alandan üretilir. `notifyError` + focus.
2. **En az 1 dolu karatlı taş var mı?** (Mevcut kontrol — korundu.)
3. **Satış Fiyatı > 0 mı?** SOFT WARN — `confirm()` dialog ile kullanıcıya seçenek. "Hayır" derse focus + cancel; "Evet" derse devam (fiyatsız etiket OK).

**Korunan Akış:**
- Validate-first, lock-later pattern korundu (releaseLock erken return'lerde çağrılıyor).
- Lock SET'i hâlâ tüm validation'lar geçtikten sonra yapılıyor.
- Mevcut "stoneCarats > 0" kontrolü orijinal sırasında kaldı.

## Pırlanta Form Pusula-Eşleştirme Refactor (R-01..R-12) — 2026-04-28

**Dosyalar:**
- `apps/products/models.py` — `DiamondStone.stone_type` + `color_grade` genişletildi.
- `apps/gold_purchases/views.py` — `_parse_diamond_stones_payload`, `multi_material_add` profit + PIR fallback.
- `templates/management/gold_purchases/index.html` — Pırlanta tab'ı ciddi sadeleştirildi.

**Amaç:** Pusula yazılımının pragmatik form akışına eşdeğer, minimum giriş + maksimum otomasyon.

### Backend Değişiklikleri (FAZ A)

**R-06 — DiamondStone.stone_type alanı:**
```python
class StoneType(models.TextChoices):
    DIAMOND, EMERALD, RUBY, SAPPHIRE, PEARL, ALEXANDRITE, OTHER

stone_type = CharField(max_length=16, choices=StoneType.choices, default='DIAMOND')
```
- Default `DIAMOND` — mevcut kayıtlar etkilenmez (geri uyumlu).
- `color_grade` artık `max_length=24` ve `choices` parametresi yok; renkli taşlar için serbest metin destekler ("Yeşil", "Kırmızı"). Pırlanta için yine GIA D-N skalası tercih edilir (form-level enforcement).

**R-12 — `_parse_diamond_stones_payload` güncellemesi:**
- Yeni `stone_type[]` payload alanı okunur; geçersizse `DIAMOND` fallback.
- Rol auto-derive: kullanıcı `stone_role[]` payload'ı göndermezse veya boşsa, `stone_type==DIAMOND && accepted_idx==0 → CENTER`, diğer her durum `SIDE`.
- `position` artık `accepted_idx` üzerinden hesaplanır (0 karatlı atlanan satırlar boşluk bırakmaz).
- Renkli taşlarda `color_grade` 24 char'a kırpılarak saklanır.

**R-05 — Products.profit yazımı:**
- `multi_material_add`: `record.profit = parse_decimal_locale(POST.get('profit'), default='0.000')`
- Form'dan gelen Kâr % değeri Products tablosuna persist edilir (rapor + DataTable kolonu için).

**R-11 — PIR fallback:**
- `multi_material_add` barkod üretiminde: `cat_prefix is None and mat_type == DIAMOND` → `cat_prefix = 'PIR'`.
- Kullanıcı Takı Tipi boş bıraksa bile barkod `PIR-XXXXX` formatıyla üretilir.

### Frontend Değişiklikleri (FAZ B + C)

**R-01/02/03 — Vitrin sadeleştirme:**
- "Ürün Adı", "Adet (Stok)", "Montür Metali" alanları **Gelişmiş Bilgiler accordion**'una taşındı (`data-advanced-field="1"` ile rozet sayacına dahil).
- Backend defaults: `name = jewelry_type or 'Pırlanta'`, `stock_pieces = 1`, `mount_metal = GOLD_YELLOW`.
- Üst layout artık: Tedarikçi + Takı Tipi (üstte 2 sütun); Montür: Ayar + Gram (vitrinde 2 sütun).

**R-04 — Kâr % + FX auto-calc:**
- Section 3 (Satış Fiyatı): `Alış TL` + `Kâr %` + `Para Birimi` + `Satış (Döviz)` 4 sütun layout.
- Hidden input `name="sale_price_tl"` JS tarafından doldurulur.
- IIFE `_initDiamondProfitAutoCalc`:
  - Formül: `sale_price_tl = buy_price_tl × (1 + profit/100)`; `sale_price = sale_price_tl / fx_rate`.
  - FX kurları `/products/get-fx-rates?currencies=USD,EUR,GBP,TRY` endpoint'inden lazy-fetch + cache.
  - TRY için rate sabit 1.0; missing currency → indicator "kuru bulunamadı".
  - Kullanıcı `Satış (Döviz)` alanına manuel yazınca `_saleManual = true` flag set edilir → auto-calc susturulur. Kâr veya Alış TL değişimi flag'i temizler.
  - `dia_fx_indicator` küçük yazı: aktif kur veya manuel mod uyarısı gösterir.

**R-10 — PIR default seçimi:**
- IIFE `_initDiamondJewelryTypeDefault`: sayfa açılışında `dia_jewelry_type` boşsa, option text'inde `(PIR)` geçen ilk kategoriyi seçer + change event tetikler.
- Hiç PIR'lı kategori yoksa boş kalır → backend R-11 fallback devreye girer.

**R-07 — "Tür" sütunu (Rol → Tür):**
- Yeni vitrin sütunu `<th>Tür</th>` eklendi; `name="stone_type[]"`.
- Choices: Pırlanta / Zümrüt / Yakut / Safir / İnci / İskenderit / Diğer.
- `buildStoneRow(position, role, stoneType)` imzası genişletildi; default `DIAMOND`.

**R-08 — Eski Rol + Sıra → Gelişmiş:**
- `<th>Rol</th>` ve `<th>Sıra</th>` artık `col-advanced` class'lı — toggle ile görünür.
- Backend Rol payload'ı opsiyonel: gönderilmezse otomatik türetilir (R-12).

**R-09 — Renk swap (Tür ≠ Pırlanta):**
- `_swapStoneColorCell(row, newType)` fonksiyonu Renk hücresini dinamik değiştirir:
  - DIAMOND → `<select name="stone_color[]">` (D-N skalası).
  - Diğer türler → `<input type="text" name="stone_color[]" maxlength="24">`.
- Önceki değer transfer kuralları:
  - GIA → GIA: D-N regex eşleşirse korunur.
  - Free text → Free text: D-N olmayan değerler korunur.
  - GIA ↔ Free text: değer atılır (cross-paradigm transfer yok).
- STONES_TBODY üzerinde event delegation: `change` event'inde `.stone-type-select` yakalanır.

**R-11 frontend tarafı — Submit validation soft warn:**
- P-08 (1/3) hard block kaldırıldı; `confirm()` ile soft warn:
  - "Takı Tipi seçilmedi. Barkod 'PIR-XXXXX' formatıyla üretilecek. Devam edilsin mi?"
- "Hayır" → focus + cancel; "Evet" → backend PIR fallback ile devam.

### MIGRATION GEREKLİ

Bu refactor 2 yeni alan / değişiklik gerektirir:
1. `DiamondStone.stone_type` (yeni)
2. `DiamondStone.color_grade` (max_length 2 → 24, choices kaldırıldı)
3. `DiamondDetail.supplier_ref` (önceki iterasyondan beri pending)

Kullanıcı şu komutları çalıştırmalı:
```
python manage.py makemigrations products
python manage.py migrate
```

### Kapsam Garantisi

- **Altın tab'ına HİÇ DOKUNULMADI.** `addGoldForm` ve onun JS handler'ları korundu.
- Saat tab'ı etkilenmedi.
- Pırlanta input name'leri DEĞİŞMEDİ: `stone_role[]`, `stone_position[]`, `stone_carat[]`, `stone_shape[]`, `stone_color[]`, `stone_clarity[]`, `stone_cut[]`, `stone_cert_lab[]`, `stone_cert_no[]`. Yalnızca **yeni** `stone_type[]` eklendi.
- `multi_material_add` endpoint sözleşmesi geriye uyumlu: eski form (yeni alanlar olmadan) hâlâ çalışır.
- Form reset preserve listesi (`keepSupplier/Jewelry/Currency/MountKar/MountMet`) korundu — Mount Metal artık accordion'da olsa da reset davranışı aynı.

---

## FAZ DIA-DT (2026-04-28) — Pırlanta Etiket + DataTable Düzeltmeleri

R-01..R-12 sonrası kullanıcı testinde iki regression tespit edildi:
1. Pırlanta ürünleri DataTable'da GOLD kolonlarıyla (Gram=0, Maliyet=0, Satış=0) görünüyordu.
2. Pırlanta etiketinde sertifika "NONE", fiyat "0.00 S", 4C alanları boş çıkıyordu.

### A. Etiket Çıktısı — DIAMOND Fallback Zinciri

**A.1 — Helper:** `apps/gold_purchases/views.py` içine `_resolve_diamond_label_data(p)` eklendi (`get_print_data`'dan önce). Bu helper hem `get_print_data` hem `print_barcode_normal` tarafından kullanılır → DRY.

**Fiyat fallback zinciri:**
- 1. `DiamondDetail.sale_price` + `sale_currency` → varsa öncelikli (asıl döviz fiyatı).
- 2. `Products.sale_price_tl` → fallback (manuel TL girişi varsa).
- 3. Hiçbiri yoksa `'-'`.

**Currency suffix:** `{TRY:'₺', USD:'$', EUR:'€', GBP:'£'}` — eski "0.00 S" placeholder'ı kayboldu.

**A.2 — 4C auto-derive:** `DiamondDetail` özet alanları (`color_grade/clarity_grade/cut_grade/carat_weight`) boşsa **ilk DiamondStone**'dan okunur. `total_carat` boşsa `sum(stones.carat_weight)` hesaplanır.

**A.3 — `NONE` filtresi:** `cert_lab.upper() == 'NONE'` ise boş gösterilir. (Eski `update_or_create` defaultları "NONE" stringi yazıyordu.)

**A.4 — N+1 önleme:** `get_print_data` ve `print_barcode_normal` queryset'lerine `prefetch_related('product__diamond_detail__stones')` eklendi.

### B. DataTable — Material-Aware Kolonlar

**B.1 — Backend (`apps/gold_purchases/views.py::get_all`):**
- `qs.values(...)` listesine eklenen DIAMOND alanları:
  - `product__material_type`, `product__sale_price_tl`
  - `product__diamond_detail__sale_price`, `__sale_currency`
  - `product__diamond_detail__carat_weight`, `__color_grade`, `__clarity_grade`, `__cut_grade`
  - `product__diamond_detail__certificate_lab`, `__certificate_no`
- Annotation: `diamond_stones_total_carat=Sum(...)`, `diamond_stones_count=Count(...)` — özet alan boşsa fallback için.

**B.2 — Frontend (`templates/management/gold_purchases/index.html`):**

`<th>` etiketlerine `data-col-key` attribute eklendi: `jewelry_type/gram/buy_price/sale_price/profit`.

**Material-aware render** (kolon başına `r.product__material_type` kontrolü):
| Kolon | GOLD | DIAMOND |
|---|---|---|
| Gram | `gram.toFixed(3)` | `carat_weight ‖ stones_sum + ' ct'` |
| Maliyet | `buy_price_hs.toFixed(3)` | `color / clarity` özet |
| Satış | `sale_price_hs.toFixed(3)` | `dd.sale_price + currency_symbol` (TL fallback) |
| Kar | sayı | `'%' + profit.toFixed(2)` |

**Header swap:** `window._kpSwapDtHeaders(mat)` — Pırlanta tab'ında başlıklar `Toplam Karat / Renk-Berraklık / Satış (Döviz) / Kar (%)` olur. `materialFilterTabs` click handler'ında `_kpSwapDtHeaders(currentMaterialFilter)` çağrılır.

### Kapsam Garantisi

- GOLD render path'i `r.product__material_type !== 'DIAMOND'` durumunda **birebir korundu** (regression yok).
- WATCH için bağımsız header set'i (gram/maliyet/satış/kar kelimeleri sadeleştirildi); render path'i GOLD ile paylaşılır.
- Backend `values()`'a yalnızca alan eklendi; var olan alanlar dokunulmadı. Migration gerekmedi (sadece okuma).
- Etiket helper'ı `getattr` defansif okumalarla sarıldı: `DiamondDetail` veya `stones` yoksa exception fırlatmaz, boş string döner.
- ZPL şablonu (`_render_zpl_label`) değişmedi; helper sadece `text_field_list` payload'ını besledi.

---

## FAZ DIA-LBL (2026-04-28) — Pırlanta Etiket: Montür Alanları + Para Birimi Toggle

DIA-DT sonrası kullanıcı geri bildirimi:
1. Pırlanta etiketinde montürün **altın ayarı** (örn. "18K") ve **gramı** (örn. "4,10 gr") yoktu — bazı satışlarda bu bilgi etikette istenir (sigorta/değerleme).
2. Fiyat satırında para birimi simgesi (`₺/$/€/£`) **bazı müşteriler için zorunlu, bazıları için gereksiz** — toggle gerekiyordu.
3. Bu seçenekler **yalnızca Pırlanta için** geçerli olmalı; GOLD etiketi tamamen aynı kalmalı.

### A. Yeni Alanlar — `mount_karat` + `mount_gram`

**Veri kaynağı:** `DiamondDetail.mount_karat` (CharField, choices: K8/K14/K18/K22/K24/NONE) + `DiamondDetail.mount_gram` (Decimal 8.3). Her ikisi de zaten formda alınıyor (R-01..R-12 fazından).

**Default config:** [`apps/settings/models.py`](apps/settings/models.py) içinde `default_diamond_small_config()` ve `default_diamond_large_config()` fonksiyonlarına 2 yeni key eklendi:
```python
"mount_karat":  {"x": 320, "y": 200, "font": 13/15, "visible": False, "label": "Montür Ayarı"},
"mount_gram":   {"x": 380, "y": 200, "font": 13/15, "visible": False, "label": "Montür Gramı"},
```
**Default `visible=False`** → mevcut müşterilerin etiket düzeni sürpriz alan eklenmesinden korunur.

**Helper:** `_resolve_diamond_label_data` çıktısına eklenen alanlar:
- `mount_karat`: `dd.mount_karat` (NONE filtrelenir → boş string).
- `mount_gram_str`: `dd.mount_gram > 0` ise `"4,10 gr"` formatı, değilse boş.

**Render:**
- ZPL `get_print_data` DIAMOND branch'ine `('mount_karat', ...)` ve `('mount_gram', ...)` tuple'ları eklendi.
- HTML preview `print_barcode_normal` product dict'ine `'mount_karat'` ve `'mount_gram'` keys'leri eklendi.
- Template [`print-barcode.html`](templates/management/gold_purchases/print-barcode.html) DIAMOND branch'inde 2 yeni `<div>` block (cfg+product guard'lı): `cfg.mount_karat.visible AND product.mount_karat`.

### B. Para Birimi Toggle — `price.show_currency`

**Default config:** DIAMOND price field'ına `"show_currency": True` eklendi. (GOLD/WATCH price field'larına dokunulmadı — onlar kendi `' S'` Satış suffix mantığını kullanmaya devam ediyor.)

**UI:** [`store_detail.html`](templates/management/stores/store_detail.html) küçük + büyük etiket tablolarının üstüne `.diamond-only-option` class'lı bir alert + form-switch eklendi: "Fiyat satırında para birimi simgesini göster". Sadece DIAMOND aktifken görünür (`_syncDiamondOnlyOptions(material, pair)` fonksiyonu).
- Material değişiminde checkbox config.price.show_currency'den okunur (eski kayıtlar için default `true`).
- Save handler'da `vals.show_currency` price row'una eklenir (yalnız DIAMOND ise).
- Reset handler'da `true`'ya çekilir.
- Toggle change → ilgili boyutun preview'ı yenilenir; preview'da DIAMOND price sample suffix'i regex ile strip edilir (`/\s*[^\d,.]+$/`).

**Render path'leri:**
- ZPL: `cfg.price.show_currency` False ise `dl['sale_price_str']` (suffix yok), True ise `dl['price_with_ccy']` (suffix var).
- HTML preview: `print_barcode_normal` aynı mantığı uygular; template'deki div sadece `{{ product.product__sale_price_hs }}` basar (literal `' S'` kaldırılmıştı DIA-DT'de).

### C. Kapsam Garantisi

- **GOLD etiketi sıfır regression:** GOLD config'lerine yeni key eklenmedi; render path'inde GOLD branch DIAMOND'dan tamamen ayrı.
- **WATCH etiketi sıfır regression:** WATCH config'lerine yeni key eklenmedi.
- **Mevcut DIAMOND DB kayıtları:** `_merge(default, saved)` örüntüsü sayesinde yeni key'ler default'tan otomatik gelir; eski saved JSON'ların `mount_karat/mount_gram/show_currency` alanları yoksa default değerler kullanılır.
- **Migration gerekmedi:** Tüm değişiklikler JSONField içeriğinde — Django default fonksiyonları yeni rows'a uygulanır, `_merge` eski rows'a.
- **Toggle kapsamı:** `.diamond-only-option` div'i HTML'de göründükten sonra JS `_syncDiamondOnlyOptions` ile show/hide; GOLD/WATCH tab'ında DOM'da var ama display:none.
- Helper `_resolve_diamond_label_data` defansif okumalarla sarıldı: `mount_karat='NONE'`, `mount_gram=0.000` durumlarında alan boş döner — etiket boyutu sürpriz büyümez.

---

## FAZ PIRLANTA-EDIT + DETAYLI RAPOR (2026-09-01)

Barkodlu Ürünler ekranında iki iş: **(1)** Pırlanta düzenlemenin çalışmaması (P0),
**(2)** Detaylı Rapor'a ürün grubu + tarih filtresi ve gerçek maliyet.

### 1. Kök nedenler

| # | Belirti | Kök neden |
|---|---------|-----------|
| 1 | Pırlanta "Düzenle" boş Altın formu açıyor | `edit_record()` `material_type`'a HİÇ bakmıyordu, daima `#addGoldModal` + altın alanları. `get_details` yalnızca altın kolonlarını dönüyordu (`DiamondDetail`/`DiamondStone`/`sale_currency` yok). |
| 2 | Kaydedince ikinci ürün oluşurdu | Pırlanta için **UPDATE endpoint'i YOKTU**; `multi_material_product_add` create-only (her çağrıda yeni barkod + yeni `GoldPurchases` + `StockService.record_entry` + `SupplierLedger`). |
| 3 | Para birimi hep USD | Şablonda `<option value="USD" selected>` + view'da `or 'USD'` / `else 'USD'` hard-code. |
| 4 | Maliyet 0 görünüyor | Pırlanta maliyeti `Products.buy_price_eur`'dedir; `Products.clean()` DIAMOND/WATCH için `buy_price_hs`'i **zorla 0'a çeker** ve `get_details` tam da onu dönüyordu. |
| 5 | Rapor filtreleri sahte | `/detailed-report` filtresiz tek aggregate; durum/ayar/arama **JS ile satır gizleyerek**; PDF ayrı bir Python filtresi uyguluyordu. |

### 2. Para birimi SSOT — `apps/settings/currency.py` (YENİ)

- `read_primary_currency(store)` → yapılandırma yoksa **None** ("yok" ile "EUR" ayrımı korunur).
- `get_store_primary_currency(store, default)` → görüntüleme/etiket için.
- `resolve_default_sale_currency(store, allowed, legacy_default='USD')` → **yeni kayıt** varsayılanı.
- Kaynak: `StoreConfiguration.primary_currency`. **Ülke→para birimi hard-code'u YOK.**
  Almanya (EUR) → EUR, TRY yapılandırılmış mağaza → TRY, yapılandırma satırı yok → USD (eski davranış).
- `apps/process/fast_views.py` ve `apps/banking/bank_views.py` içindeki iki kopya helper bu modüle **delege** edildi (üçüncü kopya çıkmadı).
- **Kayıtlı para birimi ASLA ezilmez:** düzenlemede DB'deki `sale_currency` aynen gelir; mağaza varsayılanı yalnız alan gerçekten boşsa uygulanır.

### 3. Düzenleme akışı

- **Yeni endpoint:** `POST /gold-purchases/multi-material-update` → `multi_material_product_update`.
  Barkod/RFID üretmez, `GoldPurchases` açmaz, `StockService`'i **çağırmaz**, `SupplierLedger`/`Process` **yazmaz**.
  `material_type` değiştirilemez; taşlar aynı transaction içinde replace-all.
- `get_details` materyal farkındalı: `material_type` + `diamond{...stones[]}` / `watch{...}` + `stock_pieces` + `has_supplier_ledger` + `can_view_cost` + `cost_currency`.
  Ekranın kendi yetki kodu (`GOLD_PURCHASES_GOLD_PURCHASES_INDEX`) ile korunur.
- Frontend: `window.KP_MULTI_EDIT.open(payload)` doğru sekmeyi açar, formu hydrate eder, taş tablosunu kayıttan kurar; sekmeler kilitlenir, stok adedi read-only olur, cari toggle kapanır, "Kaydet ve Yazdır" gizlenir.
  **Endpoint kararı veriden gelir:** formdaki gizli `gold_purchase_id` doluysa UPDATE, boşsa CREATE.
- **IDOR yaması:** `gold_purchase_add` UPDATE yolundaki `get_object_or_404(Products, id=...)` artık `store=` + `is_deleted=False` ile kapsamlı.

### 4. Detaylı Rapor

- Filtre SSOT'u: `parse_detailed_report_filters(request)` + `_build_detailed_report_rows(store, filters, include_cost)` — ekran ve PDF **aynı** fonksiyonları çağırır.
- **Ürün grubu:** `Products.material_type` (isim eşleştirmesi DEĞİL) — DataTable materyal pill'leriyle aynı sözlük.
- **Tarih semantiği:** Tezgahtaki → `GoldPurchases.created_on`; Satılan → `Process.date` (`transaction_type='SALE'`, `is_status='COMPLETED'`) `Exists` alt sorgusu ile. `updated_at` KULLANILMAZ.
  Aralık gün bazında **inclusive** (`_report_day_range`: bitiş günü + 1 gün exclusive üst sınır, timezone-aware).
- **Maliyet birimi satır bazında:** metal satır → HAS; Pırlanta/Saat → `Products.price_currency` (yoksa mağaza birincil birimi).
  Satırlar maliyet para birimine göre de ayrılır (`cost_currency_key`) — **altın gruplaması etkilenmez** (anahtar sabit `''`).
  Toplamlar `summarize_cost_by_currency()` ile birim bazında ayrı; kur dönüşümü YOK, karışık toplam YOK (`—`).
- **RBAC:** `user_can_view_cost(user)` tek kapı; JSON, ekran ve PDF aynı koşula bağlı. Yetkisiz kullanıcı maliyet alanlarını hiç almaz ve **kayıtlı maliyeti POST ile sıfırlayamaz**.

### 5. Testler

`apps/gold_purchases/tests_diamond_edit_report.py` — 60 test (A–M maddeleri + Altın/Türkiye regresyonu + şablon render).
Toplam `apps.gold_purchases`: **84/84 OK**.
