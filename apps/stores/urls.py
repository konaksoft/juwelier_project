from django.urls import path
from apps.stores.views import *

app_name = 'stores'

urlpatterns = [
    path('index', stores_view, name='index'),
    path('add', add_store, name='add'),

    # ─── FAZ 19 — Hızlı Onboarding (Fast-Track) ───
    path('create-demo', create_demo_store_view, name='create-demo'),
    path('demo-convert/<uuid:store_id>', demo_convert_view, name='demo-convert'),
    path('demo-extend/<uuid:store_id>', demo_extend_view, name='demo-extend'),

    path('detail/<uuid:record_id>', detail_view, name='detail'),
    path('manage/<uuid:store_id>', store_detail_view, name='store-detail'),  # Mağaza Detay Sayfası
    path('manage/<uuid:store_id>/update-package-modules', update_store_package_modules, name='update-package-modules'),

    # --- SİLME VE SIFIRLAMA İŞLEMLERİ (DÜZELTİLDİ) ---
    # Mağaza verilerini sıfırlama (Ürün, stok vs. siler, mağaza kalır) — LEGACY (monolitik)
    path('hard-reset', hard_data_delete, name='hard_reset'),

    # FAZ 9.2 — Parçalı (Granular) Sıfırlama Merkezi
    path('reset-panel/', reset_panel_view, name='reset_panel'),
    path('reset-panel/preview', reset_preview_endpoint, name='reset_preview'),
    path('reset-panel/execute', reset_execute_endpoint, name='reset_execute'),

    # Mağazayı komple veritabanından silme
    path('store-hard-delete', store_hard_delete, name='store_hard_delete'),

    # Diğer İşlemler
    path('update-labor-setting', update_labor_setting, name='update-labor-setting'),
    path('delete', delete, name='delete'),  # Firma silme (Soft)
    path('hard-delete', hard_delete, name='hard-delete'),  # Firma silme (Hard)
    path('store-delete', store_delete, name='store-delete'),  # Mağaza silme (Soft)
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),

    # Doğrulama
    path('<uuid:store_id>/verify/send', send_store_verification, name='store_verify_send'),
    path('<uuid:store_id>/verify/confirm', confirm_store_verification, name='store_verify_confirm'),

    # WhatsApp & Pavo
    path("wa-usage-me", wa_usage_me, name="wa_usage_me"),
    path("wa-templates-me", wa_templates_me, name="wa_templates_me"),
    path("wa-save-templates-me", wa_save_templates_me, name="wa-save-templates-me"),
    path("wa-chat-me", wa_chat_me, name="wa_chat_me"),
    path("pavo-settings/", store_pavo_settings_view, name="pavo_settings"),

    # e-Süreç Entegrasyon Yönetimi (Automated Provisioning)
    path("esurec-provision", esurec_provision_store, name="esurec_provision"),
    path("esurec-test", esurec_test_connection, name="esurec_test"),
    path("esurec-deactivate", esurec_deactivate_store, name="esurec_deactivate"),

    # ─── FAZ 4-5: Mağaza Rol Yönetimi ───
    path("roles/", store_roles_list, name="store-roles-list"),
    path("roles/create/", store_role_create, name="store-role-create"),
    path("roles/<uuid:role_id>/update/", store_role_update, name="store-role-update"),
    path("roles/<uuid:role_id>/delete/", store_role_delete, name="store-role-delete"),
    path("roles/<uuid:role_id>/toggle-status/", store_role_toggle_status, name="store-role-toggle-status"),

    # ─── FAZ 4-5 Ek 3: Personel Rol Atama ───
    path("personnel/change-role/", store_personnel_change_role, name="store-personnel-change-role"),
]
