## Tedarikçi Cari

- `validate_mileage(value, *, required, field_label)`: geçerli aralık 1-1000 (dahil); required=True'da 0 reddedilir
- `add_scrap_multi_to_wholesale_process`: sessiz atlatma yerine `skipped_rows` listesi döner
- `supplier_adjustment_create` modları: ALL / BY_CURRENCY / CUSTOM

## ADJ (Düzeltme) İşlem Numarası

- `SupplierLedgerService.generate_adjustment_process_no()` format: `ADJ-YYYYMMDD-HHMMSS-supplier8-random4`
- `secrets.token_hex(2)` ile random bölüm; 5 deneme + UUID fallback
- ADJ- öneki FIFO otomatik mahsuba dahildir (OB- öneki gibi hariç tutulmaz)

## Geliştirme Kuralları

- Terminal komutları ÇALIŞTIRILMAZ
- Business logic değişikliği yapılmaz (kullanıcı onayı olmadan)
- Dosyalar TAMAMEN yazılır (kısmi değil)
- Her adım sonrası context .md dosyaları güncellenir

## Kayıt Yönetimi Genel Kurallar

- `Stores.is_active = False` varsayılan (başlangıçta inaktif)
- Her mağaza için otomatik StoreConfiguration, varsayılan BankAccount'lar oluşturulur
- `Products.is_protected=True` → yeni mağaza açılınca StockSnapshot.bulk_create()

## Fiyat Hiyerarşisi (3 Katman Özeti)

- Katman 3 (Global): `Products.fixed_labor_amount` → varsayılan
- Katman 2 (Dernek): `ChamberProductPrice.fixed_labor_amount` → override
- Katman 1b (Mağaza İşçilik): `StockSnapshot.custom_fixed_labor` → her zaman okunur
- Katman 1a (Mağaza Has): `StockSnapshot.custom_buy/sale_price_hs` → manuel has modu gerekir
- Custom fiyatlar Products tablosunda DEĞİL, StockSnapshot'ta (mağaza filtreli)

## İptal/Geri Sarma Kuralları

- StockLedger satırları silinmez/güncellenmez — REVERSAL kaydı eklenir
- SupplierLedger: `is_active=False` (silinmez) + ZIT YONLU REVERSAL kaydı OLUŞTURULMALI (sadece is_active=False yetmez!)
- Payment: `is_cancelled=True` (silinmez)
- `cancel_stock_entry()` evrensel iptal utility: REVERSAL_REASON_MAP + frozen rate kopyalama
- Manuel `SupplierLedger.update(is_active=False)` ÇAĞRISI YASAK — daima `cancel_stock_entry()` kullanılır (bkz: BUG B / Onarım Fazı 4 Adım 3)
- Çoklu giriş içeren havuz iptallerinde `ref_type` + `ref_id` BENZERSİZ olmalı; aksi halde tek cancel zincirleme tüm havuzu hedef alır (bkz: BUG A / Onarım Fazı 4 Adım 1)

## Toplu İşlem Kuralları

- `select_for_update()` + `transaction.atomic()` her stok operasyonunda zorunlu
- Race condition koruması: FX bakiye okuma + Payment yazma aynı transaction içinde
- `BankAccount.objects.select_for_update().get(id=...)` FX guard için önerilir

## Emekliye Ayrılan Modeller

- `Inventories`, `InventoryMovement`, `Scraps`, `Bracelets` — operasyonel kodda import YASAK
- Bu tablolar DROP edilmemiş; sadece `fake_data.py` erişebilir

## Dil ve Yanıt Kuralları

- Tüm yanıtlar ve context .md logları TÜRKÇE yazılır

## Bakiye Kontrol Yönü Kuralı (Evrensel)

- GİRİŞ (PURCHASE): bakiye kontrolü YAPILMAZ (stok/para geliyor)
- ÇIKIŞ (SALE): bakiye kontrolü YAPILIR (stok/para gidiyor)
- Bu kural hem backend hem frontend için geçerli; tüm ekranlarda tutarlı uygulanır

## Onarım Fazı 4 — Hurda Cari Sıfırlanma Bug Düzeltmesi (2026-04-27)

- **BUG A (ref_id mismatch):** `scrap_add` view'ında `StockLedger.ref_id=f"scrap_{product.id}"` yazılıyordu; havuza eklenen tüm girişler aynı ref_id'yi paylaşıyordu. "İşlemler > İptal" sayfasından bir girişi iptal etmek `cancel_stock_entry`'nin tüm havuzu hedef almasına yol açıyordu. **Fix:** Her giriş için `sp_process_no = generate_process_no()` üretilir; StockLedger.ref_id = SupplierLedger.process_no = Process.process_no
- **BUG B (eksik reversal):** `_cancel_single_process` fonksiyonu `SupplierLedger.update(is_active=False)` yapıyor ama zıt yönlü reversal satırı oluşturmuyordu; bu yüzden cari bakiyesi iz bırakmadan sıfıra düşüyordu. **Fix:** `cancel_stock_entry()` kullanılır — atomic StockLedger reversal + SupplierLedger reversal + soft-disable
- **BUG C (F() + save() + full_clean() çakışması):** `existing_product.gram = F('gram') + gram` ve ardından `existing_product.save(update_fields=['gram'])` 500 Internal Server Error'a yol açıyordu. `Products.save()` otomatik `full_clean()` çağırıyor; `clean()` içindeki `if self.gram < Decimal('0')` kontrolü `CombinedExpression` ile `Decimal`'i karşılaştırmaya çalışıyor → `TypeError: '<' not supported between instances of 'CombinedExpression' and 'decimal.Decimal'`. **Fix:** `Products.objects.filter(id=existing_product.id).update(gram=F('gram') + gram)` kullanılır — atomic SQL UPDATE, Python instance'ı dirty olmaz, `full_clean()` bariyeri tetiklenmez. `_cancel_single_process`'deki `gram=F('gram') - gram` deseniyle tutarlıdır
- **Tedarikçisiz Process:** Tedarikçisiz hurda girişlerinde de artık Process kaydı oluşur (`supplier=None`); böylece "İşlemler" listesinde görünür ve `cancel_stock_entry` ile iptal edilebilir
- **Gümüş izolasyonu:** `silver_index` view + `scraps/silver/index` URL eklendi; `view_material_type='SILVER'` ile aynı template'i SILVER moduna sabitler (hidden input + JS sabit). `find_scrap_pool_by_karat` artık geçersiz material_type'a ValueError fırlatır (sessiz GOLD fallback kaldırıldı)
- **Etkilenen dosyalar:** `apps/scraps/views.py`, `apps/scraps/urls.py`, `templates/management/scraps/index.html`, `templates/management/base.html`

## F() Expression + save() Tuzağı (Genel Kural)

- `instance.field = F('field') + x` ardından `instance.save()` ÇAĞIRMA — eğer model `clean()` içinde alanı Python değeriyle karşılaştırıyorsa (`if self.field < 0` gibi) `TypeError` fırlar
- Doğru desen: `Model.objects.filter(id=instance.id).update(field=F('field') + x)` — atomic SQL, instance dirty olmaz, `full_clean()` tetiklenmez
- Alternatif (önerilmez): `instance.save(skip_validation=True)` — `Products.save()` özelinde mevcut ama bypass davranışı kırılgan; ileride başka kontrollerde tekrar patlayabilir
- Kontrol: `grep -n "F(['\"]\\w+['\"]).*save("` ile tüm kodu tara, kalan bulguları temizle

## full_clean() + update_fields Semantik Tuzağı

- Django'da `instance.save(update_fields=['x', 'y'])` SADECE `x` ve `y` sütunlarını SQL'e yazar; ANCAK `Products.save()` override'ı her durumda `full_clean()` çağırdığı için instance'ın TÜM alanları doğrulanır
- Sonuç: `update_fields` listesinde olmayan ama instance'a DB'den negatif yüklenmiş bir alan (örn. legacy `Products.gram`) alakasız bir partial save'i 500 hatasıyla patlatabilir
- Çözüm deseni: meta alan güncellemelerini `Model.objects.filter(id=...).update(...)` ile yap — instance dirty olmaz, `full_clean()` tetiklenmez
- Bkz. ONARIM FAZI 5 (2026-04-27): `update_scrap_pool_weighted_mileage` bu tuzağa düşmüştü

## Legacy Sayaç Alanları için Zemin Koruması (Greatest)

- `Products.gram` gibi raw SQL `update(field=F('field') - x)` ile düşürülen sayaç alanlarına ZEMIN koruması ekle
- Doğru desen: `update(field=Greatest(F('field') - x, Decimal('0')))` — alan asla negatife düşemez
- Import: `from django.db.models.functions import Greatest`
- StockSnapshot.stock_gram DB CheckConstraint ile korunuyor; legacy Products.gram için bu desen tek savunma hattıdır

## Onarım Fazı 5 — Hurda Havuzu full_clean() Çakışması (2026-04-27)

- **BUG D (full_clean() + negatif Products.gram):** `update_scrap_pool_weighted_mileage` fonksiyonu `product.save(update_fields=['product_mileage', 'buy_price_hs', 'sale_price_hs'])` çağırıyordu. `Products.save()` override'ı her durumda `full_clean()` çağırdığı için instance'ın `gram` alanı da kontrol ediliyordu. `_cancel_single_process` ise `Products.objects.filter(...).update(gram=F('gram') - gram)` ile gram'ı korumasız düşürüyordu → çoklu iptal sonrası `Products.gram` veritabanında negatif kalıyordu → bir sonraki hurda girişinde `existing_product` instance'ına negatif `gram` yükleniyordu → `clean()` içindeki `if self.gram < Decimal('0')` koşulu `ValidationError: gram negatif olamaz` fırlatıyordu (500 Internal Server Error). **Fix:** İki katmanlı:
  - **ADIM A (full_clean bypass):** `update_scrap_pool_weighted_mileage` artık `Products.objects.filter(id=product.id).update(product_mileage=..., buy_price_hs=..., sale_price_hs=...)` raw SQL UPDATE kullanıyor. Instance dirty olmadığı için `full_clean()` tetiklenmiyor; `gram` ne olursa olsun bu güncelleme patlamıyor
  - **ADIM B (zemin koruması):** `_cancel_single_process` artık `update(gram=Greatest(F('gram') - gram, Decimal('0')))` kullanıyor. `Products.gram` asla negatife düşemez; gelecekte aynı tuzak yeniden oluşmaz
