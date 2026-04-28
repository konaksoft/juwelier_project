/**
 * FAZ D — Client-Side Validator
 * ============================================================================
 * Backend'deki _validate_material_type_quantities() fonksiyonunun UI ön-kontrolü.
 * Trust boundary: Backend her durumda validasyonu tekrar uygular; bu modül
 * yalnızca kullanıcıya anında feedback vermek için.
 *
 * Kurallar (FAZ B ile eş):
 *   WATCH / DIAMOND -> gram == 0 && piece_count >= 1
 *   SILVER / GOLD   -> gram > 0 (en az bir metal miktarı)
 */
(function (global) {
    'use strict';

    var PIECE_ONLY = { WATCH: true, DIAMOND: true };

    function asFloat(v) {
        var n = parseFloat(v);
        return isNaN(n) ? 0 : n;
    }

    function asInt(v) {
        var n = parseInt(v, 10);
        return isNaN(n) ? 0 : n;
    }

    function validate(formRoot, materialType) {
        var errors = [];
        var mt = (materialType || 'GOLD').toString().toUpperCase();

        var gramInput = formRoot.querySelector('[name="gram"]');
        var pieceInput = formRoot.querySelector('[name="piece_count"]');
        var gramValue = gramInput ? asFloat(gramInput.value) : 0;
        var pieceValue = pieceInput ? asInt(pieceInput.value) : 0;

        if (PIECE_ONLY[mt]) {
            if (gramValue > 0) {
                errors.push({
                    field: 'gram',
                    message: (mt === 'WATCH' ? 'Saat' : 'Pırlanta') +
                             ' ürünlerinde gram alanı 0 olmalıdır.'
                });
            }
            if (pieceValue < 1) {
                errors.push({
                    field: 'piece_count',
                    message: 'Adet en az 1 olmalıdır.'
                });
            }
        } else if (mt === 'GOLD' || mt === 'SILVER') {
            if (gramValue <= 0) {
                errors.push({
                    field: 'gram',
                    message: (mt === 'SILVER' ? 'Gümüş' : 'Altın') +
                             ' ürünlerinde gram 0\'dan büyük olmalıdır.'
                });
            }
        }

        // Pırlanta: karat zorunlu (0'dan büyük)
        if (mt === 'DIAMOND') {
            var caratInput = formRoot.querySelector('[name="diamond_carat_weight"]');
            var caratValue = caratInput ? asFloat(caratInput.value) : 0;
            if (caratValue <= 0) {
                errors.push({
                    field: 'diamond_carat_weight',
                    message: 'Pırlanta için karat ağırlığı 0\'dan büyük olmalıdır.'
                });
            }
        }

        // Saat: marka zorunlu
        if (mt === 'WATCH') {
            var brandInput = formRoot.querySelector('[name="watch_brand"]');
            var brandValue = brandInput ? (brandInput.value || '').trim() : '';
            if (!brandValue) {
                errors.push({
                    field: 'watch_brand',
                    message: 'Saat markası boş bırakılamaz.'
                });
            }
        }

        return {
            isValid: errors.length === 0,
            errors:  errors
        };
    }

    global.ProductFormValidator = {
        validate: validate
    };
})(window);
