from django.urls import path
from apps.backups.views import (
    # FAZ A + B
    get_company_backups,
    create_backup,
    restore_backup,
    delete_backup,
    check_backup_status,
    download_backup,
    export_xlsx,
    # FAZ C + D
    smart_export,
    smart_restore,
    # FAZ 60.2 — Chunked Upload
    chunked_upload_init,
    chunked_upload_chunk,
    chunked_upload_finalize,
    chunked_upload_abort,
    # FAZ E
    get_audit_log,
)

app_name = 'backups'

urlpatterns = [
    # --- FAZ A + B (Tam Yedek) ---
    path('api/get-all/',        get_company_backups, name='get-all'),
    path('api/create/',         create_backup,       name='create'),
    path('api/restore/',        restore_backup,      name='restore'),
    path('api/delete/',         delete_backup,       name='delete'),
    path('api/check-status/',   check_backup_status, name='check_status'),
    path('download/<uuid:backup_id>/', download_backup, name='download'),
    path('api/export-xlsx/',    export_xlsx,         name='export_xlsx'),

    # --- FAZ C + D (Smart) ---
    path('api/smart/export/',   smart_export,        name='smart_export'),
    path('api/smart/restore/',  smart_restore,       name='smart_restore'),

    # --- FAZ 60.2 (Chunked Upload — Cloudflare 413 by-pass) ---
    path('api/smart/upload/init/',     chunked_upload_init,     name='chunked_upload_init'),
    path('api/smart/upload/chunk/',    chunked_upload_chunk,    name='chunked_upload_chunk'),
    path('api/smart/upload/finalize/', chunked_upload_finalize, name='chunked_upload_finalize'),
    path('api/smart/upload/abort/',    chunked_upload_abort,    name='chunked_upload_abort'),

    # --- FAZ E (Audit) ---
    path('api/audit-log/',      get_audit_log,       name='audit_log'),
]
