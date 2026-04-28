/**
 * FAZ D — Product Form Manager (Orchestrator)
 * ============================================================================
 * Config + FieldGroups + Renderer'ı birleştirir.
 *
 * Kullanım (HTML'de):
 *   <form id="addProductForm" data-product-form>...</form>
 *   <!-- 4 script include + bu dosya, sayfa sonunda -->
 *
 * `data-product-form` öznitelikli tüm <form>'lara otomatik bağlanır.
 * Material_type select'inin change event'ine subscribe olur ve UI'i yeniler.
 *
 * Manuel API:
 *   ProductFormManager.attach(formEl, {materialTypeSelect: '[name="material_type"]'});
 *   ProductFormManager.applyConfig(formEl, 'SILVER');     // Forceful uygulama
 *   ProductFormManager.validate(formEl);                   // Client-side kontrol
 */
(function (global) {
    'use strict';

    function applyConfig(formRoot, materialType) {
        if (!formRoot) return;

        var Config = global.ProductFormConfig;
        var FG     = global.ProductFormFieldGroups;
        var R      = global.ProductFormRenderer;

        if (!Config || !FG || !R) {
            console.error('ProductFormManager: zorunlu modüller yüklenmedi.');
            return;
        }

        var cfg = Config.get(materialType);

        // 1) HIDE: gizle + required kaldır + (gizlenen alanların) değerini temizle
        (cfg.hide || []).forEach(function (groupName) {
            var names  = Config.resolveGroup(groupName);
            var fields = FG.findFields(formRoot, names);
            Object.keys(fields).forEach(function (key) {
                R.setFieldVisibility(fields[key], false);
                R.setFieldRequired(fields[key], false);
                // Form submit sırasında istemeden backend'e gitmesin
                R.clearFieldValue(fields[key]);
            });
        });

        // 2) SHOW: göster (required burada değil, aşağıda tek seferde)
        (cfg.show || []).forEach(function (groupName) {
            var names  = Config.resolveGroup(groupName);
            var fields = FG.findFields(formRoot, names);
            Object.keys(fields).forEach(function (key) {
                R.setFieldVisibility(fields[key], true);
            });
        });

        // 3) REQUIRED: açıkça belirtilen alanlara required ekle
        (cfg.required || []).forEach(function (fieldName) {
            var f = FG.findField(formRoot, fieldName);
            if (f) R.setFieldRequired(f, true);
        });

        // 4) LABEL: override metinler
        if (cfg.labels) {
            Object.keys(cfg.labels).forEach(function (fieldName) {
                var f = FG.findField(formRoot, fieldName);
                if (f) R.updateLabel(f, cfg.labels[fieldName]);
            });
        }

        // 5) MILEAGE DROPDOWN: SILVER için özel ayar listesi
        if (cfg.mileage_options) {
            var mileage = FG.findField(formRoot, 'product_mileage');
            if (mileage) R.populateMileageDropdown(mileage, cfg.mileage_options);
        }

        // 6) Custom event: dinleyen başka modüller varsa
        try {
            formRoot.dispatchEvent(new CustomEvent('product-form:material-changed', {
                detail: { materialType: (materialType || 'GOLD').toUpperCase(), config: cfg }
            }));
        } catch (e) { /* IE fallback */ }
    }

    function attach(formRoot, options) {
        if (!formRoot) return null;
        options = options || {};
        var selector = options.materialTypeSelect || '[name="material_type"]';
        var mtSelect = formRoot.querySelector(selector);

        if (!mtSelect) {
            console.warn(
                'ProductFormManager.attach: material_type select bulunamadı ' +
                '(form=' + (formRoot.id || '<no-id>') + ', selector=' + selector + ').'
            );
            return null;
        }

        function handler() {
            applyConfig(formRoot, mtSelect.value);
        }

        mtSelect.addEventListener('change', handler);

        // İlk açılışta mevcut değeri uygula
        applyConfig(formRoot, mtSelect.value || 'GOLD');

        return {
            formRoot: formRoot,
            refresh:  handler,
            detach:   function () { mtSelect.removeEventListener('change', handler); }
        };
    }

    function validate(formRoot) {
        if (!formRoot) return { isValid: false, errors: [{ field: null, message: 'Form bulunamadı.' }] };
        var mtSelect = formRoot.querySelector('[name="material_type"]');
        var materialType = mtSelect ? mtSelect.value : 'GOLD';
        if (!global.ProductFormValidator) {
            return { isValid: true, errors: [] };
        }
        return global.ProductFormValidator.validate(formRoot, materialType);
    }

    global.ProductFormManager = {
        attach:      attach,
        applyConfig: applyConfig,
        validate:    validate
    };

    // Otomatik bootstrap: data-product-form öznitelikli tüm form'lara bağlan
    function bootstrap() {
        var forms = document.querySelectorAll('form[data-product-form]');
        forms.forEach(function (form) { attach(form); });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
        bootstrap();
    }
})(window);
