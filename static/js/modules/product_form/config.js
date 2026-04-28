/**
 * FAZ D — Declarative Field Configuration
 * ============================================================================
 * Pure data layer — DOM'dan habersizdir.
 *
 * Her material_type için:
 *   show:            Görünür olacak alan grubu adları
 *   hide:            Gizlenecek alan grubu adları
 *   required:        required attr eklenecek alan adları
 *   mileage_options: Ayar dropdown'ını değiştirmek için opsiyon listesi (SILVER)
 *   labels:          Alan label metinlerini üzerine yazmak için
 *
 * Yeni bir ürün tipi eklemek için yalnızca CONFIG objesine yeni anahtar eklenir.
 * Spaghetti kod engeli: renderer/validator/manager bu obje dışına çıkmaz.
 */
(function (global) {
    'use strict';

    var MATERIAL_TYPES = {
        GOLD:    'GOLD',
        SILVER:  'SILVER',
        WATCH:   'WATCH',
        DIAMOND: 'DIAMOND'
    };

    // ---------------------------------------------------------------------
    // Alan grupları — birbirine bağlı field'ların isim listesi.
    // DOM bağımlılığı YOK; sadece name/id referansı.
    // ---------------------------------------------------------------------
    var FIELD_GROUPS = {
        // Sadece altında anlamlı (alyans numarası)
        GOLD_ONLY: ['ring_size'],

        // Metal bazlı ortak alanlar (GOLD + SILVER)
        METAL_BASE: [
            'gram',
            'product_mileage',
            'gold_rate',
            'labor_mileage',
            'piece_labor'
        ],

        // Adet bazlı ortak alanlar (WATCH + DIAMOND)
        PIECE_ONLY: ['piece_count'],

        // Saat spesifik (WatchDetail alanları)
        WATCH_SPECIFIC: [
            'watch_brand',
            'watch_model_name',
            'watch_reference_no',
            'watch_serial_no',
            'watch_movement_type',
            'watch_case_material',
            'watch_case_diameter',
            'watch_year_of_mfg',
            'watch_warranty_date',
            'watch_box_papers',
            'watch_condition'
        ],

        // Pırlanta spesifik (DiamondDetail alanları - 4C)
        DIAMOND_SPECIFIC: [
            'diamond_carat_weight',
            'diamond_shape',
            'diamond_color_grade',
            'diamond_clarity_grade',
            'diamond_cut_grade',
            'diamond_certificate_lab',
            'diamond_certificate_no',
            'diamond_fluorescence',
            'diamond_depth_pct',
            'diamond_table_pct',
            'diamond_is_mounted',
            'diamond_mount_metal'
        ]
    };

    // ---------------------------------------------------------------------
    // Ana konfigürasyon: material_type -> UI davranışı
    // ---------------------------------------------------------------------
    var CONFIG = {
        GOLD: {
            show:     ['METAL_BASE', 'GOLD_ONLY'],
            hide:     ['WATCH_SPECIFIC', 'DIAMOND_SPECIFIC', 'PIECE_ONLY'],
            required: ['gram', 'product_mileage'],
            mileage_options: null,  // Altında serbest giriş / mevcut davranış
            labels: {
                product_mileage: 'Milyem / Ayar',
                gram: 'Gram'
            }
        },

        SILVER: {
            show:     ['METAL_BASE'],
            hide:     ['WATCH_SPECIFIC', 'DIAMOND_SPECIFIC', 'GOLD_ONLY', 'PIECE_ONLY'],
            required: ['gram', 'product_mileage'],
            // Gümüş milyem dropdown opsiyonları
            mileage_options: [
                { value: '999', label: '999 Ayar (Has Gümüş)' },
                { value: '925', label: '925 Ayar (Sterling)' },
                { value: '835', label: '835 Ayar' },
                { value: '800', label: '800 Ayar' }
            ],
            labels: {
                product_mileage: 'Gümüş Ayarı',
                gram: 'Gümüş Gramı'
            }
        },

        WATCH: {
            show:     ['WATCH_SPECIFIC', 'PIECE_ONLY'],
            hide:     ['METAL_BASE', 'DIAMOND_SPECIFIC', 'GOLD_ONLY'],
            required: ['piece_count', 'watch_brand'],
            mileage_options: null,
            labels: {
                piece_count: 'Adet'
            }
        },

        DIAMOND: {
            show:     ['DIAMOND_SPECIFIC', 'PIECE_ONLY'],
            hide:     ['METAL_BASE', 'WATCH_SPECIFIC', 'GOLD_ONLY'],
            required: ['piece_count', 'diamond_carat_weight'],
            mileage_options: null,
            labels: {
                piece_count: 'Adet',
                diamond_carat_weight: 'Karat Ağırlığı'
            }
        }
    };

    // ---------------------------------------------------------------------
    // Public API
    // ---------------------------------------------------------------------
    global.ProductFormConfig = {
        MATERIAL_TYPES: MATERIAL_TYPES,
        FIELD_GROUPS: FIELD_GROUPS,
        CONFIG: CONFIG,

        /** material_type için config döndürür; tanınmıyorsa GOLD default'u. */
        get: function (materialType) {
            var mt = (materialType || 'GOLD').toString().toUpperCase();
            return CONFIG[mt] || CONFIG.GOLD;
        },

        /** Grup adından alan adı listesi döndürür. */
        resolveGroup: function (groupName) {
            return FIELD_GROUPS[groupName] || [];
        }
    };
})(window);
