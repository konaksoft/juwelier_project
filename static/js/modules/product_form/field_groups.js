/**
 * FAZ D — DOM Element Resolver
 * ============================================================================
 * Alan adı (name/id) üzerinden input elementini ve wrapper'ını bulur.
 * Config'ten habersizdir — sadece DOM aramayı üstlenir.
 *
 * Wrapper tespit hiyerarşisi (ilk eşleşen kazanır):
 *   1. [data-field-wrapper] özniteliği
 *   2. .form-floating
 *   3. .col-md-* (1..12)
 *   4. input.parentElement (fallback)
 */
(function (global) {
    'use strict';

    var COL_SELECTORS = [
        '.col-md-1', '.col-md-2', '.col-md-3', '.col-md-4',
        '.col-md-6', '.col-md-8', '.col-md-12', '.col-lg-3',
        '.col-lg-4', '.col-lg-6'
    ].join(', ');

    function findInput(formRoot, fieldName) {
        if (!formRoot || !fieldName) return null;
        return (
            formRoot.querySelector('[name="' + fieldName + '"]') ||
            formRoot.querySelector('#' + fieldName) ||
            null
        );
    }

    function findWrapper(input) {
        if (!input) return null;
        return (
            input.closest('[data-field-wrapper]') ||
            input.closest('.form-floating') ||
            input.closest(COL_SELECTORS) ||
            input.parentElement
        );
    }

    function findField(formRoot, fieldName) {
        var input = findInput(formRoot, fieldName);
        if (!input) return null;
        return {
            name:    fieldName,
            input:   input,
            wrapper: findWrapper(input)
        };
    }

    function findFields(formRoot, fieldNames) {
        var out = {};
        if (!fieldNames || !fieldNames.length) return out;
        for (var i = 0; i < fieldNames.length; i++) {
            var f = findField(formRoot, fieldNames[i]);
            if (f) out[fieldNames[i]] = f;
        }
        return out;
    }

    global.ProductFormFieldGroups = {
        findInput:   findInput,
        findWrapper: findWrapper,
        findField:   findField,
        findFields:  findFields
    };
})(window);
