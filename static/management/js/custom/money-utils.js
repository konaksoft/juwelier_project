/**
 * money-utils.js — Kuyum Plus Para Formatlama & Maskeleme Yardımcıları
 *
 * API:
 *   KPMoney.safeRound(val, places)     → float-güvenli yuvarlama
 *   KPMoney.fmtMoney(val, places?)     → 6875.05  → "6.875,05"
 *   KPMoney.parseMoney(str)            → "6.875,05" → 6875.05
 *   KPMoney.unmaskedFormData(form)     → .js-money alanlarını temizlenmiş FormData döner
 *
 * Maskeleme:
 *   Input'a class="js-money" ekle (type="text" olmalı).
 *   Focus'ta raw değer gösterilir, blur'da Türkçe formatlı gösterilir.
 */
(function (global) {
    'use strict';

    // ─── Formatlayıcılar ──────────────────────────────────────────────────────
    const _fmt2 = new Intl.NumberFormat('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const _fmt3 = new Intl.NumberFormat('tr-TR', { minimumFractionDigits: 3, maximumFractionDigits: 3 });

    /**
     * Float-güvenli yuvarlama.
     * Örn: safeRound(1.005, 2) → 1.01  (toFixed yanlış: "1.00")
     */
    function safeRound(value, decimals) {
        const d = parseInt(decimals, 10) || 2;
        const factor = Math.pow(10, d);
        return Math.round((parseFloat(value || 0) + Number.EPSILON) * factor) / factor;
    }

    /**
     * Sayı → Türk formatlı string.
     * fmtMoney(6875.05)    → "6.875,05"
     * fmtMoney(99.5, 3)    → "99,500"
     */
    function fmtMoney(val, decimals) {
        const d = parseInt(decimals, 10);
        const n = safeRound(parseFloat(String(val || 0).replace(/[^0-9,.\-]/g, '').replace(',', '.')) || 0, isNaN(d) ? 2 : d);
        return isNaN(d) || d === 2 ? _fmt2.format(n) : _fmt3.format(n);
    }

    /**
     * Türk veya ingiliz formatlı string → float.
     * "6.875,05"  → 6875.05
     * "6,875.05"  → 6875.05
     * "6875.05"   → 6875.05
     * "6875,05"   → 6875.05
     */
    function parseMoney(val) {
        if (val === null || val === undefined) return 0;
        const s = String(val).trim();
        if (!s) return 0;

        const lastComma = s.lastIndexOf(',');
        const lastDot   = s.lastIndexOf('.');

        let clean;
        if (lastComma >= 0 && lastDot >= 0) {
            // Her ikisi de varsa: en sağdaki ondalık ayırıcıdır
            clean = lastComma > lastDot
                ? s.replace(/\./g, '').replace(',', '.')   // Türk: "6.875,05"
                : s.replace(/,/g, '');                      // İngiliz: "6,875.05"
        } else if (lastComma >= 0) {
            // Sadece virgül: ondalık virgül ("6875,05")
            clean = s.replace(',', '.');
        } else {
            // Sadece nokta veya hiçbiri
            clean = s;
        }
        return parseFloat(clean) || 0;
    }

    // ─── .js-money otomatik maskeleme ─────────────────────────────────────────

    document.addEventListener('focusin', function (e) {
        if (!e.target.classList.contains('js-money')) return;
        const raw = parseMoney(e.target.value);
        // Düzenleme için sadece virgüllü sayı göster: "6875,05"
        e.target.value = raw !== 0 ? String(safeRound(raw, 2)).replace('.', ',') : '';
    });

    document.addEventListener('focusout', function (e) {
        if (!e.target.classList.contains('js-money')) return;
        const raw = parseMoney(e.target.value);
        if (raw !== 0) e.target.value = fmtMoney(raw);
    });

    document.addEventListener('input', function (e) {
        if (!e.target.classList.contains('js-money')) return;
        // Sadece rakam, virgül, nokta ve eksi işaretine izin ver
        const pos = e.target.selectionStart;
        const cleaned = e.target.value.replace(/[^0-9,.\-]/g, '');
        if (cleaned !== e.target.value) {
            e.target.value = cleaned;
            try { e.target.setSelectionRange(pos - 1, pos - 1); } catch (_) {}
        }
    });

    // ─── Form submit yardımcısı ────────────────────────────────────────────────
    /**
     * Verilen form elementinden FormData oluşturur;
     * .js-money class'lı tüm inputların değerlerini
     * maskelenmiş string'den temiz "100000.00" formatına çevirir.
     */
    function unmaskedFormData(form) {
        const fd = new FormData(form);
        form.querySelectorAll('.js-money[name]').forEach(function (el) {
            fd.set(el.name, safeRound(parseMoney(el.value), 2).toFixed(2));
        });
        return fd;
    }

    // ─── Global erişim ────────────────────────────────────────────────────────
    global.KPMoney = { safeRound, fmtMoney, parseMoney, unmaskedFormData };

})(window);
