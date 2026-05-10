"""
==============================================================================
 FAZ C — Smart Export Service
==============================================================================

Tarih: 2026-05-05
Amaç:
    Tam firma yedeği yerine ilişkisel paketler üretir:
      - "Barkodlu Ürün Paketi" — GoldPurchases + Products + Suppliers +
        SupplierLedger + (satıldıysa) Customers + Process + CustomerLedger
      - "Müşteri Paketi" — Customers + tüm CustomerLedger +
        CustomerCustodyLedger + ilgili Process'ler
      - "Mağaza Ayarları Paketi" — StoreConfiguration + StoreLabelSettings +
        BankAccount + ProductCategory + BarcodeTemplate

Karar 1 (Onaylı):
    Smart Export → Smart Restore (Merge mantığı). Hiçbir mevcut veri silinmez.
    Tedarikçi/müşteri eşleşmezse YENİ kayıt oluşturulur (Karar 5: Konservatif —
    sadece TCKN/Vergi No ile eşleştirilir, restore tarafında).

Karar 3 (Onaylı):
    Smart Export'ta CustomerLedger sadece o ürünün satışıyla ilgili
    satırları içerir (özet). Müşteri Paketi'nde ise müşterinin TÜM hareket
    geçmişi alınır (taşıma senaryosu).

Idempotency Key:
    Her item için belirleyici alanlardan SHA256 hash üretilir:
      - gold_purchases: sha256(barcode + supplier_tax_no + sale_date_iso)
      - customers:      sha256(customer_id_str + total_ledger_count)
      - settings:       sha256(store_id_str + 'settings_v1')
    Aynı paket iki kez yüklenirse RestoreAuditLog.idempotency_key kontrolü
    çift yazımı engeller.
==============================================================================
"""

import hashlib
import io
import json
import os
import zipfile
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal

from django.utils import timezone


# ---- Yardımcılar -------------------------------------------------------------

def _decimal_default(o):
    """JSON serialize için Decimal/datetime/UUID destekleyici."""
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (datetime,)):
        return o.isoformat()
    return str(o)


