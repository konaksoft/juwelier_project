# ============================================================================
# DOSYA: apps/banking/admin.py
# KONUM: Kuyum Plus (jewelery_project)
# ============================================================================

from django.contrib import admin
from apps.banking.models import BankTransaction, BankAccount


@admin.register(BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display = (
        'api_transaction_id', 'store', 'bank_name', 'doc_date',
        'amount', 'currency_code', 'plus_minus',
        'other_name', 'other_vkn_tckn',
        'match_status', 'match_score', 'payment_status', 'is_read',
    )
    list_filter = (
        'match_status', 'payment_status', 'plus_minus',
        'currency_code', 'is_read', 'is_succeed', 'store',
    )
    search_fields = (
        'api_transaction_id', 'other_name', 'other_vkn_tckn',
        'other_iban', 'iban', 'note', 'doc_no', 'reference',
    )
    readonly_fields = ('id', 'created_on', 'updated_on', 'api_created_date')
    raw_id_fields = ('customer', 'store')
    ordering = ('-doc_date',)
    date_hierarchy = 'doc_date'

    fieldsets = (
        ('Kimlik', {
            'fields': ('id', 'store', 'api_transaction_id', 'api_created_date'),
        }),
        ('Hesap Bilgileri', {
            'fields': (
                'iban', 'account_no', 'account_name',
                'bank_name', 'bank_branch_code', 'bank_branch_name', 'currency_code',
            ),
        }),
        ('İşlem Bilgileri', {
            'fields': (
                'doc_no', 'doc_date', 'reference',
                'plus_minus', 'amount', 'balance', 'current_balance', 'note',
            ),
        }),
        ('Karşı Taraf', {
            'fields': ('other_iban', 'other_vkn_tckn', 'other_name'),
        }),
        ('Mysoft Kodlamaları', {
            'fields': (
                'bank_transaction_code', 'bank_transaction_desc',
                'mysoft_transaction_type',
            ),
            'classes': ('collapse',),
        }),
        ('Cari Eşleştirme', {
            'fields': ('customer', 'match_status', 'match_score'),
        }),
        ('Ödeme Durumu', {
            'fields': ('payment_status',),
        }),
        ('Durum', {
            'fields': ('is_succeed', 'api_message', 'is_read'),
        }),
        ('Sistem', {
            'fields': ('created_on', 'updated_on'),
            'classes': ('collapse',),
        }),
    )


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'store', 'bank_name', 'iban', 'currency', 'is_active', 'is_deleted')
    list_filter = ('is_active', 'is_deleted', 'currency', 'store')
    search_fields = ('name', 'bank_name', 'iban')
    readonly_fields = ('id', 'created_on', 'updated_on')
    raw_id_fields = ('store',)
