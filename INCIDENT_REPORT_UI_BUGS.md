# INCIDENT REPORT — UI Reverse-Calculation Outage

**Tarih:** 2026-04-27
**Şiddet:** P0 — Production Outage
**Etkilenen Modüller:** Perakende (`retail_index.html`), Hızlı İşlem (`fast_index.html`)
**Durum:** ✅ Hotfix uygulandı

---

## 1. Olay Özeti

Önceki iki görevde uygulanan reverse-calculation (Toplam Tutar → Adet/Gram) düzeltmeleri canlı ortamda iki kritik UX hatasına yol açtı:

| # | Belirti | Etkilenen Sayfa |
|---|---|---|
| A | "Toplam Tutar" inputuna rakam yazılamıyor (cursor anında resetleniyor) | Hem Perakende hem Hızlı İşlem |
| B | Döviz ürününe tıklayınca form alanları doğru dolmuyor / boş görünüyor | Perakende |

---

## 2. Kök Neden Analizi (Root Cause Analysis)

### 2.1 Kök Neden — Cursor Loss / Tuş Vuruşu Ezimi

**Yer:** `retail_index.html` `recalcAll()` Döviz dalı (eski satır 3460-3486) ve `fast_index.html` `onFastTotalAmountInput()` (eski satır 1187-1265).

**Hatalı Pattern:**
```javascript
// oninput event'i sırasında inputun KENDİ value'sunu üzerine yazma
input.addEventListener('input', () => {
    // ... hesaplama ...
    document.getElementById('total_price').value = lockedTotal.toFixed(2);
    //                                              ^^^^^^^^^^^
    //                                              CURSOR KAYBI
});
```

**Mekanizma:**
1. Kullanıcı `total_price` inputuna "5" yazar → DOM `value = "5"`
2. `oninput` tetiklenir → `recalcAll('total_price', true)` çağrılır
3. Yazdığım Döviz dalı: `lockedTotal = 1 * unit_price = 32.50`
4. `total_price.value = "32.50"` — kullanıcının "5"i SİLİNDİ
5. Cursor input sonuna teleport oldu
6. Kullanıcı "0" yazmaya çalışır → adımlar tekrarlanır → kullanıcı asla "50" yazamaz

**Neden Tetiklendi:**
Auto-correct (yuvarlama farkını kura tam oturtma) niyetiyle yazılan satır, **`oninput` lifecycle'ı içinde** çalıştırıldı. Auto-correct yalnızca kullanıcı yazımını **bitirdikten sonra** (blur / `change` event) yapılmalıydı.

### 2.2 Kök Neden — "Fiyatlar Gelmiyor"

**Yer:** Aynı kod bloku — yan etki.

**Mekanizma:**
1. Kullanıcı Döviz ürününe tıklar
2. `selectProduct` → `applyPriceForCurrentOperation` → `recalcAll('unit_price', false)` çağrılır
3. `unit_price` dalı total_price'ı `quantity * unit_price` ile günceller (orijinal kod)
4. Kullanıcı şimdi total_price'a yazmaya çalışır
5. Yazdığım Döviz dalı tetiklenir → kullanıcı yazdığını ezer
6. Sonuç: kullanıcı "fiyatlar gelmiyor / yazamıyorum" olarak algılar

**Asıl etki:** Crash değil — kullanıcı algısı. Kombinasyon halinde sayfa çalışmaz görünüyor.

---

## 3. Hotfix Detayı

### 3.1 `retail_index.html` (satır ~3465-3510)

**Değişiklik:**
- `total_price.value` üzerine yazma (auto-correct) **kaldırıldı**
- Tüm `getElementById` çağrıları null-guard'a sarıldı
- `try/catch` ile bütün blok izole edildi (hata olsa bile sayfa çökmez)
- `isFinite()` kontrolleri eklendi (NaN / Infinity koruması)

```javascript
} else if (sourceField === 'total_price') {
    try {
        const isCurrencyProduct = (catName === 'Döviz');
        if (isCurrencyProduct) {
            if (isFinite(unit_price) && unit_price > 0 && ...) {
                // SADECE piece/gram input'larını güncelle
                // ⚠️ total_price input'una GERİ YAZILMIYOR (cursor koruması)
            }
        } else { /* eski standart davranış, null-guard'lı */ }
    } catch (e) { console.warn(...); }
}
```

### 3.2 `fast_index.html` (satır ~1170-1280)

**Değişiklik:**
- `onFastTotalAmountInput` ikiye bölündü:
  - `onFastTotalAmountInput()` — `oninput`'tan çağrılır, totalInput'a YAZMAZ
  - `onFastTotalAmountChange()` — `onchange` (blur) anında çağrılır, auto-correct yapar