def _idempotency_key(*parts):
    """Sıralı parts'tan SHA256 hash döner — paket çift yazımı önleme."""
    raw = '|'.join(str(p) if p is not None else '' for p in parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _supplier_dict(supplier):
    """Tedarikçi alanlarını eşleşme öncelikli kopyala (Karar 5)."""
    if supplier is None:
        return None
    return {
        '_restore_strategy': 'match_or_create',
        '_match_fields': ['tax_number'],  # Karar 5: SADECE Vergi No ile eşleş
        'id': str(supplier.id),
        'company_name': supplier.company_name or '',
        'person_name': supplier.person_name or '',
        'person_surname': supplier.person_surname or '',
        'phone': supplier.phone or '',
        'email': supplier.email or '',
        'tax_number': supplier.tax_number or '',
        'tax_payer_type': supplier.tax_payer_type or '',
        'account_type': supplier.account_type or 'SUPPLIER',
        'company_address': supplier.company_address or '',
    }


def _customer_dict(customer):
    """Müşteri alanlarını eşleşme öncelikli kopyala (Karar 5)."""
    if customer is None:
        return None
    return {
        '_restore_strategy': 'match_or_create',
        '_match_fields': ['identification_number', 'tax_number'],  # Karar 5
        'id': str(customer.id),
        'first_name': customer.first_name or '',
        'last_name': customer.last_name or '',
        'identification_number': customer.identification_number or '',
        'tax_number': getattr(customer, 'tax_number', '') or '',
        'customer_number': customer.customer_number or '',
        'phone': customer.phone or '',
        'email': customer.email or '',
        'address': customer.address or '',
    }


def _product_dict(product):
    if product is None:
        return None
    return {
        'id': str(product.id),
        'barcode': product.barcode or '',
        'name': product.name or '',
        'jewelry_type': product.jewelry_type or '',
        'material_type': product.material_type or 'GOLD',
        'brand': product.brand or '',
        'gold_dry': str(product.gold_dry) if product.gold_dry is not None else None,
        'gold_rate': str(product.gold_rate) if product.gold_rate is not None else None,
        'product_mileage': str(product.product_mileage) if product.product_mileage is not None else None,
        'labor_mileage': str(product.labor_mileage) if product.labor_mileage is not None else None,
        'piece_labor': str(product.piece_labor) if product.piece_labor is not None else None,
        'gram': str(product.gram) if product.gram is not None else None,
        'sale_price_hs': str(product.sale_price_hs) if product.sale_price_hs is not None else None,
        'sale_price_eur': str(product.sale_price_eur) if product.sale_price_eur is not None else None,
        'buy_price_hs': str(product.buy_price_hs) if product.buy_price_hs is not None else None,
        'buy_price_eur': str(product.buy_price_eur) if product.buy_price_eur is not None else None,
        'product_hs': str(product.product_hs) if product.product_hs is not None else None,
        'is_currency': bool(getattr(product, 'is_currency', False)),
        'is_scrap': bool(getattr(product, 'is_scrap', False)),
        'is_gram_bullion': bool(getattr(product, 'is_gram_bullion', False)),
        'is_completed': bool(getattr(product, 'is_completed', False)),
        'category_name': getattr(product.category, 'name', '') if getattr(product, 'category', None) else '',
        'image_path': str(product.image) if product.image else '',
    }


# ==============================================================================
#  ANA SERVİS
# ==============================================================================

class SmartExportService:
    """
    Smart Export — ilişkisel paketler üretir.

    Public API:
        export_gold_purchases(store_id, gold_purchase_ids=None) → bytes (JSON)
        export_customers(store_id, customer_ids=None) → bytes (JSON)
        export_settings(store_id) → bytes (JSON)
        export_as_zip(payload, type_name) → bytes (ZIP — paket + manifest)
    """

    SCHEMA_VERSION = 'kp_smart_v1'

    def __init__(self, store_id):
        from apps.stores.models import Stores
        self.store = Stores.objects.get(id=store_id)
        self.store_id = store_id

    # --------------------------------------------------------------------------
    #  Barkodlu Ürün Paketi
    # --------------------------------------------------------------------------
    def export_gold_purchases(self, gold_purchase_ids=None):
        """
        Barkodlu ürünler için tam ilişkisel paket.

        Args:
            gold_purchase_ids: liste — sadece bu UUID'leri al (None = hepsi).

        Returns:
            dict — JSON-serializable payload.
        """
        from apps.gold_purchases.models import GoldPurchases
        from apps.process.models import Process
        from apps.suppliers.models import SupplierLedger
        from apps.customers.models import CustomerLedger

        qs = GoldPurchases.objects.filter(
            store=self.store, is_deleted=False
        ).select_related('product', 'supplier', 'created_by')

        if gold_purchase_ids:
            qs = qs.filter(id__in=gold_purchase_ids)

        items = []
        for gp in qs:
            product = gp.product
            supplier = gp.supplier

            # İlgili SupplierLedger satırı (alış kaydı)
            # process_no veya source_process_id ile eşleş
            supp_ledger_qs = SupplierLedger.objects.none()
            if supplier:
                supp_ledger_qs = SupplierLedger.objects.filter(
                    supplier=supplier
                ).filter(
                    # description alanında barkod eşleşmesi (alış kaydı tespiti)
                    description__icontains=product.barcode if product.barcode else 'XXXX_NEVER'
                ).order_by('created_on')[:5]  # max 5 ilgili satır

            purchase_ledger_entries = []
            for sl in supp_ledger_qs:
                purchase_ledger_entries.append({
                    'transaction_type': sl.transaction_type or '',
                    'quantity_piece': str(sl.quantity_piece) if sl.quantity_piece is not None else None,
                    'quantity_gram': str(sl.quantity_gram) if sl.quantity_gram is not None else None,
                    'amount_value': str(sl.amount_value) if sl.amount_value is not None else None,
                    'currency': sl.currency or 'HS',
                    'exchange_rate_eur': str(getattr(sl, 'exchange_rate_eur', None)) if getattr(sl, 'exchange_rate_eur', None) is not None else None,
                    'process_no': sl.process_no or '',
                    'description': sl.description or '',
                    'created_on': sl.created_on.isoformat() if sl.created_on else None,
                })

            # Satıldıysa müşteri + ilgili Process(SALE) + CustomerLedger (sadece o ürünün)
            sale_info = None
            sale_date = ''
            if product.is_completed:
                sale_proc = Process.objects.filter(
                    product=product,
                    transaction_type='SALE',
                    is_deleted=False,
                ).select_related('customer').order_by('-date').first()

                if sale_proc:
                    sale_date = sale_proc.date.isoformat() if sale_proc.date else ''
                    sale_customer = sale_proc.customer

                    # İlgili CustomerLedger satırı (sadece bu satışın)
                    cust_ledger_entries = []
                    if sale_proc.process_no:
                        cust_ledger_qs = CustomerLedger.objects.filter(
                            store=self.store,
                            process_no=sale_proc.process_no,
                        ).order_by('created_on')[:5]
                        for cl in cust_ledger_qs:
                            cust_ledger_entries.append({
                                'transaction_type': cl.transaction_type or '',
                                'amount_hs': str(cl.amount_hs) if cl.amount_hs is not None else None,
                                'amount_eur': str(cl.amount_eur) if cl.amount_eur is not None else None,
                                'currency': cl.currency or 'HS',
                                'exchange_rate_eur': str(getattr(cl, 'exchange_rate_eur', None)) if getattr(cl, 'exchange_rate_eur', None) is not None else None,
                                'process_no': cl.process_no or '',
                                'description': cl.description or '',
                                'created_on': cl.created_on.isoformat() if cl.created_on else None,
                            })

                    sale_info = {
                        'customer': _customer_dict(sale_customer),
                        'process': {
                            'process_no': sale_proc.process_no or '',
                            'transaction_type': sale_proc.transaction_type or '',
                            'process_type': sale_proc.process_type or '',
                            'piece': str(sale_proc.piece) if sale_proc.piece is not None else None,
                            'gram': str(sale_proc.gram) if sale_proc.gram is not None else None,
                            'price_hs': str(getattr(sale_proc, 'price_hs', None)) if getattr(sale_proc, 'price_hs', None) is not None else None,
                            'amount': str(getattr(sale_proc, 'amount', None)) if getattr(sale_proc, 'amount', None) is not None else None,
                            'unit_price': str(getattr(sale_proc, 'unit_price', None)) if getattr(sale_proc, 'unit_price', None) is not None else None,
                            'date': sale_date,
                        },
                        'customer_ledger_entries': cust_ledger_entries,
                    }

            status = 'SOLD' if product.is_completed else 'ACTIVE'
            idem_key = _idempotency_key(
                'gp', product.barcode or str(product.id),
                getattr(supplier, 'tax_number', '') if supplier else '',
                sale_date,
            )

            items.append({
                '_meta': {
                    'barcode': product.barcode or '',
                    'status': status,
                    'idempotency_key': idem_key,
                    'gold_purchase_id': str(gp.id),
                },
                'product': _product_dict(product),
                'supplier': _supplier_dict(supplier),
                'purchase_ledger_entries': purchase_ledger_entries,
                'sale_info': sale_info,
            })

        return {
            'export_type': 'gold_purchases_smart',
            'schema_version': self.SCHEMA_VERSION,
            'export_uuid': hashlib.sha256(
                f'{self.store_id}-{timezone.now().isoformat()}'.encode()
            ).hexdigest()[:16],
            'created_at': timezone.now().isoformat(),
            'store_id': str(self.store_id),
            'store_title': self.store.title or '',
            'item_count': len(items),
            'items': items,
        }

    # --------------------------------------------------------------------------
    #  Müşteri Paketi (Karar 3: TÜM hareket geçmişi)
    # --------------------------------------------------------------------------
    def export_customers(self, customer_ids=None):
        """
        Müşteri paketi — taşıma senaryosu için müşterinin TÜM hareket geçmişi.
        """
        from apps.customers.models import Customers, CustomerLedger
        from apps.custody.models import CustomerCustodyLedger
        from apps.process.models import Process

        qs = Customers.objects.filter(
            store=self.store, is_deleted=False
        ).distinct()
        if customer_ids:
            qs = qs.filter(id__in=customer_ids)

        items = []
        for c in qs:
            ledger_entries = []
            for cl in CustomerLedger.objects.filter(
                customer=c, store=self.store
            ).order_by('created_on'):
                ledger_entries.append({
                    'transaction_type': cl.transaction_type or '',
                    'amount_hs': str(cl.amount_hs) if cl.amount_hs is not None else None,
                    'amount_eur': str(cl.amount_eur) if cl.amount_eur is not None else None,
                    'amount_fx': str(getattr(cl, 'amount_fx', None)) if getattr(cl, 'amount_fx', None) is not None else None,
                    'currency': cl.currency or 'HS',
                    'exchange_rate_eur': str(getattr(cl, 'exchange_rate_eur', None)) if getattr(cl, 'exchange_rate_eur', None) is not None else None,
                    'process_no': cl.process_no or '',
                    'description': cl.description or '',
                    'created_on': cl.created_on.isoformat() if cl.created_on else None,
                    'is_active': bool(getattr(cl, 'is_active', True)),
                })

            custody_entries = []
            for cc in CustomerCustodyLedger.objects.filter(
                customer=c, store=self.store, is_deleted=False
            ).order_by('created_on'):
                custody_entries.append({
                    'transaction_type': getattr(cc, 'transaction_type', '') or '',
                    'gram_value': str(getattr(cc, 'gram_value', None)) if getattr(cc, 'gram_value', None) is not None else None,
                    'process_no': getattr(cc, 'process_no', '') or '',
                    'created_on': cc.created_on.isoformat() if cc.created_on else None,
                })

            processes = []
            for p in Process.objects.filter(
                customer=c, store=self.store, is_deleted=False
            ).order_by('-date')[:200]:  # son 200 işlem
                processes.append({
                    'process_no': p.process_no or '',
                    'transaction_type': p.transaction_type or '',
                    'process_type': p.process_type or '',
                    'gram': str(p.gram) if p.gram is not None else None,
                    'piece': str(p.piece) if p.piece is not None else None,
                    'amount': str(getattr(p, 'amount', None)) if getattr(p, 'amount', None) is not None else None,
                    'date': p.date.isoformat() if p.date else None,
                })

            idem_key = _idempotency_key(
                'cust', str(c.id),
                c.identification_number or c.phone or '',
                len(ledger_entries),
            )

            items.append({
                '_meta': {
                    'customer_name': f"{c.first_name or ''} {c.last_name or ''}".strip(),
                    'idempotency_key': idem_key,
                },
                'customer': _customer_dict(c),
                'identity_images': {
                    'front': str(c.identification_front_image) if c.identification_front_image else '',
                    'back': str(c.identification_back_image) if c.identification_back_image else '',
                },
                'ledger_entries': ledger_entries,
                'custody_entries': custody_entries,
                'recent_processes': processes,
            })

        return {
            'export_type': 'customers_smart',
            'schema_version': self.SCHEMA_VERSION,
            'export_uuid': hashlib.sha256(
                f'{self.store_id}-cust-{timezone.now().isoformat()}'.encode()
            ).hexdigest()[:16],
            'created_at': timezone.now().isoformat(),
            'store_id': str(self.store_id),
            'item_count': len(items),
            'items': items,
        }

    # --------------------------------------------------------------------------
    #  Mağaza Ayarları Paketi
    # --------------------------------------------------------------------------
    def export_settings(self):
        """
        Mağaza ayarları + kasalar + kategori/şablonlar.
        Hareket verisi içermez (cari/işlem yok).
        """
        from apps.settings.models import StoreConfiguration, StoreLabelSettings
        from apps.banking.models import BankAccount
        from apps.gold_purchases.models import ProductCategory, BarcodeTemplate

        sc = StoreConfiguration.objects.filter(store=self.store).first()
        sls = StoreLabelSettings.objects.filter(store=self.store).first()

        bank_accounts = []
        for ba in BankAccount.objects.filter(store=self.store, is_deleted=False):
            bank_accounts.append({
                'name': ba.name or '',
                'bank_name': ba.bank_name or '',
                'iban': ba.iban or '',
                'currency': ba.currency or 'TRY',
                'account_type': ba.account_type or 'CASH',
                'reconciliation_tolerance': str(getattr(ba, 'reconciliation_tolerance', '0')),
                'is_inter_branch_transit_account': bool(getattr(ba, 'is_inter_branch_transit_account', False)),
            })

        categories = []
        for pc in ProductCategory.objects.filter(store=self.store, is_deleted=False):
            categories.append({
                'name': pc.name or '',
                'barcode_prefix': pc.barcode_prefix or '',
                'is_active': bool(getattr(pc, 'is_active', True)),
            })

        barcode_templates = []
        for bt in BarcodeTemplate.objects.filter(store=self.store, is_deleted=False).select_related('supplier'):
            barcode_templates.append({
                'material_type': bt.material_type or 'GOLD',
                'jewelry_type': getattr(bt, 'jewelry_type', '') or '',
                'gold_rate': str(getattr(bt, 'gold_rate', '')) if getattr(bt, 'gold_rate', None) is not None else '',
                'product_mileage': str(getattr(bt, 'product_mileage', '')) if getattr(bt, 'product_mileage', None) is not None else '',
                'labor_mileage': str(getattr(bt, 'labor_mileage', '')) if getattr(bt, 'labor_mileage', None) is not None else '',
                'piece_labor': str(getattr(bt, 'piece_labor', '')) if getattr(bt, 'piece_labor', None) is not None else '',
                'ring_size': str(getattr(bt, 'ring_size', '')) if getattr(bt, 'ring_size', None) is not None else '',
                'supplier_tax_number': bt.supplier.tax_number if bt.supplier else '',
                'extra_data': getattr(bt, 'extra_data', None) or {},
            })

        config_dict = None
        if sc:
            config_dict = {
                'language_code': getattr(sc, 'language_code', '') or '',
                'base_spot_currency': getattr(sc, 'base_spot_currency', '') or '',
                'base_spot_unit': getattr(sc, 'base_spot_unit', '') or '',
                'price_margin_percent': str(getattr(sc, 'price_margin_percent', '0')),
                'use_average_labor': bool(getattr(sc, 'use_average_labor', False)),
                'use_manual_has_calculation': bool(getattr(sc, 'use_manual_has_calculation', False)),
                'use_manual_currency_rate': bool(getattr(sc, 'use_manual_currency_rate', False)),
                'manual_currency_rates': getattr(sc, 'manual_currency_rates', {}) or {},
                'enforce_cash_limit': bool(getattr(sc, 'enforce_cash_limit', True)),
                'is_safe_approval_required': bool(getattr(sc, 'is_safe_approval_required', False)),
                'enforce_invoice_customer': bool(getattr(sc, 'enforce_invoice_customer', False)),
                'enforce_masak_identity': bool(getattr(sc, 'enforce_masak_identity', True)),
                'enforce_customer_always': bool(getattr(sc, 'enforce_customer_always', False)),
                'require_customer_phone': bool(getattr(sc, 'require_customer_phone', True)),
                'require_customer_tckn': bool(getattr(sc, 'require_customer_tckn', True)),
                'debt_currency_mode': getattr(sc, 'debt_currency_mode', 'HS') or 'HS',
                'allow_overpayment_default': bool(getattr(sc, 'allow_overpayment_default', False)),
            }

        label_dict = None
        if sls:
            label_dict = {
                'active_size': getattr(sls, 'active_size', 'small') or 'small',
                'small_design': getattr(sls, 'small_design', {}) or {},
                'large_design': getattr(sls, 'large_design', {}) or {},
                'diamond_small_design': getattr(sls, 'diamond_small_design', {}) or {},
                'diamond_large_design': getattr(sls, 'diamond_large_design', {}) or {},
                'watch_small_design': getattr(sls, 'watch_small_design', {}) or {},
                'watch_large_design': getattr(sls, 'watch_large_design', {}) or {},
                'label_bottom_left_type': getattr(sls, 'label_bottom_left_type', '') or '',
                'label_layout_mode': getattr(sls, 'label_layout_mode', 'STANDARD') or 'STANDARD',
                'rfid_mode': bool(getattr(sls, 'rfid_mode', False)),
            }

        idem_key = _idempotency_key('settings', str(self.store_id), 'v1')

        return {
            'export_type': 'settings_smart',
            'schema_version': self.SCHEMA_VERSION,
            'export_uuid': hashlib.sha256(
                f'{self.store_id}-set-{timezone.now().isoformat()}'.encode()
            ).hexdigest()[:16],
            'created_at': timezone.now().isoformat(),
            'store_id': str(self.store_id),
            'idempotency_key': idem_key,
            'configuration': config_dict,
            'label_settings': label_dict,
            'bank_accounts': bank_accounts,
            'product_categories': categories,
            'barcode_templates': barcode_templates,
        }

    # --------------------------------------------------------------------------
    #  Yardımcı: payload'u JSON bytes'a çevir
    # --------------------------------------------------------------------------
    @staticmethod
    def to_json_bytes(payload):
        return json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            default=_decimal_default,
        ).encode('utf-8')

    # --------------------------------------------------------------------------
    #  Yardımcı: payload'dan media path listesi topla
    # --------------------------------------------------------------------------
    @staticmethod
    def _collect_media_paths(payload):
        """
        Payload içindeki tüm ImageField path'lerini toplar.

        Returns:
            list[str] — MEDIA_ROOT'a göre relative path'ler.
            'default/default.png' ve boş path'ler atlanır.
        """
        from apps.backups.media_packager import DEFAULT_IMAGE_PATHS

        paths = set()
        export_type = payload.get('export_type', '')
        items = payload.get('items', []) or []

        if export_type == 'gold_purchases_smart':
            for it in items:
                p = (it.get('product') or {}).get('image_path') or ''
                if p and p not in DEFAULT_IMAGE_PATHS:
                    paths.add(p)
                # satıldıysa müşteri kimlik görselleri (var ise sale_info'da yok,
                # ürün paketinde müşteri görseli taşınmaz — sadece müşteri paketinde)

        elif export_type == 'customers_smart':
            for it in items:
                imgs = it.get('identity_images') or {}
                for key in ('front', 'back'):
                    p = imgs.get(key) or ''
                    if p and p not in DEFAULT_IMAGE_PATHS:
                        paths.add(p)

        # settings_smart paketinde görsel yok
        return sorted(paths)

    # --------------------------------------------------------------------------
    #  Yardımcı: Görseli RAM içinde optimize et (FAZ 60.2)
    # --------------------------------------------------------------------------
    @staticmethod
    def _optimize_image_bytes(abs_path, max_dim=1024, quality=75):
        """
        Pillow ile görseli yeniden boyutlandır + JPEG quality azalt.

        - Maksimum boyut max_dim×max_dim (oran korunur, sadece küçültür).
        - Şeffaflık varsa (PNG/RGBA) → beyaz arkaplanla flatten + JPEG.
        - Animated/multi-frame → ilk frame.
        - Hata olursa orijinal dosya bytes'ı döner (graceful degrade).

        Args:
            abs_path: Path veya str — kaynak görsel mutlak yol.
            max_dim:  int — uzun kenar maksimum px.
            quality:  int — JPEG quality (1-95).

        Returns:
            tuple[bytes, str, bool] — (opt_bytes, arc_extension, was_optimized)
            arc_extension: '.jpg' (her zaman, optimize edildiyse).
                           Aksi halde orijinal uzantı.
            was_optimized: True ise yeniden kodlandı.
        """
        try:
            from PIL import Image  # type: ignore
        except Exception:
            # Pillow yoksa orijinali kullan
            with open(str(abs_path), 'rb') as fh:
                return fh.read(), os.path.splitext(str(abs_path))[1].lower() or '.bin', False

        try:
            with Image.open(str(abs_path)) as im:
                # İlk frame (animasyonlu görseller için)
                if getattr(im, 'is_animated', False):
                    im.seek(0)
                # Şeffaflığı flatten et
                if im.mode in ('RGBA', 'LA', 'P'):
                    bg = Image.new('RGB', im.size, (255, 255, 255))
                    if im.mode == 'P':
                        im = im.convert('RGBA')
                    bg.paste(im, mask=im.split()[-1] if im.mode in ('RGBA', 'LA') else None)
                    im = bg
                elif im.mode != 'RGB':
                    im = im.convert('RGB')
                # Resize (oran korunur)
                w, h = im.size
                if max(w, h) > max_dim:
                    if w >= h:
                        new_w = max_dim
                        new_h = int(round(h * (max_dim / float(w))))
                    else:
                        new_h = max_dim
                        new_w = int(round(w * (max_dim / float(h))))
                    im = im.resize((new_w, new_h), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format='JPEG', quality=int(quality), optimize=True)
                return buf.getvalue(), '.jpg', True
        except Exception:
            # Hata: orijinali kullan
            try:
                with open(str(abs_path), 'rb') as fh:
                    return fh.read(), os.path.splitext(str(abs_path))[1].lower() or '.bin', False
            except OSError:
                return b'', '.bin', False

    # --------------------------------------------------------------------------
    #  ZIP formatında dışa aktarım — payload.json + manifest.json + media/
    # --------------------------------------------------------------------------
    def export_as_zip(self, payload, kind, include_media=True,
                      optimize_media=True, optimize_max_dim=1024,
                      optimize_quality=75):
        """
        Smart export paketini ZIP olarak paketle.

        ZIP içeriği:
            manifest.json  — meta bilgi (export_type, schema_version, item_count,
                             include_media, media_file_count, created_at, ...)
            payload.json   — to_json_bytes(payload) çıktısı (geriye uyum)
            media/         — opsiyonel; payload'daki image_path'lere karşılık
                             gelen gerçek dosyalar (MEDIA_ROOT-relative)

        Args:
            payload: SmartExportService.export_*() çıktısı.
            kind:    'gold_purchases' | 'customers' | 'settings'
            include_media: bool — False ise sadece manifest+payload.
            optimize_media: bool (FAZ 60.2) — True ise görseller Pillow ile
                            yeniden kodlanır (1024×1024 max, JPEG q75).
                            Disk üzerindeki orijinaller dokunulmaz; sadece
                            ZIP içine optimize hâl yazılır.
            optimize_max_dim / optimize_quality: optimize parametreleri.

        Returns:
            tuple[bytes, dict] — (zip_bytes, stats)
            stats: {'media_count', 'media_bytes', 'media_bytes_original',
                    'optimize_media', 'failed': [...]}
        """
        from pathlib import Path
        from django.conf import settings as dj_settings

        media_root = Path(dj_settings.MEDIA_ROOT)
        media_paths = self._collect_media_paths(payload) if include_media else []

        media_count = 0
        media_bytes = 0
        media_bytes_original = 0
        media_optimized_count = 0
        failed = []

        # Optimize edilmiş path'leri payload içinde de yansıtmak için map
        # (ürün/müşteri image_path'leri ZIP içinde farklı uzantıda yer alabilir)
        # Restore tarafı path'i payload'dan okuyor — bu yüzden tutarlı olmalı.
        optimized_path_map = {}  # rel_orig → rel_in_zip

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            # 1) Media dosyaları (önce — path map oluştuktan sonra payload yazılır)
            if include_media:
                for rel in media_paths:
                    abs_p = media_root / rel
                    if not abs_p.exists():
                        failed.append({'path': rel, 'reason': 'not_found'})
                        continue
                    try:
                        orig_size = abs_p.stat().st_size
                        media_bytes_original += orig_size

                        if optimize_media:
                            data, ext, was_opt = self._optimize_image_bytes(
                                abs_p,
                                max_dim=optimize_max_dim,
                                quality=optimize_quality,
                            )
                            if was_opt and data:
                                # Uzantıyı .jpg ile değiştir, path'i map'e ekle
                                base, _old_ext = os.path.splitext(rel)
                                rel_new = base + ext
                                arcname = f'media/{rel_new}'
                                zf.writestr(arcname, data)
                                optimized_path_map[rel] = rel_new
                                media_count += 1
                                media_bytes += len(data)
                                media_optimized_count += 1
                                continue
                        # Optimize devre dışı veya başarısız → orijinal
                        arcname = f'media/{rel}'
                        zf.write(str(abs_p), arcname=arcname)
                        media_count += 1
                        media_bytes += orig_size
                    except Exception as e:
                        failed.append({'path': rel, 'reason': str(e)})

            # 2) Payload'da image_path'leri optimize map'e göre güncelle
            if optimized_path_map:
                self._rewrite_image_paths(payload, optimized_path_map)

            # 3) payload.json (path map güncellemesinden sonra)
            json_bytes = self.to_json_bytes(payload)
            zf.writestr('payload.json', json_bytes)

            # 4) manifest.json (en sonda — istatistik dolu olsun)
            manifest = {
                'kuyumplus_smart_package': True,
                'schema_version': self.SCHEMA_VERSION,
                'export_type': payload.get('export_type', ''),
                'export_uuid': payload.get('export_uuid', ''),
                'kind': kind,
                'created_at': payload.get('created_at', ''),
                'store_id': str(self.store_id),
                'store_title': self.store.title or '',
                'item_count': payload.get('item_count', 0),
                'include_media': bool(include_media),
                'media_file_count': media_count,
                'media_total_bytes': media_bytes,
                'media_total_bytes_original': media_bytes_original,
                'media_optimized_count': media_optimized_count,
                'optimize_media': bool(optimize_media and include_media),
                'optimize_max_dim': int(optimize_max_dim) if optimize_media else None,
                'optimize_quality': int(optimize_quality) if optimize_media else None,
                'media_failed_count': len(failed),
            }
            zf.writestr(
                'manifest.json',
                json.dumps(manifest, indent=2, ensure_ascii=False).encode('utf-8'),
            )

        stats = {
            'media_count': media_count,
            'media_bytes': media_bytes,
            'media_bytes_original': media_bytes_original,
            'media_optimized_count': media_optimized_count,
            'optimize_media': bool(optimize_media and include_media),
            'failed': failed,
        }
        return buf.getvalue(), stats

    # --------------------------------------------------------------------------
    #  Yardımcı: Optimize sonrası payload içindeki image_path'leri rewrite et
    # --------------------------------------------------------------------------
    @staticmethod
    def _rewrite_image_paths(payload, path_map):
        """
        Optimize edilen görsellerin payload içindeki referanslarını günceller.
        path_map: {orig_rel_path: new_rel_path}

        gold_purchases_smart → items[].product.image_path
        customers_smart      → items[].identity_images.{front,back}
        """
        if not path_map:
            return
        export_type = payload.get('export_type', '')
        items = payload.get('items', []) or []

        if export_type == 'gold_purchases_smart':
            for it in items:
                prod = it.get('product') or {}
                p = prod.get('image_path') or ''
                if p in path_map:
                    prod['image_path'] = path_map[p]
        elif export_type == 'customers_smart':
            for it in items:
                imgs = it.get('identity_images') or {}
                for key in ('front', 'back'):
                    p = imgs.get(key) or ''
                    if p in path_map:
                        imgs[key] = path_map[p]