- **ADIM C (race condition):** `update_scrap_pool_weighted_mileage` içindeki `StockSnapshot.objects.filter(...)` artık `select_for_update()` ile satır kilitli. `scrap_add` zaten `@transaction.atomic` içinde olduğu için aynı havuza eş zamanlı gelen iki giriş aynı `current_gram`'ı okuyamaz, WAC milyem hesabı yarış koşulundan korunur
- **ADIM E (sessiz başarısızlık → log):** `_cancel_single_process` içindeki `except Exception: pass` blokları `logger.error(...)` ile değiştirildi. `cancel_stock_entry` veya `Products.gram` düşürme başarısız olursa Process yine `CANCELED` olur ama operasyonel ekip log'tan görür. Eski davranış muhasebe tutarsızlıklarını görünmez kılıyordu
- **ADIM F (null guard):** `current_gram = (snap.stock_gram if snap and snap.stock_gram else Decimal('0'))` → `Decimal('0') is not None` (`Decimal('0')` falsy olduğu için "snapshot var ama stok=0" durumu yanlış dala düşüyordu). `is not None` ile bu ayrım netleştirildi
- **Etkilenen dosyalar:** `apps/scraps/views.py` (yalnız)
- **Migration gerekmez:** Tüm değişiklikler kod düzeyinde; şema değişikliği yok

## Onarım Fazı 6 — Hurda UAT Düzeltmeleri (2026-04-27)

UAT testlerinde Onarım Fazı 5 sonrası tespit edilen 4 mantık/senkronizasyon hatası kapatıldı. (5. UAT bulgusu — supplier cari yön etiketlemesi — backend tarafında ENTRY=Borç/EXIT=Alacak doğru çalışıyor; UI etiket netleştirmesi ayrı bir görev olarak ertelendi.)

- **BUG 1 (Pasif havuza yeniden giriş):** Tüm havuz `cancel_stock_entry()` ile iptal edildikten sonra `Products.is_active=False` ve/veya `Scraps.is_active=False / is_deleted=True` kalabiliyordu. Aynı ayar sınıfına yeni hurda eklendiğinde havuz pasif görünüyor, listede gizli kalıyordu. **Fix:** `scrap_add` `existing_product` dalında `Scraps.is_deleted` ve `Scraps.is_active` ile `Products.is_active` bayrakları otomatik resetleniyor; Products için `Products.objects.filter(id=...).update(is_active=True)` (atomic, `full_clean()` bypass) kullanılıyor.

- **BUG 2A (Milyem "59" görünümü):** Liste tablosunda `d_fmt(p.product_mileage, 0)` kullanılıyordu; `d_fmt` üst seviyede `.rstrip('0')` yaptığı için 590 → "59", 600 → "6" üretiyordu. **Fix:** `str(int(p.product_mileage)) if p.product_mileage is not None else '0'` — milyem her zaman tam sayı; trailing-zero strip riski elenir.

- **BUG 3 (Update yolu çoklu kaynak ezmesi):** `scrap_add` üst kısmındaki `if scrap_id:` bloğu, havuza birden fazla kaynaktan giriş yapılmış olsa bile `p.gram = gram; p.product_mileage = product_mileage; p.save()` ile tek kalemi güncelleyip ağırlıklı ortalamayı bozuyordu; ayrıca `p.save()` yine `full_clean()` tuzağına açıktı. **Fix:** İki katmanlı koruma:
  - **GUARD:** Aktif `Process(transaction_type='PURCHASE', is_status='COMPLETED')` sayısı > 1 VEYA `Process(transaction_type='SALE', is_status='COMPLETED').exists()` ise update reddedilir (HTTP 409). Kullanıcı "İşlemler > İptal" + yeni giriş akışına yönlendirilir.
  - **SAFE WRITE:** Tek kaynaklı satılmamış havuzlarda `Products.objects.filter(id=p.id).update(name=..., product_mileage=..., buy_price_hs=..., sale_price_hs=...)` kullanılır. `gram` doğrudan ezilmez; `StockService.adjustment` tek otorite olarak kalır. Atomic UPDATE → `full_clean()` tetiklenmez.

- **BUG 4 (Hayalet kayıtlar listede):** "İşlemler > İptal" akışı `Scraps.is_deleted=True` yapmıyor; tamamen iptal edilmiş havuzlar listede stok=0, milyem=0 olarak kalıyordu. **Fix:** Liste sorgusuna `qs.exclude(Q(ever_sold=False) & (Q(inv_stock_gram__lte=0) | Q(inv_stock_gram__isnull=True)))` filtresi eklendi:
  - Hiç satış olmamış + stok yok → HAYALET → gizle
  - Satış geçmişi olan (ever_sold=True) sıfırlanmış kayıtlar → tarihsel bilgi → görünür kalır
  - Aynı havuza yeni giriş gelince stok > 0 olur, BUG 1 reset ile birlikte otomatik yeniden görünür