- HTML: `<input ... oninput="..." onchange="..."/>` ikili event binding
- Çekirdek `_fastReverseCalc({ applyTotalCorrection })` ortak fonksiyonu
- `safeWriteTotal()` helper'ı: `document.activeElement === totalInput` ise yine de yazmaz (çift güvenlik)
- `try/catch` blok wrap'i

### 3.3 Davranış Karşılaştırması

| Senaryo | Eski (Bozuk) | Hotfix |
|---|---|---|
| User totalInput'a yazıyor | Her tuş vuruşunda input ezilir | Her tuş vuruşu serbestçe yazılır |
| User totalInput'tan çıkıyor (blur) | — | Ziynet/Barkodlu için auto-correct çalışır |
| User Adet'e yazıyor | totalInput güncellenir | totalInput güncellenir (focus check'li) |
| Sayfa yüklenir | totalInput dolar | totalInput dolar |
| Hesaplama hatası (NaN/Infinity) | Form çöker | try/catch + console.warn |

---

## 4. Audit — Diğer Potansiyel Bombalar

| Risk | Durum | Açıklama |
|---|---|---|
| Locale (virgül/nokta) | ✅ Güvenli | `parseNumberInput` `replace(',', '.')` yapıyor |
| Division by zero | ✅ Güvenli | `unitPrice > 0` guard'ları her iki tarafta var |
| NaN propagation | ✅ Güvenli | `isFinite()` kontrolü hotfix'te eklendi |
| Null DOM element | ✅ Güvenli | Hotfix'te tüm `getElementById` null-guard'lı |
| Sonsuz döngü | ✅ Güvenli | `_suppressTotalSync` flag + programatik `.value =` event tetiklemiyor |
| WAC / StockService etkisi | ✅ Güvenli | Backend dokunulmadı — yalnızca UI katmanı |
| `txtLaborVatAmount` referans kırılması | ✅ Güvenli | Hidden input olarak korundu (Pavo invoice context için) |

---

## 5. Etkilenen Dosyalar

| Dosya | Değişiklik | Risk |
|---|---|---|
| `templates/management/transactions_board/retail_index.html` | recalcAll Döviz dalı yeniden yazıldı | DÜŞÜK — eski standart davranış aynen korundu |
| `templates/management/transactions_board/fast_index.html` | `onFastTotalAmountInput` ikiye bölündü, `onFastTotalAmountChange` eklendi | DÜŞÜK — yeni eklenen feature, eski çağrı yolları aynı |
| `INCIDENT_REPORT_UI_BUGS.md` | Yeni dosya | YOK |
| `.cursorrules` | KURAL 6, 7, 8 eklendi | YOK |

---

## 6. Test Senaryoları (Doğrulama)

| # | Senaryo | Beklenen |
|---|---|---|
| 1 | Perakende: Döviz ürün seç, Toplam Tutar'a "45000" yaz | Tuşlar serbestçe yazılır, cursor kaymaz, Adet otomatik 1385 olur |
| 2 | Perakende: Altın/Ziynet ürün seç, Toplam Tutar'a yaz | Birim Fiyat = Toplam/Adet hesaplanır (eski davranış) |
| 3 | Hızlı: Döviz seç, Toplam Tutar'a "45000" yaz, blur | Adet 1384.62 olur, blur sonrası total = 45000.15 (auto-correct) |
| 4 | Hızlı: Çeyrek (Ziynet) seç, Toplam'a "10000" yaz, blur | Adet 2 olur, blur sonrası total 9000 (auto-correct: 2 × 4500) |
| 5 | Hızlı: Birim Fiyat'ı değiştir | totalInput senkron güncellenir (forward path) |
| 6 | Hızlı: PURCHASE tab'ına geç, alış işlemi yap | Stok kontrolü bypass edilir (önceki Bug 1 fix korundu) |

---

## 7. Çıkarılan Dersler

1. **`oninput` ile auto-format yapılırken inputun kendi value'su asla overwrite edilmez** — cursor pozisyonu kaybolur. Auto-correct için `change` / `blur` kullan.
2. **Defansif DOM** — her `getElementById` null dönebilir. Production JS'de exception → tüm `<script>` block'unun durması demek.
3. **try/catch izolasyonu** — kritik UI hesaplamaları silent-fail olabilir; sayfa çalışır kalmalı.
4. **Scope sızıntısı** — bir sayfada yapılan değişiklik (Bug 2) başka sayfayı (retail) tetikledi çünkü ortak bir patern uygulandı. Tek sayfa istendiğinde, ortak patern uygulansa bile **diğer sayfalar test edilmeli**.

---

## 8. REVİZYON 2 — 2026-04-27 (Aynı Gün İkinci Hotfix)

### 8.1 Devam Eden İki Bug

Önceki hotfix sonrası iki sorun rapor edildi:
- **Bug A:** Perakende — Çeyrek/Ziynet ürünlerine tıklayınca fiyatlar (Has, Birim Fiyat) gelmiyor.
- **Bug B:** Hızlı İşlem — PURCHASE (Alış) tab'ında Dolar ürün seçince alış fiyatı boş görünüyor.

