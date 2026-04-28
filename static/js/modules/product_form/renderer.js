/**
 * FAZ D — Field Renderer
 * ============================================================================
 * Config'e göre DOM güncellemesi yapan saf UI katmanı.
 * Business logic YOK — sadece show/hide/label/required gibi UI işlemleri.
 */
(function (global) {
    'use strict';

    function setFieldVisibility(field, visible) {
        if (!field || !field.wrapper) return;
        if (visible) {
            field.wrapper.classList.remove('d-none');
            // Inline style ile gizlenmişse onu da temizle
            if (field.wrapper.style.display === 'none') {
                field.wrapper.style.display = '';
            }
        } else {
            field.wrapper.classList.add('d-none');
        }
    }

    function setFieldRequired(field, required) {
        if (!field || !field.input) return;
        if (required) {
            field.input.setAttribute('required', 'required');
        } else {
            field.input.removeAttribute('required');
        }
    }

    /**
     * Label metnini günceller; mevcut ikonu (<i>) koruyarak sadece metni değiştirir.
     */
    function updateLabel(field, newLabelText) {
        if (!field || !field.wrapper) return;
        var label = field.wrapper.querySelector('label');
        if (!label) return;

        var icon = label.querySelector('i');
        if (icon) {
            // İkon + boşluk + yeni metin
            label.innerHTML = '';
            label.appendChild(icon.cloneNode(true));
            label.appendChild(document.createTextNode(' ' + newLabelText));
        } else {
            label.textContent = newLabelText;
        }
    }

    /**
     * Select elementi (<select>) için dropdown opsiyonlarını değiştirir.
     * Mevcut değer yeni listede varsa korunur; yoksa placeholder seçilir.
     * Input (<input type="text|number">) ise hiçbir şey yapılmaz — kullanıcı serbest girer.
     */
    function populateMileageDropdown(field, options) {
        if (!field || !field.input) return;
        var el = field.input;
        if (el.tagName !== 'SELECT') return;

        var prevValue = el.value;
        var html = '<option value="" selected>Seçiniz</option>';
        for (var i = 0; i < options.length; i++) {
            html +=
                '<option value="' + options[i].value + '">' +
                options[i].label +
                '</option>';
        }
        el.innerHTML = html;

        // Eski değer hâlâ mevcut listede ise koru
        for (var j = 0; j < options.length; j++) {
            if (options[j].value === prevValue) {
                el.value = prevValue;
                break;
            }
        }
    }

    /**
     * Form alanına ait değeri temizler. Gizlenen WATCH/DIAMOND alanlarının
     * eski değerlerinin backend'e gönderilmemesi için kullanılır.
     */
    function clearFieldValue(field) {
        if (!field || !field.input) return;
        var el = field.input;
        var type = (el.type || '').toLowerCase();
        if (type === 'checkbox' || type === 'radio') {
            el.checked = false;
        } else if (el.tagName === 'SELECT') {
            el.selectedIndex = 0;
        } else {
            el.value = '';
        }
    }

    global.ProductFormRenderer = {
        setFieldVisibility:       setFieldVisibility,
        setFieldRequired:         setFieldRequired,
        updateLabel:              updateLabel,
        populateMileageDropdown:  populateMileageDropdown,
        clearFieldValue:          clearFieldValue
    };
})(window);
