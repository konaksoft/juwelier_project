## StockSnapshot Model

- `stock_gram`, `stock_pieces`, `weighted_avg_cost_hs`
- `incoming_stock_gram`, `use_custom_pricing`
- `custom_buy_price_hs`, `custom_sale_price_hs`, `custom_fixed_labor`
- `related_name='stock_snapshots'` (eski: `product_inventories`)
- is_currency=True ürünlerde StockSnapshot artık ölü veri — bakiye Payment tablosundan okunur

## StockLedger Model

- `direction`: IN / OUT
- `reason`: PURCHASE, SALE, ADJUSTMENT_PLUS, ADJUSTMENT_MINUS, REVERSAL, TRANSFER_IN, TRANSFER_OUT
- `ref_type`: 'process' | 'scrap' | 'manual'
- `quantity_gram`, `quantity_pieces`
- Eski alan adı eşleme: `quantity_weight` → `quantity_gram`, `movement_type` → `direction + reason`, `process_no` → `ref_type='process' + ref_id`
- StockLedger satırları ASLA silinemez, güncelleme yapılamaz (append-only)

## StockService API

- Tüm stok işlemleri StockService üzerinden yapılır — tablo doğrudan yazma YASAKTIR
- `StockService.record_entry(product, store, quantity_gram, quantity_pieces, reason, ...)` — `select_for_update()` + `transaction.atomic()`
- `StockService.record_exit(...)` — negatif stok koruması burada tetiklenir
- `StockService.adjustment(...)` — ADJUSTMENT_PLUS / ADJUSTMENT_MINUS reason ile

## Negatif Stok Koruması

- 2 katman: application-level `InsufficientStockError` + PostgreSQL `CheckConstraint`
- `StockSnapshot.stock_gram >= 0` DB constraint'i her zaman aktif

## WAC (Ağırlıklı Ortalama Maliyet)

- Sadece stok girişinde hesaplanır (çıkışta değişmez)
- Formül: `(old_gram × old_wac + new_gram × new_cost) / (old_gram + new_gram)`
- Alan: `StockSnapshot.weighted_avg_cost_hs`

## Material Types

- `GOLD` (varsayılan), `SILVER`, `WATCH`, `DIAMOND`
- `Products.material_type` bir kez set edildikten sonra DEĞİŞTİRİLEMEZ (`ValidationError` in clean())
- `_PIECE_ONLY_MATERIALS = frozenset({'WATCH', 'DIAMOND'})`
- WATCH/DIAMOND: `gram==0` ZORUNLU, `pieces>=1` ZORUNLU
- GOLD/SILVER: `gram>0` ZORUNLU
- `Products.save()` otomatik `full_clean()` çağırır; bypass için `skip_validation=True`

## DiamondDetail Model

- 4C: `carat_weight`, `shape`, `color_grade`, `clarity_grade`, `cut_grade`
- Sertifika: `certificate_lab`, `certificate_no`, `fluorescence`
- `is_mounted`, `mount_metal`, `depth_pct`, `table_pct`
- post_save signal: yeni WATCH/DIAMOND ürün kaydedilince otomatik boş detail kaydı oluşturulur (get_or_create)

## WatchDetail Model

- `brand`, `model_name`, `reference_no`, `serial_no`
- `movement_type`, `case_material`, `case_diameter`
- `year_of_mfg`, `warranty_date`, `box_papers`, `condition`

## Gümüş Muhasebe

- `CurrencyChoices.HG = 'Has Gümüş'`
- PriceQuote.MetalType: `SILVER_999`, `SILVER_925`
- `get_ledger_currency()`: GOLD→HS, SILVER→HG, WATCH/DIAMOND→fiat currency
- `BankAccount.currency='HG'` ile gümüş kasa; `get_store_assets_summary()` dinamik olarak HG kırılımını oluşturur

## Türkiye Milyem Standartları

- 14K = 585, 18K = 750, 22K = 916, 24K = 999
- `validate_mileage(value, *, required, field_label)`: geçerli aralık 1-1000; 0 required=True'da reddedilir

## cancel_stock_entry()

- `cancel_stock_entry(ref_type, ref_id, user, fiat_currency, notes)`
- REVERSAL_REASON_MAP ile zıt yönlü kayıt oluşturur
- StockLedger asla silinmez/güncellenmez; sadece REVERSAL kaydı eklenir
- SupplierLedger: `is_active=False` olarak işaretlenir (silinmez)
- `cancel_stock_entry()` frozen_rate = orig_sl.exchange_rate_tl kopyalar reversal için

## WATCH/DIAMOND Piece Fallback

- piece<=0 geldiyse → piece=1, gram=0 (sessiz backend düzeltmesi)
- Management command: `backfill_product_details` — orphan WATCH/DIAMOND kayıtlarını düzeltir

## Gümüş Hurda — İzole Sayfa (ONARIM FAZI 4 / ADIM 4)

- `find_scrap_pool_by_karat(material_type='GOLD'|'SILVER')` — STRICT izolasyon; geçersiz değer ValueError fırlatır (artık sessiz GOLD fallback YOK)
- Gümüş hurda: `material_type='SILVER'`; SupplierLedger.currency='HG' via `_resolve_ledger_currency()`
- Sayfa ayrımı: `scraps/index` (GOLD) ve `scraps/silver/index` (SILVER) — aynı template, `view_material_type` context değişkeniyle ayrışır
- Hidden input `material_type` form üzerinden POST'lanır; kullanıcı manipüle edemez (template hardcode)
- `get_all` endpoint: `?material_type=GOLD|SILVER` query parametresiyle filtrelenir; default GOLD
- Sol menüde ayrı "Gümüş Yönetimi" linki (icon: `fa-coins`)

## Hurda — Havuzlama ve Ref ID Eşleme (ONARIM FAZI 4 / ADIM 1)

