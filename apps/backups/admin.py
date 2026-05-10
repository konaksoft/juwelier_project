from django.contrib import admin
from apps.backups.models import CompanyBackup, RestoreAuditLog


@admin.register(CompanyBackup)
class CompanyBackupAdmin(admin.ModelAdmin):
    list_display = ('id', 'company', 'created_at', 'file_size', 'created_by_user', 'status')
    list_filter = ('status', 'created_at')
    search_fields = ('company__title', 'created_by_user', 'note')
    readonly_fields = ('id', 'created_at', 'file_size')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)


@admin.register(RestoreAuditLog)
class RestoreAuditLogAdmin(admin.ModelAdmin):
    """
    FAZ A.4 — Yedekten geri yükleme izlerini gösterir.
    Append-only ledger katmanına dokunmadan merkezi audit sağlar.
    """
    list_display = (
        'id', 'restore_type', 'backup', 'restored_at',
        'restored_by', 'content_type', 'object_id',
    )
    list_filter = ('restore_type', 'restored_at', 'content_type')
    search_fields = (
        'idempotency_key', 'restore_notes', 'original_created_by',
        'restored_by__username',
    )
    readonly_fields = (
        'id', 'backup', 'restore_type', 'content_type', 'object_id',
        'idempotency_key', 'original_created_at', 'original_created_by',
        'restored_at', 'restored_by', 'restore_notes', 'similarity_warnings',
    )
    date_hierarchy = 'restored_at'
    ordering = ('-restored_at',)

    def has_add_permission(self, request):
        # Audit log manuel eklenmemeli — sadece BackupService yazsın.
        return False

    def has_change_permission(self, request, obj=None):
        # Audit log değiştirilemez (append-only).
        return False
