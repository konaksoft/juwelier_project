# Sayım Ekranı Chainway C5 RFID "Scan content on cursor" Fix (Port)

**Tarih:** 2026-05-13
**Dosya:** `templates/management/counts/index.html`
**Migration:** Yok
**Etki:** Yalnızca sayım ekranı (`/counts/`); başka modül etkilenmez.
**Kaynak:** jewelery_project'ten birebir port — kod aynı satır numaralarındaydı.

---

## Sorun

Chainway C5 UHF RFID cihazı, sayım ekranında **"Scan content on cursor"** process modunda çalışmıyordu. Okutma yapıldığında:

- Değer `#rfidInput` alanına düşmüyor
- `input` event tetiklenmiyor
- `keydown` event tetiklenmiyor
- Hiç ürün arama başlatılmıyor

Normal USB HID barkod okuyucular ise sorunsuz çalışıyordu.

---

## Kök Neden

`enableRFIDInput()` fonksiyonu sanal klavyeye karşı "çift güvenlik" amacıyla `readonly` özelliğini set ediyordu. Bunu dengelemek için bir `focus` event listener'ı `readonly`'yi kaldırıyordu.

### Neden Çalışmadı

Chainway C5 Android tabanlıdır ve "Scan content on cursor" modu **Android Input Method Engine (IME)** üzerinden text injection yapar.

1. **IME Bağlantı Sorunu:** Android WebView, input focus aldığı anda IME bağlantısını kurma kararı verir. `readonly` set iken focus geldiğinde WebView **IME bağlantısını hiç kurmaz**. Sonradan `readonly` kaldırılsa bile IME yeniden bağlanmaz — blur + yeniden focus döngüsü gerekir.

2. **Race Condition:** `enableRFIDInput(true)` çağrıldığında input zaten fokus'taysa, `rfidInput.focus()` `focus` event'ini yeniden tetiklemez → `readonly` SET edilir ama kaldırılmaz.

3. **HID Karşılaştırması:** USB HID barkod okuyucular OS seviyesinde keystroke gönderir; HTML `readonly` özelliğini bypass eder. Bu yüzden HID modu çalışıyordu, Chainway IME modu çalışmıyordu.

---

## Çözüm

`readonly` mekanizması tamamen kaldırıldı. Sanal klavye engellemesi için `inputmode="none"` tek başına yeterli (Android Chrome 66+ / iOS Safari 12.2+ destekli — Chainway C5 zaten bu sürümlerin üstündedir).

### Değiştirilen Bölüm — `enableRFIDInput()`

**Önce:**
```javascript
function enableRFIDInput(enable) {
    rfidInput.disabled = !enable;
    if (enable) {
        if (!manualEntryActive) {
            rfidInput.setAttribute('readonly', 'readonly');
        }
        rfidInput.focus();
        rfidInput.classList.add('bg-white');
    } else {
        rfidInput.value = "";
        rfidInput.removeAttribute('readonly');
        rfidInput.classList.remove('bg-white');
    }
}

rfidInput.addEventListener('focus', function () {
    if (currentSessionId && !manualEntryActive) {
        rfidInput.removeAttribute('readonly');
    }
});
```

**Sonra:**
```javascript
function enableRFIDInput(enable) {
    rfidInput.disabled = !enable;
    if (enable) {
        // Sanal klavye engelleme: inputmode="none" tek basina yeterli.
        // readonly EKLENMEZ — Android IME (Chainway "Scan content on cursor")
        // readonly input'a baglanamaz; bu yuzden readonly kaldirilmistir.
        rfidInput.focus();
        rfidInput.classList.add('bg-white');
    } else {
        rfidInput.value = "";
        rfidInput.classList.remove('bg-white');
    }
}
```

Focus event listener tamamen silindi (gereksiz kaldı).

---

## Korunan Sistemler

`readonly` kaldırılırken bozulmayacağı doğrulanan yapılar:

| Sistem | Durum | Açıklama |
|---|---|---|
| Sanal klavye engelleme | Korundu | `inputmode="none"` tek başına yeterli |
| Buffer + flush (`BUFFER_MAX=50`, `FLUSH_DELAY_MS=500`) | Bağımsız | Hız optimizasyonu, `readonly`'ye değmiyor |
| `processedBarcodes` Set (mükerrer kontrol) | Bağımsız | Hız + duplicate prevention |
| `MAX_VISIBLE_ROWS=150` DOM cap | Bağımsız | Tablo performansı |
| Lite mode + FPS monitor | Bağımsız | Düşük FPS'de DOM azaltma |
| `LAZY_MISSING_INTERVAL_MS` | Bağımsız | Eksikler tablosu lazy render |
| HID barkod okuyucu | Korundu | OS seviyesi keystroke, zaten readonly'den bağımsız |
| Manuel giriş modu | Korundu | `inputmode` toggle ediyor, `readonly`'ye girmiyordu |
| Enter ile arama (`keydown`) | Korundu | Aynı |
| 200ms debounce fallback (`input` event) | Korundu | Aynı |

---

## Beklenen Davranış

| Cihaz / Mod | Önce | Sonra |
|---|---|---|
| Chainway C5 "Scan content on cursor" | Çalışmıyor | **Çalışır** (IME injection → `input` event → arama) |
| USB HID barkod okuyucu | Çalışıyor | Çalışır (değişiklik yok) |
| Chainway C5 Keyboard HID | Çalışıyor | Çalışır (değişiklik yok) |
| Manuel klavye girişi (manuel mod) | Çalışıyor | Çalışır (değişiklik yok) |
| Sanal klavye otomatik açılması | Engelliydi | Engelli kalır (`inputmode="none"`) |

---

## Bilinen Sınırlamalar / Sonraki Adımlar

- **Modal/SweetAlert sonrası focus dönüşü:** Document click handler `e.target.closest('button, ...')` selektöründen dolayı modal kapatma butonu tıklandığında `rfidInput`'a otomatik focus dönmüyor. Bu BAĞIMSIZ bir sorun, mevcut fix kapsamında değil. Gerekirse ayrı bir fix gerekebilir.
- **Çok eski Android cihazlar (Android 8 ve altı, Chrome 65 ve altı):** `inputmode="none"` desteklenmediğinden sanal klavye açılabilir. Chainway C5 bu sürümlerin üstündedir; pratik risk yok.

---

## Dosya Değişiklik Özeti

- 1 dosya: `templates/management/counts/index.html`
- Net etki: **−18 satır** (readonly set + else removeAttribute + focus event listener + iki yorum bloğu silindi; tek yorum bloğu eklendi)
- 0 migration
- 0 backend değişikliği
- 0 başka template değişikliği
- jewelery_project ile birebir aynı