- Aynı ayar sınıfındaki hurdalar tek `Products` (havuz) altında birleştirilir; milyem ağırlıklı ortalama ile güncellenir
- Her hurda girişi için BENZERSİZ `sp_process_no = generate_process_no()` üretilir (tedarikçili VE tedarikçisiz dahil)
- StockLedger.ref_id = SupplierLedger.process_no = Process.process_no = `sp_process_no` (üç tablo bire bir bağlı)
- Tedarikçisiz girişler de Process kaydı oluşturur (`supplier=None`); "İşlemler" listesinde görünür
- StockLedger.ref_type = `'scrap_add'` (tek tip; PURCHASE/INITIAL ayrımı `reason` ile)
- Eski davranış (HATALI): `ref_id=f"scrap_{product.id}"` — havuza eklenen tüm girişler aynı ref_id paylaşıyordu

## Hurda İptal — `_cancel_single_process` (ONARIM FAZI 4 / ADIM 3)

- Manuel `record_exit` + `SupplierLedger.update(is_active=False)` YERİNE `cancel_stock_entry(ref_type='scrap_add', ref_id=proc.process_no)` kullanır
- Tedarikçisiz Process'lerde `reverse_supplier_ledger=False` parametresi geçilir (SupplierLedger yok)
- Atomic: StockLedger reversal + SupplierLedger reversal + soft-disable tek transaction'da
- Bug öncesi: orijinal SupplierLedger.is_active=False yapılıyor ama reversal kaydı oluşturulmuyordu → audit trail kopuyordu
- Frontend: çift modal akışı — `get-pool-sources` (Process listesi) → kullanıcı seçer → `delete?selected_process_no=...`

## Hurda Havuzu — Stabilite Sertleştirmesi (ONARIM FAZI 5, 2026-04-27)

