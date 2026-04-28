## Products.clean() Kuralları

- `material_type` immutable: bir kez set edildikten sonra ValidationError (değiştirilemez)
- WATCH/DIAMOND → gold fields sıfırlanır (gram=0, related gold fields=0)
- `gram >= 0` kontrolü
- `prices >= 0` kontrolü
- `Products.save()` otomatik `full_clean()` çağırır; bypass için `skip_validation=True`

## WATCH/DIAMOND Stok Kuralları

- WATCH/DIAMOND: `gram==0` ZORUNLU; `pieces>=1` ZORUNLU
- `_validate_material_type_quantities()`: WATCH/DIAMOND → gram==0 + pieces>=1; GOLD/SILVER → gram>0
- piece<=0 gelirse → piece=1, gram=0 sessiz backend düzeltmesi
- post_save signal: yeni WATCH/DIAMOND ürün kaydedilince otomatik boş detail kaydı (get_or_create)
- `backfill_product_details` management command: orphan WATCH/DIAMOND kayıtlarını düzeltir

## İşçilik Sistemi

- `use_average_labor` (işçilik) `use_manual_has_calculation` (has fiyat) ile AYRI setting'lerdir
- Katman 1b — Mağaza İşçilik: `use_custom_pricing=True` AND `custom_fixed_labor > 0` — has modundan BAĞIMSIZ
- Katman 1a — Mağaza Has: `use_manual_has_calculation=True` AND `use_custom_pricing=True` AND `custom_buy/sale_price_hs > 0`
- `> 0` koruması: sadece işçilik girilip kaydedildiğinde has fiyatları sıfırlanmasın
- `update_inventory_ajax()`: `payload['fixed_labor_amount']` → `snap.custom_fixed_labor` + `snap.use_custom_pricing=True`
- `update_inventory_bulk_ajax()`: `data['fixed_labor_amount']` → `snap.custom_fixed_labor` + `snap.use_custom_pricing=True`
- Etkilenen sayfalar: Ürün Listesi (`get_all()`), Hızlı İşlem (`get_product_details()`)
- Perakende (`get_categories_with_products()`) etkilenmedi — DB'den direkt okuyor

## SupplierLedger Güncelleme

- `SupplierLedger.exchange_rate_tl` alanı eklendi (migration 0005)
- Döviz işlemlerinde kur işlem anında dondurulur (`exchange_rate_tl` frozen rate)
- `cancel_stock_entry()`: reversal için `frozen_rate = orig_sl.exchange_rate_tl` kopyalanır

## Hurda İşleme

- `find_scrap_pool_by_karat(material_type='GOLD')` — backward compatible (default='GOLD')
- Gümüş hurda: `material_type='SILVER'`; SupplierLedger.currency='HG' via `_resolve_ledger_currency()`
- Hurda formu: sadece GOLD ve SILVER seçenekleri (WATCH/DIAMOND hurdalanamaz)

## Tamir/İşçilik Kayıt Kuralları

- İşçilik tutarı `custom_fixed_labor` alanına kaydedilir
- `use_custom_pricing=True` set edilmesi gerekir
- Perakende ekranı işçilik değerini DB'den direkt okur (katman mantığı bypass)
- Toplu kaydetme: `update_inventory_bulk_ajax()` tüm seçili satırlar için aynı mantık