### 8.2 Kök Neden Analizi (Diff Tabanlı)

**Bug A — Çeyrek fiyat yüklenmiyor:**

`openPopup` (`retail_index.html` satır 4177) `enforceCustomPrice` koşulunda:
```javascript
if (enforceCustomPrice && (!custom_buy_price_hs || !custom_sale_price_hs))
```

İki kritik sorun:
1. **String/Number tutarsızlığı:** Backend `get-categories` bazen `"0.000"` (string, truthy), bazen `0` (number, falsy), bazen `null` döndürüyor. Salt `!value` kontrolü tutarsız sonuç veriyordu.
2. **Sert RETURN davranışı:** Custom fiyat eksikse Swal hata gösterip RETURN ediyor → popup hiç açılmıyor → kullanıcı "fiyat gelmiyor" olarak algılıyor.

Ayrıca `applyPriceForCurrentOperation` (satır 4569-4574) PURCHASE branch'inde `buyTl=0` ve `buyHs=0` durumunda fallback yoktu. SALE branch'inde fallback vardı (sale → buy), PURCHASE'ta tam tersi yapılmıyordu.

**Bug B — Fast PURCHASE alış fiyatı:**

`selectProduct()` sonunda `calculateTotalFast()` çağrılıyor. Yeni eklenen `txtTotalAmount` sync kodu (satır 1159-1167) bazı senaryolarda `total = 0` hesaplayıp inputu `"0.00"` ile dolduruyordu. Kullanıcı bunu "alış fiyatı boş" olarak algılıyor.

Ek olarak: SALE tab'ında `LABOR_VAT_RATE` hesabının kaldırılması, Pavo invoice context'ine `laborVatTotal=0` gönderilmesine neden olmuştu (UI değişikliği gibi görünse de backend'i etkileyen veri kaybı).

### 8.3 Hotfix Detayı

**`retail_index.html`:**

1. **`openPopup` enforceCustomPrice (satır ~4177):**
   - `Number()` ile string→number normalize (`_customBuyHsNum`, `_customSaleHsNum`).
   - Swal hata + RETURN yerine: WAC (`weighted_buy_price_hs`) veya Products fiyatı ile fallback.
   - Hâlâ sıfırsa toast bilgi (popup yine açılır).
   - Console.warn ile debug logu.

2. **`applyPriceForCurrentOperation` PURCHASE fallback (satır ~4569):**
   - `buyTl=0 && buyHs*rate=0` durumunda `saleTl` veya `saleHs*rate` fallback'ine düşer.

**`fast_index.html`:**

3. **`calculateTotalFast` KDV (satır ~1137):**
   - SALE tab'ında eski `laborNet * LABOR_VAT_RATE` hesabı geri getirildi (Pavo data koruması).
   - PURCHASE'ta 0 (eski davranış aynı).
   - Null guard + isFinite() koruması eklendi.

4. **`selectProduct` izolasyonu (satır ~1350):**
   - `calculateTotalFast()` çağrısı `_suppressTotalSync = true` ile sarmalandı (otomatik txtTotalAmount overwrite'ını engelle).
   - try/catch/finally ile güvenli unwrap.
   - Sonrasında doğrudan `total = qty × unit_price` hesaplanıp `txtTotalAmount`'a tek seferde yazılıyor (kullanıcı focus'ta değilse).

### 8.4 Etkilenen Dosyalar (Revizyon 2)

| Dosya | Değişiklik | Risk |
|---|---|---|
| `retail_index.html` | `openPopup` enforceCustomPrice fallback + `applyPriceForCurrentOperation` PURCHASE fallback | DÜŞÜK — sadece sıfır/eksik fiyat senaryoları etkileniyor |
| `fast_index.html` | KDV hesabı geri getirildi + `selectProduct` totalSync izolasyonu | DÜŞÜK — eski davranış restorasyonu + ekstra güvenlik |

### 8.5 Test Senaryoları (Revizyon 2)

| # | Senaryo | Beklenen |
|---|---|---|
| R2.1 | Perakende SATIŞ: Çeyrek seç (manuel has açık, custom fiyat yok) | Popup açılır, WAC fallback ile Has alanı dolar, toast uyarı çıkar |
| R2.2 | Perakende ALIŞ: Çeyrek seç (Products tablosunda buy_price=0) | unit_price sale fallback ile dolar |
| R2.3 | Hızlı SATIŞ: Çeyrek seç | KDV hidden input = işçilik × 0.20 (Pavo invoice doğru) |
| R2.4 | Hızlı ALIŞ: Dolar seç | txt-buy-fiyat dolu, txtTotalAmount hesaplanmış total ile dolu |
| R2.5 | Hızlı SATIŞ: Toplam Tutar'a yaz | Reverse calc çalışır, KDV de güncellenir |