- `update_scrap_pool_weighted_mileage` artık `product.save(update_fields=...)` YERİNE `Products.objects.filter(id=...).update(...)` raw SQL UPDATE kullanır → `full_clean()` bariyeri atlanır, `Products.gram` instance'ında kalmış negatif değer alakasız bir partial save'i patlatamaz
- `_cancel_single_process` `Products.gram` düşürmesinde `Greatest(F('gram') - gram, Decimal('0'))` zemin koruması kullanır → legacy `Products.gram` alanı artık negatife düşemez (StockSnapshot.stock_gram'daki DB CheckConstraint korumasının legacy alan karşılığı)
- `update_scrap_pool_weighted_mileage` içindeki `StockSnapshot` okumasına `select_for_update()` eklendi — eş zamanlı hurda girişlerinde WAC milyem hesabı race condition'a düşmez
- `_cancel_single_process` içindeki `except: pass` blokları `logger.error(...)` ile değiştirildi — sessiz başarısızlık yerine operasyonel sinyal düşer
- `current_gram` null guard'ı `if snap.stock_gram` → `is not None` olarak düzeltildi (`Decimal('0')` falsy idi, "snapshot var ama stok 0" yanlış dala düşüyordu)
- Tek dosya değişikliği: `apps/scraps/views.py`. Migration gerekmez

## Hurda Havuzu — UAT Düzeltmeleri (ONARIM FAZI 6, 2026-04-27)

- **Pasif havuza yeniden giriş reset:** `scrap_add` `existing_product` dalı artık `Scraps.is_deleted=False`, `Scraps.is_active=True` ve `Products.is_active=True` bayraklarını otomatik resetler. `Products` reset için `Products.objects.filter(id=...).update(is_active=True)` (atomic, `full_clean()` bypass) kullanılır
- **Update yolu çoklu kaynak guard'ı:** `scrap_add` `if scrap_id:` dalı artık aktif `Process(transaction_type='PURCHASE')` sayısı > 1 VEYA satış geçmişi varsa HTTP 409 ile reddeder. Tek kaynaklı satılmamış havuzlarda `p.save()` yerine `Products.objects.filter(id=...).update(name=..., product_mileage=..., buy_price_hs=..., sale_price_hs=...)` kullanılır → `gram` ezilmez (StockService tek otorite), `full_clean()` tetiklenmez
- **Hayalet kayıt filtresi:** Liste sorgusu artık `qs.exclude(Q(ever_sold=False) & (Q(inv_stock_gram__lte=0) | Q(inv_stock_gram__isnull=True)))` ile tamamen iptal edilmiş + satılmamış kayıtları gizler. Satış geçmişi olanlar (ever_sold=True) tarihsel veri olarak kalır
- **Milyem görünüm fix:** `d_fmt(p.product_mileage, 0)` `.rstrip('0')` ile 590 → "59" üretiyordu. Listede `str(int(p.product_mileage))` ile değiştirildi (milyem her zaman tam sayı)
- **Revival reset (BUG 6):** Silinmiş havuza yeni hurda eklenince eski snapshot stoğu (örn. 13g) + yeni stok (10g) birleşip WAC milyemini kaydırıyordu (23g, 587 milyem). `existing_product` dalında `was_revival = is_deleted OR not is_active` tespit edilir; doğruysa `StockService.adjustment(actual_gram=0, actual_pieces=0)` ile snapshot ADJUSTMENT_MINUS audit satırı oluşturularak sıfırlanır, ardından `Products.objects.filter(id=...).update(gram=0, product_mileage=0)` ile legacy alanlar atomic sıfırlanır. Sonuçta `update_scrap_pool_weighted_mileage` ELSE dalına düşer ve `result_mileage = new_mileage` olur (taze başlangıç)
- Tek dosya değişikliği: `apps/scraps/views.py`. Migration gerekmez

## Hurda Havuzu — Çapraz Modül Çatlağı (ONARIM FAZI 7, 2026-04-27)

- **İptal sonrası WAC geri hesaplama (Bulgu 1):** `apps/scraps/views.py` içinde yeni `recalculate_scrap_pool_mileage_after_cancel(product, store)` fonksiyonu. Aktif `StockLedger` IN girişlerini (`PURCHASE`, `INITIAL`, `ADJUSTMENT_PLUS`) tarar; `_cancel` ile biten OUT satırlarıyla eşleştirip iptal edilmişleri filtreler; kalanlardan ağırlıklı ortalama ile yeni `product_mileage` ve `weighted_avg_cost_hs` hesaplar. Atomic UPDATE ile `Products.product_mileage / buy_price_hs / sale_price_hs` ve `StockSnapshot.weighted_avg_cost_hs` günceller (full_clean bypass). Tüm girişler iptal edilmişse 0'a düşer
- **`_cancel_single_process` ref_type fallback (Bulgu 2 köprüsü):** Toptan modülü `update_product_stock` ile `ref_type='process'` yazıyor; perakende `scrap_add` `ref_type='scrap_add'` kullanıyor. `cancel_stock_entry` artık `('scrap_add', 'process')` sırasıyla denenir; herhangi biri `cancelled_stock_count > 0` veya `supplier_ledger_reversals` üretirse döngü kırılır. İptal sonrası `recalculate_scrap_pool_mileage_after_cancel` çağrılır
- **Toptan tarafı senkronizasyon (Bulgu 2):** `apps/process/wholesale_views.py` `add_scrap_to_wholesale_process` ve `add_scrap_multi_to_wholesale_process` view'ları:
  - `find_scrap_pool_by_karat(..., material_type='GOLD')` — explicit material_type (FAZ 4 / ADIM 4 strict izolasyon kapısı)
  - Yeni havuz oluştururken `Products.material_type='GOLD'` set edilir
  - Mevcut havuza yeniden giriş: `Scraps.objects.get_or_create()` + `is_deleted=False`, `is_active=True`, `Products.is_active=True` reset (BUG 1 toptan eşleniği)
  - Reset varsa **revival reset** (BUG 6 toptan eşleniği): `StockService.adjustment(actual_gram=0, actual_pieces=0, ref_id=f"wholesale_scrap_revival_{...}")` ile snapshot sıfırlanır; `Products.gram` ve `product_mileage` atomic UPDATE ile 0'a çekilir → `complete_process_wholesale` aşamasında gelen yeni hurda WAC için tek belirleyici olur
- **Ghost filter IN_PROGRESS muafiyeti (Bulgu 2 görünürlük):** `get_all` listesinde `has_in_progress=Exists(Process.filter(transaction_type='PURCHASE', is_status='IN_PROGRESS', is_deleted=False))` annotate edildi. Ghost filter `Q(ever_sold=False) & Q(has_in_progress=False) & (stok yok)` koşuluna dönüştürüldü → toptan ekranından eklenmiş ama henüz tamamlanmamış IN_PROGRESS havuzlar Hurda listesinde görünür kalır
- Etkilenen dosyalar: `apps/scraps/views.py`, `apps/process/wholesale_views.py`. Migration gerekmez (StockLedger append-only audit trail tam korunur)
- "WAC çıkışta sabit kalır" prensibi normal akışta korunur; `recalculate_scrap_pool_mileage_after_cancel` SADECE iptal akışında çağrılır

## Hurda Havuzu — `d_fmt` Yapısal Kesim Hatası (ONARIM FAZI 8, 2026-04-27)

- **Bulgu (Toptan UAT):** 10g 585 + 10g 595 hurda girişinde DB'de `product_mileage=590` doğru yazılırken Hurda listesi MİLYEM "59", MALİYET (HS) "0.59" gösteriyordu; modal başlığı ise doğru "590 milyem" yazıyordu. Matematik doğru, sunum kırpıyordu
- **Kök neden:** `apps/scraps/views.py` ve `apps/bracelets/views.py` içindeki ikiz `d_fmt` fonksiyonu `f"{v:f}".rstrip('0').rstrip('.')` zincirini koşulsuz çalıştırıyordu. `Decimal('590')` için `f"{v:f}"` = `"590"` (ondalık nokta YOK) → `rstrip('0')` tam-sayı kısmındaki sondaki sıfırları da kırpıyor → `"59"`. Aynı kusur 600 → "6", 100 → "1", 1000 → "1" üretirdi
- **Liste vs modal asimetrisi:** `get_all` (liste) FAZ 6 BUG 2A sonrası `str(int(p.product_mileage))` kullanıyor (590 doğru); `get_pool_contents` (modal) zaten `int(p.product_mileage or 0)` kullanıyor (590 doğru). UAT'taki "59" görünümü FAZ 6 öncesi `d_fmt(p.product_mileage, 0)` yolundaki yan etkidir
- **Düzeltme:** `d_fmt` artık `rstrip('0')` çağrısını YALNIZCA `s` içinde ondalık nokta varsa uygular: `if '.' in s: s = s.rstrip('0').rstrip('.')`. Tam sayılar (`Decimal('590')`, `Decimal('100')`) artık değişmeden döner; ondalıklı değerler (`Decimal('1.230')` → `"1.23"`) eski davranışı korur
- **`bracelets/views.py:543` `d_fmt(p.product_mileage, 0)` çağrısı yerinde bırakıldı** — düzeltilmiş `d_fmt` ile artık doğru "590" üretir; FAZ 6 BUG 2A `str(int(...))` defansif yaması da yerinde
- **Etkilenen dosyalar:** `apps/scraps/views.py`, `apps/bracelets/views.py` — yalnızca `d_fmt` gövdesi. Migration gerekmez (yalnız render katmanı; veritabanı ve audit trail dokunulmadı)
- **Defansif öneri:** Tam sayı ifade eden alanlar (milyem, ayar, adet) için `d_fmt` yerine `str(int(...))` tercih edilmeli — kontrat regresyonlarına karşı çift katmanlı koruma

## Hurda Havuzu — Kullanıcı Seçimli Ayar Anahtarı (ONARIM FAZI 9, 2026-04-27)

- **Bulgu (Toptan UAT):** 10g 14 Ayar (595 milyem) + 10g 14 Ayar (605 milyem) girişlerinde sistem TEK havuzda 20g 600 milyem yerine İKİ ayrı "14 Ayar" Products kaydı oluşturdu
- **Kök neden:** `karat_from_mileage` ROUND_HALF_UP kullanıyordu. 605 milyem → 14.52 → karat 15 (14 değil!). `find_scrap_pool_by_karat` ikinci girişte target_karat=15 ile arıyor, 14 karat'lı ilk havuzu bulamıyor → yeni Products kaydı. Aynı kusur 600+ milyem'de "14 ayar" girişlerinde, 730+ milyem'de "18 ayar" girişlerinde tetikleniyordu
- **Tasarım değişikliği:** Havuz anahtarı artık **kullanıcının formdan seçtiği ayar adı** (`scrap_name`, ör. "14 Ayar"). Milyem'den TÜRETİLMEZ. Kullanıcı "14 Ayar" seçip 595/605/995 milyem girse de aynı havuzda toplanır
- **`karat_from_mileage` floor düzeltmesi:** ROUND_HALF_UP → `int()` truncation. 14.04/14.28/14.52/14.99 → hepsi 14, 15.00 → 15. Kuyumcu konvansiyonunun "taban" mantığıyla tam uyum (14 ayar = [560, 624] milyem). Bu fonksiyon artık yalnız fallback için
- **Yeni helper'lar:**
  - `SCRAP_KARAT_LABELS` (8/10/14/18/21/22/24 → canonical etiketler)
  - `extract_scrap_karat_label(scrap_name, fallback_mileage, material_type)` — `scrap_name`'den canonical etiket çıkarır; "X Ayar" kalıbına uymayan özel isim (ör. "Eski Yüzük Hurdası") kullanıcı yazımıyla döner (kendi havuzunda kalır)
  - `find_scrap_pool_by_selected_karat(...)` — `Products.name__iexact=canonical_label` ile aramayı yapar; milyem'den bağımsız
  - `find_scrap_pool_by_karat` DEPRECATED wrapper olarak kaldı (geriye dönük uyum)
- **Yeni Products kayıtları:** `name = canonical_karat_label` (ör. "14 Ayar"). Eski "X Milyem Hurda" fallback'i sadece ayar etiketi çıkarılamadığında çalışır
- **Revival koşulu daraltıldı (toptan):** `was_revival` artık `_scrap_reset_fields`'i içermez (taze açılan Scraps satırı revival sayılmaz). Legacy alan sıfırlama (`Products.gram=0, product_mileage=0`) YALNIZCA gerçek stok kalıntısı (`_stale_gram>0 OR _stale_pieces>0`) varsa çalışır
- **`merge_scrap_pool_duplicates` yardımcısı:** Aynı canonical karat etiketindeki birden fazla aktif havuzu en eskisi (primary) altında birleştirir. Process / StockLedger / StockSnapshot / Scraps tüm bağlı kayıtları primary'ye taşır; duplicate Products soft-delete olur; primary'nin meta alanları `recalculate_scrap_pool_mileage_after_cancel` ile tutarlı kılınır. HTTP endpoint: `POST /scraps/merge-duplicates`
- Etkilenen dosyalar: `apps/scraps/views.py`, `apps/scraps/urls.py`, `apps/process/wholesale_views.py`. Migration gerekmez
- **UAT veri temizliği:** Mevcut duplicate kayıtlar `merge-duplicates` endpoint'i ile manuel temizlenir; audit trail StockLedger append-only kalır (yalnızca product FK yeniden hedeflenir)

## SupplierLedger İptal — Bulgu 5 Hayalet Bakiye (ONARIM FAZI 10, 2026-04-27)

- **Bulgu (UAT):** 3 toptan hurda alımı (12.00 + 5.95 + 5.85 = 23.80 HS) "İşlemler" ekranından iptal edildi. Hurda Stok boş ✓, İşlem Geçmişi boş ✓, ancak Tedarikçi "Finansal Durum & Bakiyeler" hâlâ **23.80 HS ALACAKLISINIZ** gösteriyordu
- **Kök neden:** `apps/stock_management/services/cancel_service.py::cancel_stock_entry` SupplierLedger tarafında iki şey yapıyordu — (1) ters `transaction_type`'lı `is_active=True` reversal satır oluştur, (2) orijinal kaydı `is_active=False` yap. `Suppliers.balance_summary()` yalnızca `is_active=True` toplar → reversal aktif (EXIT, +23.80) + orijinal pasif (ENTRY, 0) = **net +23.80 hayalet alacak**. Eski kod yorumundaki "double-entry net sıfır" iddiası matematiksel olarak hatalı: pasif kayıt bakiyeye katkı vermez
- **Tutarsızlık:** Aynı kullanıcı eylemi iki farklı koddan geçince iki farklı sonuç doğuruyordu. `apps/process/operations.py::cancel_row` (genel İşlemler iptali, barkodlu/toptan) yıllardır yalnızca `update(is_active=False)` yapıyor, reversal yazmıyor → bakiye 0. Ancak `_cancel_single_process` (hurda iptal yolu) → `cancel_stock_entry` → reversal + pasif → bakiye 23.80
- **Çözüm — Seçenek A (cancel_row ile tutarlı):** `cancel_stock_entry` artık SupplierLedger reversal satırı ÜRETMEZ; yalnızca orijinal satırları `is_active=False` yapar. `balance_summary()` sonucu temiz şekilde 0'a düşer
- **Geri uyumluluk:** `cancel_stock_entry` dönüş sözlüğündeki `supplier_ledger_reversals` anahtarı kalır ama daima `[]`. Tek caller (`_cancel_single_process` retry break logic) `deactivated_supplier_ledgers > 0` sinyaline geçirildi
- **Audit trail:** Orijinal SL kaydı `is_active=False` ile DB'de korunur (silinmez); iptal Process tablosundaki `is_deleted=True + transaction_type='CANCELED'` ve StockLedger reversal satırlarındaki `IPTAL: ...` notuyla izlenir
- **Etkilenen dosyalar:** `apps/stock_management/services/cancel_service.py` (reversal create bloğu silindi, docstring + import temizliği), `apps/scraps/views.py` (`_cancel_single_process` break sinyali güncellendi). Migration gerekmez
- **UAT veri temizliği (Bulgu 5 öncesi takılı kalmış reversal'lar):** `SupplierLedger.objects.filter(process_no__endswith='_CANCEL', is_active=True).update(is_active=False)` (Django shell, tek seferlik)

## 3 Katmanlı Fiyat Hiyerarşisi

- Katman 3 (Global): `Products.fixed_labor_amount` — varsayılan
- Katman 2 (Dernek): `ChamberProductPrice.fixed_labor_amount` — dernek fiyatı varsa override
- Katman 1b (Mağaza İşçilik): `StockSnapshot.custom_fixed_labor` — HER ZAMAN okunur, has modundan BAĞIMSIZ
- Katman 1a (Mağaza Has): `StockSnapshot.custom_buy/sale_price_hs` — SADECE `use_manual_has_calculation=True` ve `use_custom_pricing=True` ise okunur

## İşçilik Okuma Kuralı

- `use_average_labor` (işçilik) ile `use_manual_has_calculation` (has fiyat) AYRI setting'lerdir
- Katman 1b: `use_custom_pricing=True` AND `custom_fixed_labor > 0` — has modundan bağımsız
- Katman 1a: `use_manual_has_calculation=True` AND `use_custom_pricing=True` AND `custom_buy/sale_price_hs > 0`
- `> 0` kontrolü zorunlu: sadece işçilik girilip kaydetme durumunda has fiyatları sıfırlanmasın
- Etkilenen: `get_all()` ve `get_product_details()` in apps/products/views.py

## PriceFeed Sistemi

- Redis cache (`live_data:{store_id}`, TTL=4s) → CeleryTask → PriceQuote DB fallback
- `update_products_from_api.delay()` — fast_index_view'da async çağrı
- `warmup_price_cache`: sunucu restart sonrası Redis'i DB'den doldurur
- Yeni sağlayıcı eklemek için kod değişikliği gerekmez; Admin'den `PriceProvider` + `PriceProviderMapping` kaydı eklenir

## Emekliye Ayrılan Tablolar

- `Inventories`, `InventoryMovement`, `Scraps` (stok bağlamında), `Bracelets` (stok bağlamında) — OPERASYONEL KODDA KULLANILMAZ
- Bu tabloların importları yasaktır (`from apps.inventories.models import ...`)
- Yalnızca `fake_data.py` (geliştirme seed scripti) erişebilir; tablolar DROP edilmemiş

## Sayım Modülü (RFID Buffer/Batch)

- `bulk_scan_for_count` endpoint: `{"session_id": "uuid", "codes": [...]}`
- `BUFFER_MAX = 50` — 50 kodda otomatik flush
- `FLUSH_DELAY_MS = 500` — 500ms sessizlikte otomatik flush
- `processedBarcodes` Set — oturum boyunca mükerrer önleme
- Tüm işlemler tek `transaction.atomic` içinde; `bulk_create` + `bulk_update` ile

## Dashboard Multi-Material Rollup

- `DailyStoreReport` yeni alanlar: `silver_stock_gram`, `silver_stock_value_hg`, `silver_stock_value_tl`, `diamond_stock_pieces`, `diamond_stock_value_tl`, `watch_stock_pieces`, `watch_stock_value_tl`
- `compute_daily_store_report()`: `stock_gram__gt=0` filtresi kaldırıldı (WATCH/DIAMOND gram=0)
- `.exclude(product__is_currency=True)` korundu
- `_build_multi_material_stock_aggregate()` — tek sorguda conditional Sum ile tüm material breakdown
- `total_has_value` ve `stock_value_hs` artık `material_type='GOLD'` filtresiyle kısıtlı (HG karışmasın)
- `karat_breakdown` içindeki 5 alan `material_type='GOLD'` filtresi ile sınırlı

## Bilezik Havuzu — İsim Anahtarlı Birleştirme (BİLEZİK ONARIM FAZI, 2026-04-27)

- **Havuz anahtarı:** Bilezik havuzları **isim** (`Products.name`, case-insensitive) ile gruplanır. Hurdadan farklı: karat/milyem türetilmez. Aynı isimli ("Burma", "Ajda") farklı milyem girişleri tek havuzda WAC milyem ile birikir
- **`find_bracelet_pool_by_name(store, category, name)`:** `apps/bracelets/views.py`'de yeni finder. `name__iexact` + `is_deleted=False` + `is_active=True`. Eski `find_bracelet_pool_by_name_milyem` DEPRECATED wrapper olarak kaldı (geriye dönük uyum)
- **Benzersiz `bp_process_no`:** Her bilezik girişi için `generate_process_no()`; StockLedger.ref_id = SupplierLedger.process_no = Process.process_no = bp_process_no. Eski `ref_id=f"bracelet_{p.id}"` kaldırıldı (zincirleme iptal riski)
- **`_cancel_bracelet_process`:** `cancel_stock_entry()` çağırır; `ref_type` fallback `('bracelet_add', 'process')` ile hem perakende hem toptan kaynaklı satırları yakalar. `Products.gram` düşürmesi `Greatest(F('gram') - gram, Decimal('0'))` zemin korumasıyla — legacy alan negatife düşemez
- **`update_bracelet_pool_weighted_mileage(product, store, new_gram, new_mileage)`:** `StockSnapshot` `select_for_update`; ağırlıklı ortalama milyem; atomic `Products.objects.filter(id=...).update(...)` (full_clean bypass — Hurda FAZ 5 ADIM A deseni)
- **`recalculate_bracelet_pool_mileage_after_cancel(product, store)`:** İptal sonrası WAC geri hesaplama; aktif IN girişlerini `_cancel` ile biten OUT satırlarıyla eşleştirip kalandan ROUND_HALF_UP tam sayı milyem üretir; tüm girişler iptal edilmişse 0'a düşer (revival semantiği)
- **`bracelet_add` update yolu çoklu kaynak guard'ı:** Aktif `Process(transaction_type='PURCHASE', is_status='COMPLETED')` sayısı > 1 VEYA satış geçmişi varsa HTTP 409 `MULTI_SOURCE_POOL` ile reddedilir; tek kaynaklı havuzda atomic UPDATE kullanılır (Hurda FAZ 6 BUG 3 eşleniği)
- **Toptan senkronizasyonu (`add_bracelet_to_wholesale_process`):** Lazy import ile `find_bracelet_pool_by_name` çağrılır; aktif havuz yoksa pasif/silinmiş candidate aranır; yoksa yeni `Products`. `Bracelets.objects.get_or_create()` + flag reset (`is_deleted=False, is_active=True, Products.is_active=True`). **Revival reset:** stok kalıntısı varsa `StockService.adjustment(actual_gram=0, actual_pieces=0, ref_id=f"wholesale_bracelet_revival_...")` ile snapshot sıfırlanır + `Products.gram=0, product_mileage=0` atomic UPDATE (Hurda FAZ 7 ADIM 3 eşleniği)
- **`complete_process_wholesale` bilezik WAC bloğu:** Scrap WAC bloğundan sonra eklendi; `Bracelets.objects.filter(product=p.product, store=...).exists()` ise `update_bracelet_pool_weighted_mileage` çağrılır → toptan ENTRY tamamlandığında perakende `bracelet_add` ile aynı WAC semantiği uygulanır
- **Ghost filter + IN_PROGRESS muafiyeti:** `get_all` listesinde `has_in_progress=Exists(Process.filter(transaction_type='PURCHASE', is_status='IN_PROGRESS', is_deleted=False))` annotate; ghost filter `Q(ever_sold=False) & Q(has_in_progress=False) & (inv_stock_weight<=0 | isnull)` → toptan'dan eklenmiş IN_PROGRESS havuzlar listede görünür kalır
- **Milyem render defansifliği:** `str(int(p.product_mileage)) if p.product_mileage is not None else '0'` — FAZ 6 BUG 2A + FAZ 8 d_fmt düzeltmesinin çift katmanlı koruması
- Etkilenen dosyalar: `apps/bracelets/views.py` (komple rewrite), `apps/process/wholesale_views.py` (`add_bracelet_to_wholesale_process` + `complete_process_wholesale` WAC bloğu + Q import). Migration gerekmez (StockLedger append-only audit trail tam korunur)

## Hayalet Filtre + Multi-Source Pool UX (UAT 1 & 2, 2026-04-27)

- **UAT-1A — Hayalet filtre açığı (Bilezik + Hurda):** `get_all` içindeki `ever_sold_q` ve `last_sale_sq` subquery'leri `is_deleted=False` filtresini eksik bırakıyordu. İptal edilmiş satışlar `ever_sold=True` üretiyor → ghost filter `Q(ever_sold=False)` koşuluna takılmıyor → 0 gram + iptal edilmiş satış geçmişli kayıtlar listede kalıyordu. **Fix:** Her iki subquery'ye `is_deleted=False` eklendi (`apps/bracelets/views.py` + `apps/scraps/views.py`)
- **UAT-1B — `is_multi_source_pool` sinyali:** `get_all` response'una iki yeni alan eklendi:
  - `active_purchase_count` — aktif `Process(transaction_type='PURCHASE', is_status='COMPLETED', is_deleted=False)` satır sayısı
  - `is_multi_source_pool` = `active_purchase_count > 1`
  - Bilezik: mevcut `supplier_map` loop'una `process_count` eklendi (ek SQL yok)
  - Hurda: `Coalesce(Subquery(Count('id')), 0)` ile annotate (N+1 koruması)
  - **Önemli:** `suppliers_count` (tedarikçi entity sayısı) ile aynı şey değildir; aynı tedarikçiden 2+ ayrı giriş de multi-source sayılır — backend 409 guard'ı zaten `active_purchase_count > 1` ile çalışıyor
- **UAT-2 — Multi-source pool UX (kalem ikonu + 409 önleme):** Çoklu kaynaklı havuzlarda backend `MULTI_SOURCE_POOL` HTTP 409 döndürüyor; eskiden çirkin "Error: 409" alert ile karşılaşılıyordu. İki katmanlı koruma:
  1. **Render katmanı:** `is_multi_source_pool=true` ise template kalem ikonunu hiç render etmez; yerine `fa-layer-group` ikonlu `btn-light-warning` renkli havuz detay butonu gösterilir, tooltip "düzenlemek için ilgili işlemi iptal edin" yönlendirmesi yapar
  2. **Defansif katman:** `edit_bracelet` / `edit_scrap` JS fonksiyonları da `is_multi_source_pool` kontrolü yapar (eski cache / programatik çağrı senaryoları için); 409 round-trip yerine SweetAlert ile şık bilgi mesajı gösterilir
- Backend 409 guard'ı korunur — kullanıcı bu kapıya çarpmaz, ama programatik çağrılar yine engellenir
- **Doğal yan etki:** "İşlemler > İptal" sonrası `Process.is_deleted=True` olduğu için iptal edilen alışlar `active_purchase_count`'a sayılmaz — 3 alış + 2 iptal → `active_purchase_count=1` → kalem ikonu otomatik geri döner
- Etkilenen dosyalar: `apps/bracelets/views.py`, `apps/scraps/views.py`, `templates/management/bracelets/index.html`, `templates/management/scraps/index.html`. Migration gerekmez

## Perakende Mimari Hizalama (R-FAZ, 2026-04-28)

- **R-2 (acil crash fix) — `Products.gram` partial save:** `apps/process/retail_views.py` `add_scrap_to_process` artık `p.save(update_fields=['gram','buy_price_tl'])` yerine `Products.objects.filter(id=...).update(gram=Greatest(F('gram')+gram, Decimal('0')), buy_price_tl=...)` kullanır → instance'ta kalmış negatif değerin alakasız `full_clean()` çakışmasını engeller (Hurda FAZ 5 ADIM A perakende eşleniği)
- **R-Faz 1 — Hurda havuzlama perakendeye taşındı:** `add_scrap_to_process` artık `find_scrap_pool_by_selected_karat(scrap_name, fallback_mileage, material_type)` (kullanıcı seçtiği ayar adıyla aramalı, milyem türetilmez — Hurda FAZ 9 deseni); canonical karat etiketiyle yeni `Products` oluşturur; `was_revival` koşulu (silinmiş havuza yeniden giriş) tespit edildiğinde `StockService.adjustment(actual_gram=0, actual_pieces=0)` ile snapshot sıfırlanır + `Products.gram=0, product_mileage=0` atomic UPDATE; `update_scrap_pool_weighted_mileage` ile WAC milyem; `process_id = uuid.uuid4()` üretilip `StockLedger.ref_id = str(process_id)` (per-line cancel için Process.id kullanımı). `ref_type='scrap_add'` (merkezî hurda akışıyla aynı)
- **R-Faz 2 — Bilezik havuzlama perakendeye taşındı:** `add_bracelet_to_retail_process` artık `find_bracelet_pool_by_name(store, category, name)` kullanır; `was_revival` reset Bracelets eşleniğiyle aynı; deferred stock pattern korunur (stok `complete_process` aşamasında işler), `Process.id = uuid.uuid4()` set edilir
- **R-Faz 5 — Unified Cancel:** `apps/process/views.py::update_product_stock` artık opsiyonel `process_id` parametresi alır; verilirse `StockLedger.ref_id = str(process_id)` (Process.id UUID), aksi halde geriye dönük uyum için `process_no`. `complete_process` retail_views'da üç dalda da (unique-barcoded, scrap EXIT, default) `process_id=p.id` geçirir. `apps/process/operations.py::_revert_process_stock` artık manuel ters `record_entry/record_exit` yerine `cancel_stock_entry(ref_type=..., ref_id=str(p.id))` kullanır; `ref_type` PURCHASE/scrap için `'scrap_add'`, diğer satırlar için `'process'`. İptal sonrası uygun havuz `recalculate_scrap_pool_mileage_after_cancel` veya `recalculate_bracelet_pool_mileage_after_cancel` ile yeniden hesaplanır. `cancel_row` / `cancel_group` custody satırlarında `record_exit('cancel_process_custody') + c_rec.delete()` yerine `cancel_stock_entry(ref_type='process_custody', ref_id=str(p.id))` + `hasattr(c_rec, 'is_deleted')` defansif soft-delete (yoksa hard-delete fallback)
- **Müşteri bakiye reverse — R-Faz 4'e ertelendi:** `complete_process` `customer.payable_hs/receivable_hs` mutasyonu audit trail'siz; güvenli reverse için CustomerLedger (Seçenek A) tasarımı gerekli. R-Faz 4 ayrı kullanıcı onayı bekler
- **R-Faz 3 — WAC Kâr Hesabı:** `complete_process`'te kâr hesaplama döngüsü stok döngüsünden SONRA çalışır; ayrıca her satır için `StockSnapshot.weighted_avg_cost_tl` okunup `purchase_amount_per_unit` olarak kullanılır (yoksa `Products.buy_price_tl` fallback). Bilezik havuzu için stok döngüsünden sonra ek bir blok eklendi: PURCHASE rows içinde `Bracelets.objects.filter(product=p.product, store=store).exists()` ise `update_bracelet_pool_weighted_mileage` çağrılır → toptan `complete_process_wholesale` semantiğiyle eşleşir
- **R-Faz 6 — `Products.gram` Tutarlılığı:** Hurda SALE dalı artık `Products.objects.filter(id=...).update(gram=Greatest(F('gram')-_dec(p.gram), Decimal('0')))` ile legacy `gram` alanını da düşürür (StockSnapshot otoritesi korunurken görüntü/listeleme katmanı senkron kalır). `_revert_process_stock` ayrıca PURCHASE iptalinde `Greatest(F-gram,0)` zemin korumasıyla, SALE iptalinde `F+gram` ile legacy alanı geri yükler
- Etkilenen dosyalar: `apps/process/retail_views.py`, `apps/process/views.py::update_product_stock`, `apps/process/operations.py` (`_revert_process_stock` rewrite + `cancel_row`/`cancel_group` custody hizalaması). Migration gerekmez (StockLedger append-only audit trail tam korunur; reversal'lar `_cancel` suffix ile eşleşir)

## R-Faz 4 — CustomerLedger (Müşteri Carisi Audit Trail, 2026-04-28)

- **Yeni model `apps/customers/models.py::CustomerLedger`:** `customer FK`, `store FK(null)`, `process_no` (Process.process_no eşleşmesi), `transaction_type` (`DEBT` = müşteri borçlandı / `CREDIT` = mağaza borçlandı), `amount_hs`, `exchange_rate_tl` (kur snapshot), `description`, `is_active`, `created_on`. Migration: `0005_customerledger.py`. SupplierLedger FAZ 10 deseniyle: REVERSAL kaydı yok, iptalde `is_active=False`
- **`Customers.balance_hs` property:** `SUM(amount_hs WHERE DEBT, is_active=True) - SUM(amount_hs WHERE CREDIT, is_active=True)`. Pozitif → müşteri borçlu, negatif → mağaza borçlu. `receivable_hs_computed` / `payable_hs_computed` yardımcı property'leri ile eski statik alanların okuma muadili. Statik `payable_hs` / `receivable_hs` alanları korunur (ama artık yazılmaz) — geriye dönük rapor + carry-forward semantiği için
- **`complete_process` (retail_views.py):** Statik mutasyon (`curr_pay/curr_rec` netleştirme + `customer.save(update_fields=...)`) tamamen silindi. Yerine `CustomerLedger.objects.create(transaction_type='DEBT'|'CREDIT', amount_hs=..., exchange_rate_tl=...)`. Netleştirme örtük olarak `balance_hs` aggregate'i ile yapılır → `payable_hs >= new_debt_hs` dallanmasına gerek kalmadı. `process_no = procs[0].process_no` (checkout grup anahtarı)
- **`cancel_group` (operations.py):** `SupplierLedger` pasifleştirmenin hemen ardından `CustomerLedger.objects.filter(process_no=pn, is_active=True).update(is_active=False)` — toplu iptalde tüm cari satırları aynı transaction'da pasifleşir
- **`cancel_row` PROC dalı:** Tek satır iptalinde grup içinde kalan aktif Process var mı kontrolü (`Q(is_deleted=True) | Q(is_status='CANCELED') | Q(transaction_type='CANCELED')` exclude + mevcut p.id exclude). Hiç aktif kalmadıysa CustomerLedger satırları pasife çekilir; aksi halde olduğu gibi bırakılır (ödeme farkı kalan satırlar için geçerli)
- **Audit trail:** Pasif satır DB'de korunur; `created_on` + `process_no` + `description` ile kim/ne zaman/hangi işlemden izlenir. `is_active=False` çift yazım yok → SupplierLedger FAZ 10 hayalet bakiye bug'ı CustomerLedger'da baştan engellendi
- Etkilenen dosyalar: `apps/customers/models.py`, `apps/customers/migrations/0005_customerledger.py`, `apps/process/retail_views.py` (complete_process bakiye bloğu), `apps/process/operations.py` (cancel_row + cancel_group). Migration ÇALIŞTIRILMASI GEREKİR (`python manage.py migrate customers`)
- **Geçiş notu:** Geçiş tarihinden önce birikmiş `Customers.payable_hs` / `receivable_hs` değerleri aynen korunur (carry-forward). Yeni işlemler ledger'a yazılır. Tam ledger geçişi için ileride `BALANCE_CARRY_FORWARD` seed migration'ı eklenebilir; şu an statik alanlar legacy snapshot olarak okunabilir

## R-Faz 7 — Perakende Sepet "Erken Stok Girişi" Onarımı (Lifecycle Alignment, 2026-04-28)

- **Bulgu (UAT):** Perakende ekranında hurda sepete eklediği anda (henüz `complete_process` çağırılmadan) StockLedger ENTRY ve WAC milyem güncellemesi gerçekleşiyordu. Sonuç: (1) Process IN_PROGRESS iken stok artıyor, (2) müşteri henüz set edilmediği için Hurda detay modalı "Tedarikçisiz" gösteriyor, (3) taslak Process satırları havuz iptal modallarında satır olarak çıkıp iptal edilebilir hale geliyor
- **Kök neden:** R-Faz 1 entegrasyonu `add_scrap_to_process` fonksiyonunu hurda paneli (`scrap_add`, anında COMPLETED) deseniyle bağladı; oysa retail sepet bilezik/toptan deseniyle (deferred-commit) çalışmalıydı. Hurda ile bilezik aynı `complete_process` içinde iki farklı zaman modeliyle koşuyordu — mimari tutarsızlık
- **`add_scrap_to_process` (apps/process/retail_views.py):** `update_scrap_pool_weighted_mileage` çağrısı ve her iki dalın (mevcut havuz + yeni havuz) `StockService.record_entry` çağrıları KALDIRILDI. Yeni havuz oluşturma `gram=Decimal('0')` ile yapılır (taslak — hayalet filtre listede gizler). Mevcut havuz dalında `Products.gram` artırma + `buy_price_tl` yenileme çağrıları kaldırıldı (hepsi `complete_process`'e taşındı). `Process.is_status='IN_PROGRESS', waiting_stock=False` (bilezik perakende deseniyle tutarlı)
- **`complete_process` `is_scrap_product` dalı:** Sadece EXIT'i değil, ENTRY'yi de işliyor: (1) `update_scrap_pool_weighted_mileage(product, store, p.gram, p.process_mileage)` — havuz WAC milyemi, (2) `update_product_stock(... process_id=p.id)` — `record_entry` (`ref_type='process'`, `ref_id=str(p.id)`), (3) `Products.gram` artırma + `buy_price_tl` yenileme (`Greatest 0` floor + atomic `filter().update()`)
- **`_revert_process_stock` (apps/process/operations.py) — ref_type fallback:** R-Faz 7 sonrası hurda PURCHASE artık `ref_type='process'` ile yazılır (eski R-Faz 1 sürümü `'scrap_add'` ile yazıyordu). Geriye dönük uyum için hurda PURCHASE'da `('process', 'scrap_add')` sırasıyla denenir; ilk eşleşme bulan döngüyü kırar. Sonuç: deploy öncesi sepete eklenmiş ama sonra iptal edilen taslak hurda satırları + R-Faz 7 sonrası satırlar AYNI iptal yolundan temiz şekilde geri alınır. Hiç eşleşme yoksa `info` log'u düşer (taslak iptali veya tekrar-iptal)
- **Liste görünürlüğü (Adım 4):** Havuz iptal modalları (`get_pool_sources`, `get_pool_contents`, `delete` endpoint linked_procs) HEM hurda HEM bilezik tarafında `.exclude(is_status='CANCELED')` yerine `is_status='COMPLETED'` filtresine geçti → taslak (IN_PROGRESS) Process satırları artık iptal modallarında gözükmez. Üç dosya × üç pattern: `apps/scraps/views.py` L1446, L1513, L1610; `apps/bracelets/views.py` L1050, L1116, L1202
- **Customer atama doğru zamanda:** `complete_process` mevcut akışı `procs.update(customer=customer)` ile customer'ı son anda set eder. Stok hareketi artık customer set edildikten SONRA gerçekleştiği için Hurda detay modalı kaynak olarak doğru müşteri/tedarikçi etiketini gösterir
- **Etkilenen dosyalar:** `apps/process/retail_views.py` (add_scrap_to_process — stok bloğu silindi; complete_process is_scrap_product dalı ENTRY kolu eklendi), `apps/process/operations.py::_revert_process_stock` (ref_type fallback), `apps/scraps/views.py` (3 yer COMPLETED filter), `apps/bracelets/views.py` (3 yer COMPLETED filter). Migration gerekmez
- **R-Faz 1 ile ilişki:** R-Faz 1 entegrasyonu havuz eşleştirme + revival reset + canonical karat label semantiklerini retail'e taşıdı (KORUNUR). R-Faz 7 sadece "stok kaydı zamanı" + "WAC milyem zamanı"nı `complete_process`'e ertelemekle sınırlıdır; yapısal SSOT (find_*_pool_by_*, update_*_pool_weighted_mileage) çağrıları aynı kalır
- **R-Faz 5 ile ilişki:** `_revert_process_stock` `cancel_stock_entry(ref_type, ref_id)` ile çalışmaya devam eder; tek değişiklik ref_type fallback adı. R-Faz 4 CustomerLedger pasifleştirme yolu da aynen çalışır (process_no eşleşmesi)

## Ürün Formu (Frontend)

- `product_form/` modülleri: `config.js` (pure data), `field_groups.js` (DOM resolver), `renderer.js` (UI updater), `validator.js` (client-side), `manager.js` (orchestrator)
- Yükleme sırası: config → field_groups → renderer → validator → manager (ÖNEMLİ, bağımlılık zinciri)
- `<form data-product-form>` attribute'u ile otomatik bootstrap
- Yeni material_type eklemek için sadece `CONFIG` objesine anahtar eklenir
