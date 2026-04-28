from rest_framework import serializers
from apps.invoices.models import Invoice, InvoiceItem


class InvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceItem
        fields = [
            'id',
            'product',
            'product_name',
            'barcode',
            'jewelry_type',
            'is_gram_bullion',
            'quantity',
            'unit',
            'unit_price',
            # Yeni eklenen kuyumculuk alanları
            'price_hs',
            'hs_to_try',
            # Hesaplama alanları
            'discount_rate',
            'discount_amount',
            'vat_rate',
            'vat_amount',
            'withholding_rate',  # Modelde varsa ekleyelim
            'withholding_amount',  # Modelde varsa ekleyelim
            'total_excl_vat',
            'total_incl_vat',
            'notes'
        ]
        read_only_fields = [
            'discount_amount',
            'vat_amount',
            'withholding_amount',
            'total_excl_vat',
            'total_incl_vat'
        ]


class InvoiceSerializer(serializers.ModelSerializer):
    items = InvoiceItemSerializer(many=True, read_only=True)

    # Store ve Customer isimlerini de dönmek isterseniz opsiyonel alanlar:
    store_name = serializers.CharField(source='store.name', read_only=True)
    customer_name = serializers.CharField(source='customer.full_name',
                                          read_only=True)  # Customer modelinize göre değişebilir

    class Meta:
        model = Invoice
        fields = [
            'id',
            'invoice_no',
            'document_number',  # GİB belge no (GIB2025...)
            'ettn',  # UUID
            'sequence_no',
            'issue_date',
            'due_date',
            'invoice_type',
            'doc_class',  # Belge türü (Fatura, Gider Pusulası vs)
            'scenario',  # Senaryo (Temel, Ticari)
            'status',
            'store', 'store_name',
            'customer', 'customer_name',
            'supplier',  # Tedarikçi alanı eklendi
            'process',
            'currency',
            'exrate_to_try',
            'hs_to_try',
            'subtotal',
            'discount_total',
            'tax_total',
            'grand_total',
            'paid_total',
            'balance',  # Modeldeki property
            'is_paid',  # Modeldeki property
            # GİB / E-Fatura Alanları (GÜNCELLENDİ)
            'is_einvoice',
            'gib_uuid',
            'gib_status_code',  # Eski 'gib_status' yerine
            'gib_status_desc',  # Yeni eklenen açıklama
            'gib_error',
            'xml_file',
            'pdf_file',
            # Pavo Alanları (Opsiyonel)
            'pavo_sale_number',
            'notes',
            'created_at',
            'updated_at',
            'items'
        ]
        read_only_fields = [
            'invoice_no',
            'document_number',
            'ettn',
            'sequence_no',
            'subtotal',
            'discount_total',
            'tax_total',
            'grand_total',
            'paid_total',
            'balance',
            'is_paid',
            'gib_uuid',
            'gib_status_code',
            'gib_status_desc',
            'created_at',
            'updated_at'
        ]