- **BUG 6 (Stok kalıntısı + WAC milyem kayması):** Tüm hurdalar silindikten sonra yeni hurda eklenince listede önceki stok + yeni stok birleşiyordu (örn: 13g 590 milyem havuz silinmiş + 10g 585 milyem yeni giriş → 23g, 587 milyem). Sebepler: `cancel_stock_entry()` exception fırlatmış olabilir (FAZ 5 / ADIM E `logger.error`'a düşer, stok geride kalır), Process'siz legacy havuzlarda RETURN_OUT yetersiz stok hatasıyla atlanmış olabilir, ya da yalnızca soft-delete yapılmış (stok hareketi hiç tetiklenmemiş). **Fix:** `scrap_add` `existing_product` dalında **revival reset** akışı eklendi:
  - `was_revival = scrap_record.is_deleted OR scrap_record.is_active=False OR existing_product.is_active=False`
  - True ise: `StockSnapshot` `select_for_update` ile okunur; stock_gram > 0 veya stock_pieces > 0 ise `StockService.adjustment(actual_gram=0, actual_pieces=0, ref_id=f"scrap_revival_{...}_{sp_process_no}")` çağrılır → ADJUSTMENT_MINUS audit satırı oluşur, snapshot 0'a çekilir
  - Sonra `Products.objects.filter(id=...).update(gram=Decimal('0'), product_mileage=Decimal('0'))` ile legacy alanlar atomic sıfırlanır (full_clean bypass)
  - `update_scrap_pool_weighted_mileage` artık `current_gram=0` ve `current_mileage=0` görür → ELSE dalına düşer → `result_mileage = new_mileage` olur
  - Audit trail tam korunur (StockLedger append-only); sadece "kullanıcı silme niyeti" semantiği netleşir
  - Adjustment exception olursa `logger.error` düşer; akış devam eder (kullanıcı yine yeni girişini ekleyebilir)

- **Etkilenen dosyalar:** `apps/scraps/views.py` (yalnız)
- **Migration gerekmez:** Tüm değişiklikler kod düzeyinde; şema değişikliği yok
- **Ertelenmiş:** UAT-5 (supplier cari yönü). Backend muhasebesi doğru (Hurda alış = ENTRY = Borç çıkar = bizim borcumuz tedarikçiye); UI'da label netleştirmesi gerekli ise ayrı görevde ele alınacak.

## Onarım Fazı 7 — Çapraz Modül Mimari Çatlağı (2026-04-27)

Onarım Fazı 6 sonrası yapılan çapraz-modül (perakende ↔ toptan) testlerinde iki kritik bulgu tespit edildi:

- **Bulgu 1 — İptal Sonrası WAC Milyem Geri Hesaplanmıyor:** Hurda havuzuna iki ayrı milyem girişi yapıldığında (örn. 10g 585 + 10g 595 → WAC=590) ikinci girişin "İşlemler > İptal" ile geri sarılması durumunda `cancel_stock_entry()` yalnızca `StockLedger` REVERSAL satırı yazıyor, `Products.product_mileage` ve `StockSnapshot.weighted_avg_cost_hs` eski değerinde (590) donup kalıyordu. Beklenen: havuz fiilen 10g 585 milyemde olmalı ve `product_mileage=585`'e düşmeli. **Kök Neden:** `StockService` "WAC çıkışta sabit kalır" prensibiyle çalışır; iptal akışı için ters yönde WAC yeniden hesaplama hiçbir katmana iliştirilmemişti.

- **Bulgu 2 — Toptan-Hurda Senkronizasyon Kopukluğu:** Toptan ekranından hurda eklendiğinde (`add_scrap_to_wholesale_process`, `add_scrap_multi_to_wholesale_process`) Hurda listesinde görünmüyordu. Üç ayrı kök neden:
  1. `find_scrap_pool_by_karat` çağrısında `material_type` parametresi eksikti (FAZ 4 / ADIM 4 kapısı `ValueError` fırlatıyor olabilirdi).
  2. Daha önce silinmiş havuza yeniden giriş yapıldığında `Scraps.is_deleted=True / is_active=False` resetlenmiyor, BUG 1'in toptan eşleniği eksikti.
  3. IN_PROGRESS aşamasında stok 0 + ever_sold=False olduğu için BUG 4 ghost filtresi havuzu listeden gizliyordu — kullanıcı "ekledim ama kayboldu" deneyimini yaşıyordu.

### ADIM 1 — `recalculate_scrap_pool_mileage_after_cancel(product, store)` (Bulgu 1)

`apps/scraps/views.py` içinde yeni utility:
- Aktif (henüz iptal edilmemiş) `StockLedger` IN giriş satırlarını tarar (`PURCHASE`, `INITIAL`, `ADJUSTMENT_PLUS` reason'ları).
- `_cancel` ile biten ref_type'lı OUT satırlarını "iptal edilmiş" olarak işaretler ve eşleştirme ile filtreler.
- Kalan girişlerden `weighted_sum_hs / total_gram` hesaplar; sonucu `ROUND_HALF_UP` ile tam sayı milyeme çevirir.
- Atomic UPDATE ile `Products.product_mileage`, `Products.buy_price_hs`, `Products.sale_price_hs` ve `StockSnapshot.weighted_avg_cost_hs` günceller (full_clean bypass).
- Tüm girişler iptal edilmişse milyem ve WAC 0'a düşer → bir sonraki giriş tek belirleyici olur (FAZ 6 BUG 6 revival semantiği ile uyumlu).

### ADIM 2 — `_cancel_single_process` ref_type Fallback (Bulgu 2 köprüsü)

Toptan modülü stok girişlerini `update_product_stock` ile `ref_type='process'` altında yazar; perakende `scrap_add` `ref_type='scrap_add'` kullanır. `cancel_stock_entry()` tek bir `ref_type` ile çağrıldığında diğer modülün ürettiği satırlar bulunamıyor, REVERSAL atılmıyor, stok geri sarılmıyordu. **Fix:** `('scrap_add', 'process')` sırasıyla denenen fallback döngüsü; herhangi bir denemede `cancelled_stock_count > 0` veya `supplier_ledger_reversals` yazıldıysa döngü kırılır. Hiçbiri sonuç vermezse `logger.warning` ile uyarı atılır.

### ADIM 2b — `_cancel_single_process` içinde `recalculate_scrap_pool_mileage_after_cancel` çağrısı

Process `CANCELED` save'inden sonra try/except ile çağrılır. Hata olursa `logger.error` düşer; iptal akışı bütünlüğü korunur (kullanıcı zaten stok geri sarıldığını görmüştür).

### ADIM 3 — Toptan: Scraps Reset + Revival Reset + material_type (Bulgu 2)

`apps/process/wholesale_views.py` içinde **iki view** güncellendi:
- `add_scrap_to_wholesale_process` (tekli)
- `add_scrap_multi_to_wholesale_process` (çoklu satır)

Her ikisinde de aşağıdaki adımlar uygulanır:
1. **`material_type='GOLD'` explicit:** `find_scrap_pool_by_karat(..., material_type='GOLD')` ile çağrılır. FAZ 4 / ADIM 4 strict izolasyon kapısı sessizce GOLD'a düşmez; eksik parametre `ValueError` üretirdi.
2. **Yeni havuz oluşturulurken `material_type='GOLD'` set edilir** (FAZ 3 / ADIM 2 kuralı: havuz `material_type` etiketi olmadan oluşursa sonraki aramalar bulamaz).
3. **`Scraps.objects.get_or_create()` + flag reset:** Mevcut havuza giriş yapıldığında Scraps satırı varsa `is_deleted=False`, `is_active=True` resetlenir; satır hiç yoksa oluşturulur. `Products.is_active=False` ise atomic UPDATE ile aktifleştirilir.
4. **Revival Reset (BUG 6 toptan eşleniği):** Reset edilen flag varsa havuz "yeniden açılış" sayılır. `StockSnapshot.stock_gram > 0` veya `stock_pieces > 0` ise `StockService.adjustment(actual_gram=0, actual_pieces=0, ref_id=f"wholesale_scrap_revival_{product.id}_{process_no}[_idx]")` çağrılır → ADJUSTMENT_MINUS audit satırı oluşur. Sonra `Products.gram` ve `product_mileage` atomic UPDATE ile sıfırlanır. Böylece `complete_process_wholesale` aşamasında gelen yeni hurda WAC için tek belirleyici olur.

### ADIM 4 — Ghost Filter IN_PROGRESS Muafiyeti (Bulgu 2 görünürlük)

`get_all` listesinde:
- `has_in_progress = Exists(Process.filter(product=p, store=s, transaction_type='PURCHASE', is_status='IN_PROGRESS', is_deleted=False))` annotate edildi.
- Ghost filter güncellendi:
  ```python
  qs.exclude(Q(ever_sold=False) & Q(has_in_progress=False) &
             (Q(inv_stock_gram__lte=0) | Q(inv_stock_gram__isnull=True)))
  ```
- Sonuç: Toptan ekranından eklenmiş ama henüz `complete_process_wholesale` çağrılmamış IN_PROGRESS havuzlar Hurda listesinde GÖRÜNÜR; Process tamamlandıktan sonra normal stok > 0 ile zaten görünmeye devam eder.

### Etkilenen Dosyalar
- `apps/scraps/views.py` — yeni utility + `_cancel_single_process` ref_type fallback + recalculate çağrısı + ghost filter IN_PROGRESS muafiyeti
- `apps/process/wholesale_views.py` — `add_scrap_to_wholesale_process` ve `add_scrap_multi_to_wholesale_process` Scraps reset + revival reset + material_type

### Migration Gerekmez
Tüm değişiklikler kod düzeyinde; şema değişikliği yok. `StockLedger` append-only mimarisi sayesinde audit trail tam korunur.

### Notlar
- `recalculate_scrap_pool_mileage_after_cancel` "WAC çıkışta sabit kalır" ilkesini KIRMAZ; yalnızca **iptal akışında** geri sarma için çağrılır. Normal satış/çıkış akışında WAC dokunulmaz kalır.
- ref_type fallback sırası `('scrap_add', 'process')` olarak seçildi: perakende kaynaklı kayıtlar baskındır, toptan-doğan kayıtlar fallback olarak işlenir. Sıra geriye dönük uyumluluğu garanti eder.
- Toptan tarafında `transaction.atomic` çoğu yerde request-scope'tadır; ek wrapping eklenmedi (mevcut atomicity yeterli).

## Onarım Fazı 8 — `d_fmt` Yapısal Kesim Hatası (2026-04-27)

Onarım Fazı 7 sonrası toptan UAT'ında tespit edilen kritik biçimlendirme bulgusu: 10g 585 milyem + 10g 595 milyem girişinde matematik doğru (WAC=590, DB'de `product_mileage=590`) çalışmasına rağmen Hurda listesi MİLYEM sütunu **"59"**, MALİYET (HS) **"0.59"** gösteriyor; aynı havuzun modal başlığı ise **"590 milyem"** yazıyordu. FAZ 6 / BUG 2A milyem için noktasal düzeltmeyle (`str(int(...))`) maskelenmiş, ancak başka tüm kullanım noktalarında (HS, gram, sale_price_hs vb.) aynı tehlike sürüyordu.

### Kök Neden — `d_fmt` Üst Seviye `rstrip('0')`
`apps/scraps/views.py` ve `apps/bracelets/views.py` içindeki ikiz `d_fmt` fonksiyonunun gövdesi:

```python
v = d_quantize(Decimal(val), max_places)
s = f"{v:f}".rstrip('0').rstrip('.')
```

`max_places=0` veya tam-sayı Decimal'lerde `f"{Decimal('590'):f}"` çıktısı **ondalık nokta içermez** ("590"). Buna rağmen `rstrip('0')` koşulsuz çalıştığı için tam sayı kısmındaki sondaki sıfırlar da kırpılıyordu:

| Girdi | `f"{v:f}"` | Eski sonuç (BUG) | Beklenen |
|-------|-----------|------------------|----------|
| `Decimal('590')` | `"590"` | `"59"` | `"590"` |
| `Decimal('600')` | `"600"` | `"6"` | `"600"` |
| `Decimal('1.230')` | `"1.230"` | `"1.23"` | `"1.23"` |
| `Decimal('5.850')` | `"5.850"` | `"5.85"` | `"5.85"` |

`rstrip('0').rstrip('.')` zinciri yalnızca ondalık kısımdan sıfır atmak için tasarlanmıştı; kontrolsüz uygulandığı için tam sayılarda yan etki üretiyordu. FAZ 6 / BUG 2A noktasal yamayla (yalnız milyem kolonu) bu yan etkiyi kapamıştı; `buy_price_hs` `Decimal('0.590')` için `f` çıktısı `"0.590"` olduğundan o kolon BUG'dan etkilenmiyor — ancak `f` precision'ı veya quantize ölçeği değişirse yine kırılabilirdi.

### Liste vs Modal Tutarsızlığının Kaynağı
- **Liste (`get_all`):** FAZ 6 BUG 2A fix sonrası `str(int(p.product_mileage))` kullanıyor → "590" döner. Eğer kod sürümü güncel değilse (sunucu yeniden başlatılmamışsa) eski `d_fmt(p.product_mileage, 0)` çağrısı 590 → "59" üretiyordu. UAT'taki "59" görünümü tam olarak bu ön-FAZ-6 yolunun yan etkisidir.
- **Modal (`get_pool_contents`):** Doğrudan `int(p.product_mileage or 0)` kullanır → 590 döner. Bu yüzden modal "590 milyem" doğru gösterirken liste eski yola düşmüş veriyle "59" gösteriyordu.

### Düzeltme — `d_fmt` Koşullu Trim
Her iki dosyada da `d_fmt` artık `rstrip('0')` çağrısını YALNIZCA `s` içinde ondalık nokta varsa uygular:

```python
def d_fmt(val: Decimal, max_places: int = 6) -> str:
    if val is None:
        return ""
    v = d_quantize(Decimal(val), max_places)
    s = f"{v:f}"
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or "0"
```

Bu düzeltme:
- `Decimal('590')` → `"590"` (tam sayı kısmı korunur)
- `Decimal('1.230')` → `"1.23"` (ondalık sıfırlar atılır — eski davranış korunur)
- `Decimal('0.500')` → `"0.5"` (ondalık sıfırlar atılır)
- `Decimal('100')` → `"100"` (eski sürümde `"1"` olurdu — kritik fix)

`bracelets/views.py:543` `d_fmt(p.product_mileage, 0)` çağrısı yerinde bırakıldı; düzeltilmiş `d_fmt` ile artık doğru "590" üretir. `scraps/views.py` içindeki FAZ 6 BUG 2A `str(int(...))` yaması da yerinde kaldı (defansif redundancy: iki katmanlı koruma).

### Etkilenen Dosyalar
- `apps/scraps/views.py` — `d_fmt` koşullu `.` kontrolü eklendi
- `apps/bracelets/views.py` — `d_fmt` koşullu `.` kontrolü eklendi (ikiz tanım)

### Migration Gerekmez
Yalnızca render katmanı; veritabanı ve audit trail dokunulmadı. WAC matematiği zaten doğru çalışıyordu (590 olarak DB'ye yazılıyor); yalnız sunum kırpması düzeltildi.

### Operasyonel Not
Bu bug'ın UAT'ta görülmesinin tek koşulu **sunucunun FAZ 6 BUG 2A sonrası yeniden başlatılmamış olması** veya **`d_fmt` içeren başka bir render yolunun (örn. bilezik listesi) milyem benzeri tam-sayı Decimal göstermesi** idi. FAZ 8 ile kök neden çıkarıldığı için artık sunucu restart sırasına bakılmaksızın güvende.

### Notlar
- `d_fmt` semantik kontratı korundu: gereksiz ondalık sıfırlar atılır, anlam değiştiren tam-sayı sıfırları korunur.
- Tek bir helper'da düzeltme yapmak yerine her iki kopyayı da düzelttik; kod duplication ayrı bir refactor faz konusu.
- Sürüm regresyonunu önlemek için ilerideki `d_fmt` çağrılarında **tam sayı ifade eden alanlar için `str(int(...))`** kullanmak hâlâ önerilir (defansif).

## Onarım Fazı 9 — Hurda Havuz Birleştirme: Kullanıcı Seçimli Ayar Anahtarı (2026-04-27)

Onarım Fazı 8 sonrası toptan UAT'ında "Pool Merging" hatası tespit edildi: 10g 14 Ayar (595 milyem) + 10g 14 Ayar (605 milyem) girişlerinde sistem iki ayrı "14 Ayar" Products kaydı oluşturdu, birleştirme yapmadı. Beklenen davranış: tek havuzda 20g 600 milyem.

### Kök Neden — `karat_from_mileage` ROUND_HALF_UP Sınır Kayması
`apps/scraps/views.py` içindeki `karat_from_mileage(605)` formülü:

```python
int((605/1000 * 24).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
# = int((14.52).quantize(1, ROUND_HALF_UP))
# = int(15)  ← 14 değil!
```

| Milyem | m/1000×24 | ROUND_HALF_UP | Eski karat |
|--------|-----------|---------------|------------|
| 595    | 14.28     | 14            | 14 ✓       |
| 604    | 14.496    | 14            | 14 ✓       |
| **605** | **14.52** | **15**       | 15 ✗       |
| 624    | 14.976    | 15            | 15 ✗       |

`find_scrap_pool_by_karat` ikinci girişte `target_karat=15` ile arama yapıyor; 14 karatlı ilk havuz eşleşmiyor → yeni Products kaydı açılıyor. Aynı kusur 600 milyem üstündeki tüm "14 ayar" girişlerinde tetikleniyordu.

### Tasarım Değişikliği — Pool Anahtarı: Kullanıcının Seçtiği Ayar
Çözüm: havuz anahtarı artık **kullanıcının formdan seçtiği ayar adı** (ör. `scrap_name="14 Ayar"`). Milyem değerinden TÜRETİLMEZ. Kullanıcı "14 Ayar" seçer, milyem 595 / 605 / 995 girse de hepsi aynı "14 Ayar" havuzunda toplanır; milyem havuz seviyesinde ağırlıklı ortalama ile takip edilir.

### ADIM 1 — `karat_from_mileage` FLOOR Semantiğine Geçiş

`apps/scraps/views.py:80`:

```python
# ÖNCE
return int((m / Decimal('1000') * Decimal('24')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
# SONRA
return int(m / Decimal('1000') * Decimal('24'))   # floor
```

Sonuç: 14.04 → 14, 14.28 → 14, 14.52 → 14, 14.99 → 14, 15.00 → 15. Kuyumcu konvansiyonunun "taban" mantığıyla tam uyum (14 ayar = [560, 624] milyem aralığı).

Bu fonksiyon artık yalnız fallback amaçlı; ana havuzlama mekanizması yeni `extract_scrap_karat_label` üzerinden çalışır.

### ADIM 2 — Yeni Helper'lar

`apps/scraps/views.py`:
- `SCRAP_KARAT_LABELS = {8: '8 Ayar', 10: '10 Ayar', 14: '14 Ayar', 18: '18 Ayar', 21: '21 Ayar', 22: '22 Ayar', 24: '24 Ayar'}`
- `extract_scrap_karat_label(scrap_name=None, fallback_mileage=None, material_type='GOLD')`:
  - `scrap_name` "X Ayar" kalıbına uyuyorsa canonical etiket döner ("14 Ayar")
  - Standart kalıba uymayan özel isim (örn. "Eski Yüzük Hurdası") → kullanıcı yazımı stripped haliyle döner; kendi havuzuna gider
  - `scrap_name` boşsa `fallback_mileage`'tan karat hesaplar (floor) ve canonical etiket türetir
- `find_scrap_pool_by_selected_karat(store, category, scrap_name, fallback_mileage, is_scrap, material_type)`:
  - `extract_scrap_karat_label` ile canonical anahtar üretir
  - `Products.objects.filter(store, category, is_scrap, is_deleted=False, material_type, name__iexact=karat_label)` — tek havuz döner
- `find_scrap_pool_by_karat` DEPRECATED wrapper olarak kaldı; yeni fonksiyona delegate eder (geriye dönük uyum)

### ADIM 3 — `scrap_add` (Perakende) Güncellemesi
- Form'dan gelen `scrap_name` (`raw_scrap_name`) korunur
- `canonical_karat_label = extract_scrap_karat_label(...)` çıkarılır
- Yeni Products kaydının `name` alanı canonical etiket olur (önceki "X Milyem Hurda" fallback'i sadece ayar etiketi çıkarılamadığında çalışır)
- Havuz arama `find_scrap_pool_by_selected_karat` ile yapılır

### ADIM 4 — Toptan Modülü Güncellemesi (`apps/process/wholesale_views.py`)

`add_scrap_to_wholesale_process` ve `add_scrap_multi_to_wholesale_process`:
- `find_scrap_pool_by_karat` çağrıları `find_scrap_pool_by_selected_karat`'e geçti
- Yeni havuz oluştururken `Products.name = canonical_karat_label` (ör. "14 Ayar")
- **Revival koşulu daraltıldı:** `was_revival = (is_deleted OR not is_active OR not product.is_active)` — `_scrap_reset_fields` kaldırıldı (taze açılan Scraps satırı revival sayılmaz)
- **Revival reset legacy alan sıfırlama:** `Products.gram=0, product_mileage=0` artık YALNIZCA `_stale_gram > 0 OR _stale_pieces > 0` ise çalışır. Stok 0 ise alanlar dokunulmaz; canonical name + product_mileage tutarlı kalır

### ADIM 5 — Duplicate Havuz Birleştirme Yardımcısı

`merge_scrap_pool_duplicates(store, category=None, material_type='GOLD')`:
- Aktif scrap havuzlarını canonical karat etiketine göre gruplar
- Her grupta birden fazla havuz varsa en eskisi PRIMARY, diğerleri DUPLICATES
- Her duplicate için atomic transaction içinde:
  1. `Process.product` PRIMARY'ye taşınır
  2. `StockLedger.product` PRIMARY'ye taşınır (audit trail KORUNUR; yalnız FK yeniden hedeflenir)
  3. `StockSnapshot` PRIMARY'ye birleştirilir (gram toplamı + WAC ağırlıklı ortalama); duplicate snapshot silinir
  4. `Scraps` satırı PRIMARY'ye taşınır veya soft-delete edilir
  5. Duplicate `Products` `is_deleted=True, is_active=False` olur
- Sonunda `recalculate_scrap_pool_mileage_after_cancel(primary)` ile primary'nin meta alanları StockLedger üzerinden tutarlı hale getirilir

HTTP endpoint: `POST /scraps/merge-duplicates` (role: `SCRAPS_SCRAP_ADD`). Body: `{material_type: 'GOLD'|'SILVER'}`. Response: `{merged_groups, merged_pools, details: [...]}`.

UAT'ta oluşmuş duplicate'leri tek operasyonel tetikle temizler.

### Etkilenen Dosyalar
- `apps/scraps/views.py` — `karat_from_mileage` floor; `SCRAP_KARAT_LABELS`; `extract_scrap_karat_label`; `find_scrap_pool_by_selected_karat` (yeni); `find_scrap_pool_by_karat` deprecated wrapper; `_canonical_pool_key`; `merge_scrap_pool_duplicates`; `merge_scrap_duplicates_view`; `scrap_add` yeni finder + canonical name
- `apps/process/wholesale_views.py` — `add_scrap_to_wholesale_process` ve `add_scrap_multi_to_wholesale_process` yeni finder; canonical name; revival koşulu daraltıldı
- `apps/scraps/urls.py` — `merge-duplicates` endpoint'i

### Migration Gerekmez
Şema değişmedi. `Products.name` zaten "X Ayar" değerlerini saklayabilen `CharField(max_length=255)`. Mevcut duplicate veriler `merge_scrap_duplicates_view` ile temizlenir.

### Operasyonel Notlar
- UAT temizliği: yönetim ekranında "Duplicate Havuzları Birleştir" butonu eklenebilir (frontend ayrı görev). Şimdilik endpoint manuel çağrılır.
- "WAC çıkışta sabit" prensibi korunur; iptal sonrası `recalculate_scrap_pool_mileage_after_cancel` kuralı değişmedi.
- `name__iexact` filtre case-insensitive eşleşme yapar; kullanıcı "14 ayar" yazsa bile "14 Ayar" havuzunu bulur. Yeni kayıtlar her zaman canonical formda yazılır.

---

## ONARIM FAZI 10 — Bulgu 5: SupplierLedger İptal Sonrası Hayalet Bakiye

**Tarih:** 2026-04-27
**Tetikleyen Bulgu (UAT):**
3 toptan hurda alımı (12.00 + 5.95 + 5.85 = 23.80 HS) kaydedildi → "İşlemler" ekranından üçü de iptal edildi.
- Hurda Stok listesi: boş ✓
- Tedarikçi "İşlem Geçmişi": boş ✓ (Process tablosu `is_deleted=True`)
- Tedarikçi "Finansal Durum & Bakiyeler": **23.80 HS ALACAKLISINIZ ✗**

### Kök Neden Analizi

`apps/scraps/views.py::_cancel_single_process` → `cancel_stock_entry` (apps/stock_management/services/cancel_service.py) çağrıyordu. Eski akış SupplierLedger tarafında iki şey yapıyordu:

1. **Yeni reversal satır:** Ters `transaction_type` (ENTRY→EXIT), aynı `amount_value`, `process_no=f"{orig}_CANCEL"`, **`is_active=True`**.
2. **Orijinal pasif:** `is_active=False` (soft disable).

`Suppliers.balance_summary()` yalnızca `is_active=True` kayıtları toplar:
```
receivable += reversal (EXIT, aktif)   = +23.80
payable    += orijinal (ENTRY, pasif)  =   0.00
─────────────────────────────────────────────
net                                    = +23.80 → "ALACAKLISINIZ" görünür
```

Eski koddaki yorum *"reversal + deactivated original == net sıfır"* matematiksel olarak hatalıydı: pasif kayıt `balance_summary`'ye katkı vermez; aktif reversal kalır → **tek-yönlü hayalet bakiye**.

Ek olarak `_cancel_single_process` davranışı `apps/process/operations.py::cancel_row` (toptan/genel "İşlemler" iptali) ile **tutarsızdı**: `cancel_row` yıllardır `update(is_active=False)` ile sadece pasifleştiriyor, reversal yazmıyor. Aynı kullanıcı eyleminin iki farklı kod yolundan geçmesi iki farklı sonuç doğuruyordu.

### Çözüm — Seçenek A: Reversal Üretmeyi Bırak

`cancel_stock_entry` artık SupplierLedger tarafında **yalnızca soft-disable** uygular. Reversal satır oluşturma bloğu tümüyle kaldırıldı. Davranış `cancel_row` ile birebir tutarlı:

```python
# Yeni davranış (sadeleşmiş)
if reverse_supplier_ledger:
    pks = list(SupplierLedger.objects
               .filter(process_no=str(ref_id), is_active=True)
               .values_list('pk', flat=True))
    if pks:
        SupplierLedger.objects.filter(pk__in=pks).update(is_active=False)
        result['deactivated_supplier_ledgers'] = len(pks)
```

`balance_summary()` sonucu: `receivable=0, payable=0, net=0` → UI "Bakiye yok" gösterir.

### Etkilenen Dosyalar

- `apps/stock_management/services/cancel_service.py`
  - Modül docstring güncellendi (Adım 2 → "soft-disable", reversal yok)
  - Fonksiyon docstring `reverse_supplier_ledger` parametresi yeniden tanımlandı
  - Lazy import'lardan `get_ledger_currency` kaldırıldı (artık kullanılmıyor)
  - Reversal satır oluşturma bloğu (`SupplierLedger.objects.create(...)` + currency seçimi + frozen rate) tümüyle silindi
  - Sadece `update(is_active=False)` kalır
  - `result['supplier_ledger_reversals']` daima `[]` döner (geri uyumluluk)
  - `logger.info` çıktısından `sl_reversals` alanı kaldırıldı

- `apps/scraps/views.py::_cancel_single_process`
  - Retry break sinyali `len(supplier_ledger_reversals) > 0` → `deactivated_supplier_ledgers > 0` olarak değiştirildi (reversal listesi artık daima boş; doğru sinyal pasifleşme sayısıdır)
  - "Hiçbir kayıt bulunamadı" warning kontrolü aynı şekilde güncellendi

### Audit Trail

- Orijinal SupplierLedger kaydı `is_active=False` ile DB'de duruyor (silinmedi); iptal sebebi/zamanı:
  - `Process.is_deleted=True` + `Process.transaction_type='CANCELED'`
  - StockLedger reversal satırlarındaki `IPTAL: ...` notu
  - StockLedger orijinal `created_on` ↔ reversal `created_on` farkı

### Geri Uyumluluk

- `cancel_stock_entry` dönüş sözlüğünün **anahtar şeması korundu**. `supplier_ledger_reversals` listesi mevcut ama daima boş; doğrudan listeye iterate eden tüm callers (yalnızca scraps/views.py) güncellendi.
- `cancel_row` davranışı zaten doğruydu — değişiklik yok.

### Test Beklentileri

- ✅ Toptan hurda alımı + İşlemler ekranından iptal → tedarikçi cari bakiye 0
- ✅ Toptan barkodlu alım + iptal → tedarikçi cari bakiye 0 (zaten doğruydu, regresyon yok)
- ✅ Hurda satış (EXIT) + iptal → bakiye 0
- ✅ Aynı tedarikçi içinde 2+ farklı işlem → biri iptal, diğer aktif tek başına bakiyede görünür
- ✅ "İşlem Geçmişi" listesi: iptal edilen satırlar zaten görünmüyor (Process.is_deleted) — değişmedi

### Migration Gerekmez

Şema değişmedi. Mevcut DB'de takılı kalmış `_CANCEL` reversal satırları (Bulgu 5 öncesi iptal edilmiş işlemlerden) UAT temizliği için elle deaktive edilebilir:
```python
# Operasyonel temizlik (gerekirse Django shell)
SupplierLedger.objects.filter(
    process_no__endswith='_CANCEL', is_active=True
).update(is_active=False)
```
Bu komut sonrası geçmiş hayalet bakiyeler de düzelir.
- Özel isimli scrap kayıtları (ör. "Müşteri X'in eski yüzükleri") canonical kalıba uymadığı için kendi havuzunda kalır (kasıtlı izolasyon).

---

## BİLEZİK ONARIM FAZI — Hurda Mimarisinin Bilezik Modülüne Taşınması (2026-04-27)

Hurda Onarım Fazları 4-10 ile kazanılan tüm mimari standartlar (havuz birleştirme, append-only audit, atomic UPDATE bypass, revival reset, ghost filter, IN_PROGRESS muafiyeti, cancel_stock_entry tek otorite) Bilezik modülüne tek bir konsolide refactor ile taşındı.

### Tetikleyen UAT Bulgusu

Aynı bilezik adı altında farklı milyem girişlerinde sistem ayrı havuzlar oluşturuyordu:
- 10g 916 milyem "Burma" → Pool A
- 10g 925 milyem "Burma" → Pool B (BEKLENMEYEN)

Beklenen davranış: tek "Burma" havuzunda 20g, WAC milyem = (10×916 + 10×925) / 20 = 920.5 ≈ 921.

### Tasarım Kararı — Bilezik Havuz Anahtarı: AD

Hurda havuzları **karat + material_type** ile gruplanır; bilezik havuzları **isim** (case-insensitive) ile gruplanır. Aynı isimli ("Burma", "Ajda", "Hasır") farklı milyem girişleri tek havuzda WAC milyem ile birikir. Milyem havuz seviyesinde ağırlıklı ortalamadır, anahtar değildir.

### B-FAZ 1 — Havuz Anahtarı: `find_bracelet_pool_by_name`

`apps/bracelets/views.py` içinde:
- `find_bracelet_pool_by_name(store, category, name)` — case-insensitive `name__iexact` ile arama; `is_deleted=False`, `is_active=True` filtreleri uygulanır
- Eski `find_bracelet_pool_by_name_milyem(...)` DEPRECATED wrapper olarak yeni fonksiyona delegate eder (geriye dönük uyum)
- Hurdadan farklı: hiçbir milyem türetme yok, anahtar doğrudan kullanıcının yazdığı isim

### B-FAZ 2 — Benzersiz `bp_process_no` + cancel_stock_entry + Greatest

- Her bilezik girişi için `bp_process_no = generate_process_no()` üretilir (tedarikçili VE tedarikçisiz dahil); StockLedger.ref_id = SupplierLedger.process_no = Process.process_no = bp_process_no
- Eski hatalı desen `ref_id=f"bracelet_{p.id}"` kaldırıldı — havuza eklenen tüm girişler aynı ref_id paylaşıyordu, tek iptal zincirleme tüm havuzu hedef alıyordu
- `_cancel_bracelet_process(*, proc, product, store, user)` artık `cancel_stock_entry()` çağırır; `ref_type` fallback sırası `('bracelet_add', 'process')` ile hem perakende hem toptan kaynaklı satırları yakalar
- `Products.gram` düşürmesinde `Greatest(F('gram') - gram, Decimal('0'))` zemin koruması — legacy alan negatife düşemez (Hurda FAZ 5 / ADIM B deseni)

### B-FAZ 3 — `update_bracelet_pool_weighted_mileage` + full_clean Bypass

- Yeni fonksiyon: `update_bracelet_pool_weighted_mileage(product, store, new_gram, new_mileage)`. `StockSnapshot` `select_for_update()` ile kilitlenir; mevcut `current_gram` ve mevcut `product_mileage` üzerinden ağırlıklı ortalama hesaplanır
- Atomic UPDATE: `Products.objects.filter(id=...).update(product_mileage=..., buy_price_hs=..., sale_price_hs=...)`. Instance dirty olmaz → `Products.save()` override'ındaki `full_clean()` tetiklenmez → `Products.gram` instance'ında negatif yüklenmiş olsa bile alakasız partial save patlamaz (Hurda FAZ 5 / ADIM A deseni)
- `bracelet_add` update yolu (`if scrap_id:` benzeri dal) artık `p.save()` yerine atomic UPDATE kullanır. Çoklu kaynak guard: aktif `Process(transaction_type='PURCHASE', is_status='COMPLETED')` sayısı > 1 VEYA satış geçmişi varsa HTTP 409 `MULTI_SOURCE_POOL` ile reddedilir; kullanıcı "İşlemler > İptal" + yeni giriş akışına yönlendirilir (Hurda FAZ 6 / BUG 3 deseni)

### B-FAZ 4 — `recalculate_bracelet_pool_mileage_after_cancel`

- İptal sonrası WAC geri hesaplama: aktif (henüz iptal edilmemiş) `StockLedger` IN girişlerini tarar (`PURCHASE`, `INITIAL`, `ADJUSTMENT_PLUS`); `_cancel` ile biten OUT satırlarını eşleştirip iptal edilmişleri filtreler; kalanlardan ağırlıklı ortalama ile yeni `product_mileage` hesaplar (`ROUND_HALF_UP` tam sayı)
- Atomic UPDATE ile `Products.product_mileage / buy_price_hs / sale_price_hs` ve `StockSnapshot.weighted_avg_cost_hs` günceller (full_clean bypass)
- `_cancel_bracelet_process` Process `CANCELED` save'inden sonra try/except ile bu fonksiyonu çağırır; hata olursa `logger.error` düşer, iptal akışı bütünlüğü korunur
- Tüm girişler iptal edilmişse milyem 0'a düşer → bir sonraki giriş tek belirleyici olur (revival reset semantiği ile uyumlu)

### B-FAZ 5 — Toptan Modülü Senkronizasyonu (`apps/process/wholesale_views.py`)

`add_bracelet_to_wholesale_process` view'ı **havuz birleştirme** mimarisine geçirildi:
- Lazy import ile `apps.bracelets.views.find_bracelet_pool_by_name` çağrılır (circular dependency'den kaçınmak için)
- Önce aktif havuz aranır; yoksa pasif/silinmiş candidate (`is_deleted=True OR is_active=False`) taranır
- Hiçbiri bulunmazsa yeni `Products` kaydı açılır (eski davranışın fallback'i)
- Mevcut havuza yeniden giriş: `Bracelets.objects.get_or_create()` + `is_deleted=False`, `is_active=True`, `Products.is_active=True` reset
- **Revival reset:** flag reset varsa VE `StockSnapshot.stock_gram > 0` veya `stock_pieces > 0` ise `StockService.adjustment(actual_gram=0, actual_pieces=0, ref_id=f"wholesale_bracelet_revival_{...}")` çağrılır → ADJUSTMENT_MINUS audit satırı; ardından `Products.gram=0, product_mileage=0` atomic UPDATE (Hurda FAZ 7 / ADIM 3 deseni)
- IN_PROGRESS Process satırı önceki gibi oluşur (henüz `complete_process_wholesale` çağrılmadı)
- Q import'u `apps/process/wholesale_views.py` üst kısmına eklendi: `from django.db.models import IntegerField, Sum, DecimalField, F, Q`

`complete_process_wholesale` içinde, scrap WAC bloğundan sonra **bilezik WAC bloğu** eklendi:
```python
elif (p.product and mv == 'ENTRY' and Decimal(str(p.gram or 0)) > 0
        and not bool(getattr(p.product, 'is_scrap', False))):
    _is_bracelet = Bracelets.objects.filter(
        product=p.product, store=request.user.store,
    ).exists()
    if _is_bracelet:
        try:
            from apps.bracelets.views import update_bracelet_pool_weighted_mileage
            _new_mileage_raw = p.process_mileage or p.product.product_mileage or 0
            update_bracelet_pool_weighted_mileage(
                product=p.product, store=request.user.store,
                new_gram=Decimal(str(p.gram)),
                new_mileage=Decimal(str(_new_mileage_raw)),
            )
        except Exception:
            pass
```

Toptan tamamlama sırasında bilezik girişlerinin WAC milyem güncellenmesi `update_product_stock`'tan sonra otomatik tetiklenir; perakende `bracelet_add` ile aynı semantik garanti edilir.

### B-FAZ 6 — Ghost Filter + IN_PROGRESS Muafiyeti

`bracelets/views.py::get_all`:
- `has_in_progress = Exists(Process.filter(product=OuterRef('pk'), store=store, transaction_type='PURCHASE', is_status='IN_PROGRESS', is_deleted=False))` annotate edildi
- Liste sorgusuna ghost filter eklendi:
  ```python
  qs.exclude(Q(ever_sold=False) & Q(has_in_progress=False) &
             (Q(inv_stock_weight__lte=0) | Q(inv_stock_weight__isnull=True)))
  ```
- Hiç satış olmamış + stok yok + IN_PROGRESS yok → HAYALET → gizle
- Toptan ekranından eklenmiş ama henüz tamamlanmamış IN_PROGRESS havuzlar Bilezik listesinde GÖRÜNÜR kalır (Hurda FAZ 7 / ADIM 4 eşleniği)
- Milyem render'ı defansif: `str(int(p.product_mileage)) if p.product_mileage is not None else '0'` (FAZ 6 BUG 2A + FAZ 8 d_fmt çift katmanlı koruma)

### Etkilenen Dosyalar
- `apps/bracelets/views.py` — KOMPLE REWRITE (B-Faz 1, 2, 3, 4, 6)
- `apps/process/wholesale_views.py` — `add_bracelet_to_wholesale_process` rewrite + `complete_process_wholesale` bilezik WAC bloğu (B-Faz 5) + Q import

### Migration Gerekmez
Tüm değişiklikler kod düzeyinde; şema değişikliği yok. `StockLedger` append-only mimarisi sayesinde audit trail tam korunur. UAT'ta oluşmuş duplicate bilezik havuzları için `merge_scrap_pool_duplicates` benzeri bir bilezik birleştirme yardımcısı ileride ayrı görev olarak değerlendirilebilir.

### Notlar
- Bilezik havuzu **isim** ile birleşir; hurda havuzu **karat + material_type** ile birleşir. İki modül paralel arketipleri farklı anahtarla uygular; ortak alt yapı `cancel_stock_entry`, `StockService`, `Greatest`, full_clean bypass desenleridir
- Bilezik kategori bazlı havuz da gerekiyorsa (örn. "Erkek Burma" vs "Kadın Burma"), `find_bracelet_pool_by_name` `category` parametresi zaten taşır; çağıran view `category` filtresini set edebilir
- "WAC çıkışta sabit" prensibi bilezikte de geçerli; `recalculate_bracelet_pool_mileage_after_cancel` yalnız iptal akışında çağrılır

---

## UAT BULGULARI 1 & 2 — Hayalet Filtre Açığı + Multi-Source Pool UX (2026-04-27)

Bilezik B-Faz 1-6 sonrası UAT testlerinde iki ek bulgu tespit edildi. Düzeltmeler hem bilezik hem hurda modüllerine paralel uygulandı (her iki modülde de aynı `get_all` mimarisi var).

### UAT BULGU 1 — Hayalet Filtre Açığı: İptal Edilmiş Satış `ever_sold` Üretiyor

**Tetikleyen senaryo:** Bilezik havuzunda 0 gram kayıt + iptal edilmiş satış geçmişi → `ever_sold=True` üretiyor → ghost filter `Q(ever_sold=False)` koşuluna takılmıyor → kayıt listede "0,000 gr" olarak görünmeye devam ediyor.

**Kök neden:** `bracelets/views.py::get_all` ve `scraps/views.py::get_all` içindeki `ever_sold_q` ve `last_sale_sq` subquery'leri `is_deleted=False` filtresini eksik bırakıyordu:
```python
ever_sold_q = Process.objects.filter(
    transaction_type='SALE',
    is_status='COMPLETED',
    # ← is_deleted=False EKSİK
)
```
Sonuç: bir satış yapılıp sonra iptal edildiğinde (`Process.is_deleted=True`) bile `Exists(ever_sold_q)` hâlâ True dönüyor; ghost filter kayda dokunamıyor.

**Düzeltme:** Her iki subquery'ye `is_deleted=False` eklendi. Cancelled satış artık `ever_sold` sinyalini tetiklemiyor; tüm satışları iptal edilmiş + 0 stoklu havuzlar listeden gizlenir. Aynı düzeltme `last_sale_sq`'a da uygulandı (Detay linki üretirken silinmiş satış process_no'su seçilmesin diye).

**Etkilenen dosyalar:**
- `apps/bracelets/views.py` (`get_all` içinde 2 subquery)
- `apps/scraps/views.py` (`get_all` içinde 2 subquery — Hurda Onarım Fazı 6 BUG 4'ün gizli kalmış uzantısı)

### UAT BULGU 2 — 409 Conflict Yerine Multi-Source Pool UX

**Tetikleyen senaryo:** Çoklu kaynaklı (havuzlanmış) bilezik/hurda kaydının kalem ikonuna tıklayınca backend `MULTI_SOURCE_POOL` HTTP 409 döndürüyor. Frontend bunu çirkin "Error: 409 Conflict" alert'i ile gösteriyordu.

**Tasarım kararı — Seçenek A (kalem ikonunu gizle) + Seçenek B (defansif Swal):** İki katmanlı koruma:
1. **Birincil koruma:** Çoklu kaynaklı havuzlarda kalem ikonu hiç render edilmez. Yerine farklı renk (`btn-light-warning`) ve farklı ikon (`fa-layer-group`) ile havuz detay butonu gösterilir; tooltip kullanıcıya "düzenlemek için ilgili işlemi iptal edin" diye yönlendirir.
2. **Defansif katman:** Eski cache veya direct programatic çağrı durumlarında `edit_*` fonksiyonu da `is_multi_source_pool` kontrolü yapar; tıklamayla 409 round-trip atılmadan SweetAlert ile şık mesaj gösterilir.

**Backend sinyali — `is_multi_source_pool` flag:**

`get_all` response'unda her satıra iki yeni alan eklendi:
- `active_purchase_count` — `Process(transaction_type='PURCHASE', is_status='COMPLETED', is_deleted=False)` satır sayısı
- `is_multi_source_pool` — `active_purchase_count > 1` boolean

**Önemli detay:** `suppliers_count` (tedarikçi entity sayısı) **değil**, `active_purchase_count` (Process satır sayısı) kullanılır. Aynı tedarikçiden gelen 2 ayrı alış da multi-source sayılır; backend 409 guard'ı zaten `active_purchase_count > 1` üzerinden çalışıyor (frontend ile birebir tutarlı).

**Backend implementasyonu farkı:**
- **Bilezik:** Mevcut `supplier_map` loop'una `process_count` field'ı eklendi (zaten gerekli sorgu var, ek SQL yok). Status filter `is_status='COMPLETED'` netleştirildi (önceden `exclude(is_status='CANCELED')` ile yapılıyordu).
- **Hurda:** Her satıra Subquery+Count annotate edildi (`Coalesce(Subquery(...), 0)`). Bilezik gibi N satır sonrası loop yapısı yok; subquery N+1 önlemini garanti eder.

**Frontend implementasyonu (her iki template'te aynı):**
```js
const isMultiSourcePool = !!row.is_multi_source_pool;
const poolIcon = isMultiSourcePool ? 'fa-layer-group' : 'fa-circle-info';
const poolBtnClass = isMultiSourcePool ? 'btn-light-warning' : 'btn-light-info';
// ...
if (isMultiSourcePool) {
    return `<div>...${btnPoolInfo}</div>`;  // kalem yok
}
return `<div>...${btnPoolInfo}${btnEdit}</div>`;
```

**Etkilenen dosyalar:**
- `apps/bracelets/views.py::get_all` — `supplier_map` `process_count` field'ı + response `is_multi_source_pool`
- `apps/scraps/views.py::get_all` — `active_purchase_count` Subquery annotate + response `is_multi_source_pool`
- `templates/management/bracelets/index.html` — DataTable render + `edit_bracelet` defansif guard
- `templates/management/scraps/index.html` — DataTable render + `edit_scrap` defansif guard

### Migration Gerekmez
Tüm değişiklikler render katmanı + queryset filtreleri; şema değişikliği yok. Audit trail dokunulmadı.

### Notlar
- Backend 409 `MULTI_SOURCE_POOL` guard'ı **olduğu gibi korunur** — programatik veya curl çağrılarında kapı yine aktif. Frontend yalnızca kullanıcının bu kapıya çarpmamasını sağlıyor.
- `is_multi_source_pool` sinyali ileride **Diğer modüllerde** (örn. ambar transferleri, sayım) de aynı UX deseniyle kullanılabilir; `get_all` response sözleşmesine kalıcı eklenti olarak değerlendirilmeli.
- "İşlemler > İptal" akışı sonrası `Process.is_deleted=True` olduğu için iptal edilmiş alışlar `active_purchase_count`'a sayılmaz — yani 3 alış + 2 iptal sonrası `active_purchase_count=1` olur ve kalem ikonu doğal olarak geri döner. Bu UAT-1A düzeltmesinin doğal yan etkisidir.

---

## R-FAZ — PERAKENDE MİMARİ HİZALAMA (Retail Gap Closure)

**Tetikleyen analiz:** Bilezik B-Faz 1-6 ve Hurda Onarım 10-Fazı sonrası yapılan kapsamlı _"Perakende Entegrasyon ve Mimari Boşluk (Gap) Analizi"_ raporu (3 senaryo, 16 boşluk: R-1 → R-16). Onaylanan yol haritası R-2 triage → R-Faz 1-2 (havuz entegrasyonu) → R-Faz 5 (unified cancel) → R-Faz 3 (WAC kâr) → R-Faz 6 (Products.gram tutarlılık) sırasıyla uygulandı.

### R-2 (Triage — `add_scrap_to_process` 500 Crash Fix)
**Sorun:** `apps/process/retail_views.py::add_scrap_to_process` mevcut hurda havuzuna alışta `product.gram = F('gram') + gram; product.save(update_fields=['buy_price_tl','gram'])` kalıbını kullanıyordu. `Products.save()` override'ı `full_clean()` çağırıyor; clean() Decimal ile CombinedExpression karşılaştırması yapamayıp `TypeError` (veya negatif gram durumunda `ValidationError`) atıyordu → kullanıcı her hurda alışında 500 hatası alıyordu.
**Düzeltme:** `Products.objects.filter(id=...).update(gram=Greatest(F('gram')+gram, Decimal('0')), buy_price_tl=...)` deseni — atomic SQL UPDATE, `full_clean()` bypass. Bu desen merkezi `scrap_add` (apps/scraps/views.py) ve hurda Onarım Fazı 5/Adım A ile birebir aynı.
**Etkilenen:** `apps/process/retail_views.py:445`

### R-Faz 1 — Perakende Hurda Alışı, Merkezi Havuz Servisine Bağlandı
**Önceki davranış:** `add_scrap_to_process` `product_mileage` ile filtreleyip pool buluyordu (her milyem ayrı havuz). `material_type` desteği yoktu. Soft-delete'lenmiş havuza yeniden giriş kalıntı stok bırakıyordu. Aynı `process_no`'lu çoklu satırlar StockLedger'da aynı `ref_id`'yi paylaşıyordu (per-line cancel imkânsız).
**Yeni davranış:**
1. **Pool lookup** = `find_scrap_pool_by_selected_karat(scrap_name, fallback_mileage, material_type)` — havuz anahtarı kullanıcının seçtiği "X Ayar" etiketi (Onarım Fazı 9 ile aynı temel).
2. **Canonical isim** = `extract_scrap_karat_label(...)` ile normalize.
3. **Revival reset** = soft-delete'lenmiş havuza yeniden giriş tespit edilirse `StockSnapshot.stock_gram` ve `Products.gram`/`product_mileage` sıfırlanır (Onarım Fazı 6 / Bug 6 ile aynı semantik).
4. **WAC milyem** = `update_scrap_pool_weighted_mileage(...)` ile `product_mileage / buy_price_hs / sale_price_hs` ağırlıklı ortalama olarak güncellenir.
5. **Per-line stock ref** = Process satırı için önceden `uuid.uuid4()` üretilir; `StockLedger.ref_id = str(process_id)`, `Process.id = process_id`. `ref_type='scrap_add'` (toptan akışla simetrik — cancel reversal `'scrap_add_cancel'` yazar).
**Etkilenen:** `apps/process/retail_views.py::add_scrap_to_process`

### R-Faz 2 — Perakende Bilezik Alışı, Merkezi Havuz Servisine Bağlandı
**Önceki davranış:** `add_bracelet_to_retail_process` her seferinde YENİ Products + Bracelets satırı oluşturuyordu (havuz birleştirme yok). Aynı isimdeki bilezikler N farklı satıra dağılıyor, listede çoğaltma görünüyordu. Stok hareketi `complete_process` tarafına ertelenmiş (deferred); bu kontrat korundu.
**Yeni davranış:**
1. **Pool lookup** = `find_bracelet_pool_by_name(store, category, name)` (B-Faz 1 ile aynı temel).
2. **Var olan havuza ek** = mevcut Products/Bracelets satırı kullanılır; `Bracelets` revival reset (`is_deleted/is_active` sıfırlama) + soft-delete kalıntı stok temizliği.
3. **Stok ertelemesi korundu** = `record_entry` BURADA çağrılmaz; `complete_process` `update_product_stock` ile yazar (R-Faz 3'te havuz milyem WAC orada tetiklenir).
4. **Per-line stock ref** = `Process.id = uuid.uuid4()`; tamamlanma anında `update_product_stock(process_id=p.id)` ile StockLedger'a per-line `ref_id=str(p.id)` yazılır.
**Etkilenen:** `apps/process/retail_views.py::add_bracelet_to_retail_process`, ayrıca import: `from apps.bracelets.views import find_bracelet_pool_by_name, update_bracelet_pool_weighted_mileage`

### R-Faz 5 — Unified Cancel: Reversal Pattern + Pool Recalculate + Custody Soft-Delete
**Önceki sorunlar:**
- `_revert_process_stock` `ref_type='cancel_sale'/'cancel_purchase'` yazıyordu — `_cancel` suffix'i ile bitmediği için `recalculate_scrap_pool_mileage_after_cancel` orijinal ↔ reversal eşlemesini kuramıyor (havuz milyemi geri sarılmıyor).
- `update_product_stock` daima `ref_id=str(process_no)` (grup paylaşımlı) yazıyordu — per-line cancel imkânsız.
- `cancel_row` custody'yi `c_rec.delete()` ile hard-delete ediyordu (denetim izi kayboluyor).
- Cancellation cari (SupplierLedger) tarafına `cancel_stock_entry` üzerinden değil, manuel `is_active=False` flag ile dokunuyordu.

**Yeni mimari:**
1. **Per-line stock ref** = `update_product_stock` opsiyonel `process_id` parametresi aldı; verilirse `StockLedger.ref_id=str(process_id)`. `complete_process` retail tamamlanmasında her `update_product_stock` çağrısına `process_id=p.id` geçer.
2. **Reversal cancel** = `_revert_process_stock` artık `cancel_stock_entry(ref_type, ref_id=str(p.id))` çağırır:
   - Hurda PURCHASE: `ref_type='scrap_add'` (R-Faz 1'de yazılan)
   - Diğer her şey (hurda SALE, bilezik, barkodlu): `ref_type='process'`
   - `waiting_stock=True` satırlar atlanır (gerçek stoğa girmemişti).
3. **Pool recalculate** = cancel sonrası ürün tipine göre `recalculate_scrap_pool_mileage_after_cancel` veya `recalculate_bracelet_pool_mileage_after_cancel` tetiklenir → havuz milyemi/buy_price_hs/sale_price_hs aktif girişlerden ağırlıklı ortalama ile yeniden kurulur.
4. **Custody reversal** = `cancel_stock_entry(ref_type='process_custody', ref_id=str(p.id))` ile complete_process'te yazılan custody girişi 1:1 reverse edilir; `c_rec` soft-delete (`is_deleted=True, is_active=False` — model destekliyorsa, aksi halde geriye dönük uyumluluk için hard-delete).
5. **`cancel_group`** aynı pattern ile güncellendi — toplu iptalde de custody reversal + soft-delete.

**Etkilenen:**
- `apps/process/views.py::update_product_stock` — `process_id` parametresi
- `apps/process/operations.py::_revert_process_stock` — tamamen yeniden yazıldı
- `apps/process/operations.py::cancel_row, cancel_group` — custody soft-delete + reversal
- `apps/process/retail_views.py::complete_process` — `update_product_stock(..., process_id=p.id)` propagation

**Kapsam dışı (R-Faz 4'e ertelendi):** Müşteri bakiye reverse'i. `complete_process` `customer.payable_hs/receivable_hs` mutasyonlarını denetim izi olmadan yazıyor (delta hiçbir yere kaydedilmiyor). Sağlıklı reverse için CustomerLedger (Seçenek A) tasarımı gerekiyor — ayrı onayla.

### R-Faz 3 — WAC Kâr Hesabı + Bilezik Pool WAC Mileage at Completion
**Önceki sorunlar:**
- Kâr hesabı `Products.buy_price_tl` okuyordu (son giriş fiyatı, ağırlıklı ortalama değil) — ardışık alımlardan sonra kâr çarpıtıyordu.
- Kâr loop'u stok loop'undan ÖNCE çalışıyordu — same-cart takas senaryosunda yeni PURCHASE WAC'ı SALE'den önce yansımıyordu.
- Bilezik havuz milyemi havuz birleştirme sonrası ASLA güncellenmiyordu (WAC mileage update yoktu).

**Düzeltme:**
1. Kâr loop'u stok loop'undan SONRAYA taşındı. Maliyet bazı `StockSnapshot.weighted_avg_cost_tl` üzerinden okunur (per-gram veya per-piece, ürün tipine göre); snapshot yoksa fallback `Products.buy_price_tl`.
2. Stok loop'u tamamlandıktan sonra ayrı bir döngü PURCHASE rows üzerinde yürür ve bilezik havuzları için (is_scrap=False, is_gram_bullion=True veya category="Bilezik") `update_bracelet_pool_weighted_mileage(...)` çağırır → `product_mileage` ve `buy_price_hs` ağırlıklı ortalama olur.

**Etkilenen:** `apps/process/retail_views.py::complete_process` (kâr loop reorder + bilezik WAC mileage post-stock loop)

### R-Faz 6 — `Products.gram` Tutarlılığı (Legacy Alan ↔ Snapshot Senkronu)
**Önceki sorun:** Hurda SATIŞINDA `update_product_stock` yalnızca `StockSnapshot.stock_gram`'ı düşürüyor; `Products.gram` (legacy) ezberini koruyor → 100g alış + 80g satış sonrası `Products.gram=100`, `stock_gram=20`. Cancel akışında `cancel_stock_entry` snapshot'ı düzeltiyor ama `Products.gram` yine dokunulmadan kalıyor (PURCHASE iptalinde gram inflate, SALE iptalinde gram negatif kalabilir).
**Düzeltme:**
1. Retail completion'da hurda EXIT sonrası: `Products.objects.filter(id=...).update(gram=Greatest(F('gram')-p.gram, 0))`.
2. `_revert_process_stock` cancel sonrası: PURCHASE iptalinde `gram=Greatest(F('gram')-p.gram, 0)`, SALE iptalinde `gram=F('gram')+p.gram`.

**Not:** Hayalet filtre (`is_ghost`) zaten `StockSnapshot.stock_gram` üzerinden çalışır; bu fix doğrudan filtre davranışını değiştirmez ama listede `Products.gram` okuyan eski sorgu/raporlarla tutarlılık sağlar.

**Etkilenen:**
- `apps/process/retail_views.py::complete_process` (hurda SATIŞINDA `Products.gram` decrement)
- `apps/process/operations.py::_revert_process_stock` (cancel'larda gram sync)

### Migration Gerekmez (R-Faz 1-3, 5, 6)
R-Faz 1-3, 5, 6 kod-yolu refactoring; şema değişikliği yok. `update_product_stock` yeni `process_id` parametresi opsiyonel — eski çağrıcılar etkilenmez.

### R-Faz 4 — CustomerLedger (Müşteri Carisi Audit Trail, 2026-04-28)
**Önceki sorun:** `complete_process` müşteri bakiyesini (`Customers.payable_hs` / `receivable_hs`) doğrudan mutate ediyordu; iptal yolunda (`cancel_row`/`cancel_group`) müşteri bakiyesi GERİ ALINMIYORDU. Audit trail yok, kim ne zaman ne yaptı izlenemez. SupplierLedger FAZ 10'dan ders alınarak REVERSAL kaydı YOK; pasifleştirme (`is_active=False`) tercih edildi (hayalet bakiye bug'ı baştan engellendi).

**Mimari:**
1. **Yeni model:** `apps/customers/models.py::CustomerLedger` — `customer FK`, `store FK`, `process_no` (Process.process_no eşleşmesi), `transaction_type` (`DEBT` / `CREDIT`), `amount_hs` (HS cinsinden, daima pozitif), `exchange_rate_tl` (kur snapshot), `description`, `is_active`, `created_on`. SupplierLedger ile aynı yaklaşım — ENTRY/EXIT yerine müşteri perspektifli DEBT/CREDIT.
2. **Migration:** `apps/customers/migrations/0005_customerledger.py` — yeni tablo + 3 index (customer+is_active, process_no, type+is_active).
3. **`Customers.balance_hs` property:** `SUM(DEBT) - SUM(CREDIT)` aktif satırlardan. Pozitif → müşteri borçlu; negatif → mağaza borçlu. `receivable_hs_computed` / `payable_hs_computed` legacy okuma muadili (yeni okuyucu kodlar bunu kullanmalı).
4. **complete_process kayıt:** Statik mutasyon (`payable_hs/receivable_hs` netleştirme + `customer.save()`) tamamen kaldırıldı. `new_debt_hs > 0` → `CustomerLedger.create(transaction_type='DEBT', ...)`. `new_credit_hs > 0` → `CustomerLedger.create(transaction_type='CREDIT', ...)`. Netleştirme örtük (SUM aggregate'i otomatik yapar).
5. **cancel_group:** `SupplierLedger` pasifleştirmenin hemen ardından `CustomerLedger.objects.filter(process_no=pn, is_active=True).update(is_active=False)`.
6. **cancel_row PROC dalı:** Tek satır iptalinden sonra grupta kalan aktif Process var mı kontrolü (`Q(is_deleted=True) | Q(is_status='CANCELED') | Q(transaction_type='CANCELED')` exclude + mevcut p.id exclude). Hiç yoksa CustomerLedger pasife çekilir; varsa olduğu gibi bırakılır (kalan satırlar için ödeme farkı geçerli).

**Geçiş notu:** Mevcut müşteri bakiyeleri (`payable_hs`/`receivable_hs`) korunur — geçiş tarihi öncesi carry-forward snapshot. Yeni işlemler ledger'a yazılır. İleride tek seferlik `BALANCE_CARRY_FORWARD` seed migration'ı ile statik alanlar ledger'a taşınabilir; şu an tutarsızlık yok (yeni ledger satırları statik alanları DOKUNMAZ, statik alanlar dokunulmadan kalır).

**Etkilenen dosyalar:**
- `apps/customers/models.py` (CustomerLedger model + balance_hs property + Sum/Coalesce/Q importları)
- `apps/customers/migrations/0005_customerledger.py` (yeni)
- `apps/process/retail_views.py::complete_process` (statik mutasyon → CustomerLedger.create)
- `apps/process/operations.py::cancel_row, cancel_group` (CustomerLedger pasifleştirme blokları + import)

**Migration zorunlu:** `python manage.py migrate customers` çalıştırılmadan deploy edilemez (yeni tablo).

### R-Faz 7 — Perakende Sepet Lifecycle Hizalaması (Erken Stok Girişi Onarımı, 2026-04-28)

**Önceki sorun:** Perakende `add_scrap_to_process` çağrısı sepete ekleme anında `StockService.record_entry` + `update_scrap_pool_weighted_mileage` tetikliyordu. Sonuç: (1) Process IN_PROGRESS iken stok artıyor (premature stock entry), (2) henüz müşteri set edilmediği için Hurda detay modalı "Tedarikçisiz" gösteriyor, (3) taslak Process satırları havuz iptal modalında satır olarak gözüküp iptal edilebilir hale geliyor.

**Kök neden:** R-Faz 1 entegrasyonu retail hurda akışını `scrap_add` (hurda paneli, anında COMPLETED) deseniyle bağladı; oysa retail sepet bilezik/toptan deseniyle (deferred-commit, complete_process'te işlenir) çalışmalıydı. Aynı `complete_process` içinde hurda + bilezik iki farklı zaman modeliyle koşuyordu — mimari tutarsızlık.

**Mimari hizalama (4 adım):**

1. **`add_scrap_to_process` (retail_views.py) temizleme:**
   - `update_scrap_pool_weighted_mileage` çağrısı SİLİNDİ
   - Mevcut havuz dalında `StockService.record_entry` çağrısı SİLİNDİ
   - Mevcut havuz dalında `Products.gram` artırma + `buy_price_tl` yenileme SİLİNDİ
   - Yeni havuz dalında `StockService.record_entry` çağrısı SİLİNDİ
   - Yeni havuz `Products.objects.create(gram=Decimal('0'), ...)` ile oluşturulur (taslak)
   - `Process.objects.create(is_status='IN_PROGRESS', waiting_stock=False)` (bilezikle tutarlı)
   - Havuz eşleştirme + revival reset + canonical karat label KORUNUR (R-Faz 1 SSOT)

2. **`complete_process` `is_scrap_product` dalı genişletme:**
   - SALE (mv='EXIT'): mevcut akış aynen kalır (`update_product_stock` + R-Faz 6 Products.gram decrement)
   - PURCHASE (mv='ENTRY', YENİ): (a) `update_scrap_pool_weighted_mileage(p.gram, p.process_mileage)` (b) `update_product_stock(... process_id=p.id)` → record_entry (c) `Products.gram` artırma + `buy_price_tl` yenileme (`Greatest 0` floor + atomic `filter().update()`)
   - Bilezik WAC bloğu (post-stock loop) hurda satırlarını SKIP etmeye devam eder (artık ana stok döngüsünde halledildi)

3. **`_revert_process_stock` ref_type fallback:**
   - Hurda PURCHASE için `('process', 'scrap_add')` sırasıyla denenir
   - `cancel_stock_entry` `cancelled_stock_count > 0` döndüren ilk ref_type'ta döngü kırılır
   - Geriye dönük uyum: deploy öncesi `'scrap_add'` ile yazılmış legacy satırlar + R-Faz 7 sonrası `'process'` ile yazılan satırlar aynı iptal yolundan temizlenir
   - Hiç eşleşme yoksa `logger.info` (taslak iptali veya tekrar-iptal — sessiz değil ama hata da değil)

4. **Liste görünürlüğü (havuz iptal modalları):**
   - `apps/scraps/views.py` `get_pool_sources` (L1446), `get_pool_contents` (L1513), `delete` linked_procs (L1610)
   - `apps/bracelets/views.py` aynı 3 endpoint
   - `.exclude(is_status='CANCELED')` → `is_status='COMPLETED'` filtresine değiştirildi
   - Sonuç: IN_PROGRESS taslak satırları havuz iptal modalında artık gözükmez

**Customer atama:** `complete_process` `procs.update(customer=customer)` ile son anda set eder (mevcut). R-Faz 7 sonrası stok hareketi artık customer set edildikten SONRA gerçekleştiği için Hurda detay modalı kaynak olarak doğru müşteri etiketini gösterir.

**R-Faz arası ilişkiler:**
- R-Faz 1 SSOT (find_scrap_pool_by_selected_karat, update_scrap_pool_weighted_mileage, revival reset, canonical label) KORUNUR
- R-Faz 4 CustomerLedger pasifleştirme yolu aynen çalışır (process_no eşleşmesi)
- R-Faz 5 `cancel_stock_entry` çağrısı korunur, sadece ref_type fallback genişledi
- R-Faz 6 `Products.gram` Greatest+filter().update() pattern'i complete_process ENTRY dalına eklendi

**Etkilenen dosyalar:**
- `apps/process/retail_views.py` (add_scrap_to_process — stok bloğu silindi; complete_process is_scrap_product ENTRY kolu eklendi)
- `apps/process/operations.py::_revert_process_stock` (ref_type fallback ('process', 'scrap_add'))
- `apps/scraps/views.py` (3 yer: COMPLETED filtresi)
- `apps/bracelets/views.py` (3 yer: COMPLETED filtresi)

**Migration gerekmez** (sadece kod yolu refactoring; şema değişikliği yok).
