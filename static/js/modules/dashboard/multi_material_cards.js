/**
 * FAZ D — Dashboard Multi-Material Cards
 * ============================================================================
 * FAZ C'de /dashboard/assets-summary/ API'sine eklenen yeni alanları render eder.
 *
 * Beklenen DOM elementleri (opsiyonel; yoksa o kart atlanır):
 *   Gümüş:
 *     #mm-silver-gram       → silver_gram (gr)
 *     #mm-silver-value-hg   → silver_value_hg (HG)
 *     #mm-silver-value-tl   → silver_value_tl (TL)
 *   Pırlanta:
 *     #mm-diamond-pieces    → diamond_pieces (adet)
 *     #mm-diamond-value-tl  → diamond_value_tl (TL)
 *   Saat:
 *     #mm-watch-pieces      → watch_pieces (adet)
 *     #mm-watch-value-tl    → watch_value_tl (TL)
 *   HG Kasa Chip (dinamik):
 *     #mm-hg-cash-chip      → container (display: none/'' toggled)
 *     #mm-hg-cash-value     → HG kasa bakiyesi metni
 *
 * Kullanım:
 *   render(data): API yanıtından doldurur
 *   reload():     Kendi fetch'ini yapar ve render'ı çağırır
 */
(function (global) {
    'use strict';

    var ASSETS_ENDPOINT = '/dashboard/assets-summary/';

    function numTR(val, digits) {
        var n = parseFloat(val);
        if (isNaN(n)) n = 0;
        return n.toLocaleString('tr-TR', {
            minimumFractionDigits: digits || 0,
            maximumFractionDigits: digits || 0
        });
    }

    function setText(id, text) {
        var el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    function setDisplay(id, visible) {
        var el = document.getElementById(id);
        if (!el) return;
        el.style.display = visible ? '' : 'none';
    }

    /**
     * API yanıtından yeni kartları doldur.
     * Mevcut altın kartlarına dokunmaz (backwards compatible).
     */
    function render(data) {
        if (!data || typeof data !== 'object') return;

        var stock = data.stock_summary || {};

        // ─── Gümüş ─────────────────────────────────────────
        setText('mm-silver-gram',     numTR(stock.silver_gram, 2) + ' gr');
        setText('mm-silver-value-hg', numTR(stock.silver_value_hg, 3) + ' HG');
        setText('mm-silver-value-tl', '₺' + numTR(stock.silver_value_tl, 2));

        // ─── Pırlanta ──────────────────────────────────────
        setText('mm-diamond-pieces',   (parseInt(stock.diamond_pieces, 10) || 0) + ' adet');
        setText('mm-diamond-value-tl', '₺' + numTR(stock.diamond_value_tl, 2));

        // ─── Saat ──────────────────────────────────────────
        setText('mm-watch-pieces',   (parseInt(stock.watch_pieces, 10) || 0) + ' adet');
        setText('mm-watch-value-tl', '₺' + numTR(stock.watch_value_tl, 2));

        // ─── HG Kasa Chip (dinamik görünürlük) ─────────────
        var cashByCur = data.cash_total_by_currency || {};
        var hgAmount = parseFloat(cashByCur['HG']) || 0;
        if (hgAmount > 0) {
            setDisplay('mm-hg-cash-chip', true);
            setText('mm-hg-cash-value', numTR(hgAmount, 2) + ' HG');
        } else {
            setDisplay('mm-hg-cash-chip', false);
        }
    }

    /**
     * Kendi başına veri çekme. Dashboard sayfasında mevcut fetchStoreAssets()
     * zaten çağrıldığı için çoğu durumda gerekmez; yine de standalone kullanım
     * için açık bırakılır.
     */
    function reload() {
        return fetch(ASSETS_ENDPOINT, { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) { render(data); return data; })
            .catch(function (err) {
                console.error('DashboardMultiMaterialCards.reload error:', err);
            });
    }

    global.DashboardMultiMaterialCards = {
        render: render,
        reload: reload
    };

    // Otomatik bootstrap: wrapper container varsa ilk veriyi çek
    function bootstrap() {
        if (document.getElementById('multi-material-assets-wrap')) {
            reload();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
        bootstrap();
    }
})(window);
