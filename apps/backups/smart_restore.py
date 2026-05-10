"""
==============================================================================
 FAZ D — Smart Restore Service
==============================================================================

Tarih: 2026-05-05
Amaç:
    SmartExportService'in ürettiği ilişkisel paketleri MERGE mantığıyla
    geri yükler. Tam Restore'dan farkı: mevcut veri SİLİNMEZ.

Karar Matrisi:
    Karar 1: Smart Restore = Merge (upsert), tam silmez.
    Karar 4: RestoreAuditLog her item için ayrı kayıt + idempotency_key.
    Karar 5: Konservatif eşleşme — TCKN/Vergi No zorunlu, yoksa yeni kayıt.

Public API:
    SmartRestoreService(store_id)
        .restore_gold_purchases(payload, user, dry_run=False) → dict
        .restore_customers(payload, user, dry_run=False) → dict
        .restore_settings(payload, user, dry_run=False) → dict
        .restore_from_payload(payload, user, dry_run=False) → dict (auto-route)

Dry-run Modu:
    dry_run=True iken hiçbir veri yazılmaz, sadece "ne olacak" raporu döner:
        {
          'dry_run': True,
          'will_create_suppliers': N,
          'will_match_suppliers': N,
          'will_create_customers': N,
          'will_match_customers': N,
          'barcode_conflicts': [...],
          'idempotency_skips': [...],
          'similarity_warnings': [...],
        }
==============================================================================
"""

import io
import json
import os
import traceback
import uuid
import zipfile
from decimal import Decimal
from pathlib import Path

from django.conf import settings as dj_settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from apps.backups.id_mapper import IdMapper
from apps.backups.conflict_resolver import ConflictResolver
from apps.backups.media_packager import MediaPackager, DEFAULT_IMAGE_PATHS


# ZIP magic bytes (PK\x03\x04) — smart restore'da tip tespiti için
_ZIP_MAGIC = b'PK\x03\x04'


def _is_zip_bytes(raw):
    """raw'in ZIP olup olmadığını magic byte ile tespit et."""
    if isinstance(raw, (bytes, bytearray, memoryview)) and len(raw) >= 4:
        return bytes(raw[:4]) == _ZIP_MAGIC
    return False


def _is_zip_file(path):
    """Bir dosyanın ZIP olup olmadığını ilk 4 byte ile tespit et."""
    try:
        with open(path, 'rb') as fh:
            head = fh.read(4)
        return head == _ZIP_MAGIC
    except OSError:
        return False


def _parse_smart_package(raw_or_payload):
    """
    Smart restore girdisini normalize et.

    Args:
        raw_or_payload:
            - dict: zaten parse edilmiş payload
            - bytes/bytearray/str: ZIP veya JSON ham içerik (bellek-içi)
            - str path (chunked upload finalize): büyük dosya → stream parse

    Returns:
        tuple[dict, bytes|None] — (payload_dict, zip_bytes_or_None)
        zip_bytes None ise media yoktur (saf JSON paket).
        FAZ 60.2 sonrası: file path verildiğinde zip_bytes RAM'e
        yüklenmek yerine path ile temsil edilir — _extract_media_from_zip
        path versiyonu ile çalışır.
    """
    # 1) Zaten dict ise
    if isinstance(raw_or_payload, dict):
        return raw_or_payload, None

    # 2) Dosya yolu (büyük paket — chunked upload finalize)
    if isinstance(raw_or_payload, (str, Path)):
        candidate = str(raw_or_payload)
        if os.path.isfile(candidate):
            if _is_zip_file(candidate):
                # ZIP: payload.json kısmını disk üzerinden oku (RAM'i şişirme)
                with zipfile.ZipFile(candidate, 'r') as zf:
                    names = set(zf.namelist())
                    if 'payload.json' not in names:
                        raise ValueError(
                            'ZIP paket bozuk: payload.json bulunamadı.'
                        )
                    with zf.open('payload.json') as fp:
                        payload = json.loads(fp.read().decode('utf-8'))
                # zip_bytes yerine path döndür — caller dosyadan çıkarır
                return payload, candidate
            # JSON dosya
            with open(candidate, 'rb') as fh:
                raw = fh.read()
            try:
                payload = json.loads(raw.decode('utf-8'))
                return payload, None
            except (UnicodeDecodeError, json.JSONDecodeError) as je:
                raise ValueError(f'Geçersiz JSON / paket: {je}')
        # Yol değilse aşağıda bytes/str olarak işle
        raw = raw_or_payload.encode('utf-8') if isinstance(raw_or_payload, str) \
            else raw_or_payload
    else:
        raw = raw_or_payload

    # 3) bytes — ZIP mı JSON mı?
    if isinstance(raw, str):
        raw = raw.encode('utf-8')

    if _is_zip_bytes(raw):
        # ZIP paket: payload.json'ı oku
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = set(zf.namelist())
            if 'payload.json' not in names:
                raise ValueError('ZIP paket bozuk: payload.json bulunamadı.')
            with zf.open('payload.json') as fp:
                payload = json.loads(fp.read().decode('utf-8'))
        return payload, raw

    # 4) Düz JSON
    try:
        text = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else str(raw)
        payload = json.loads(text)
        return payload, None
    except json.JSONDecodeError as je:
        raise ValueError(f'Geçersiz JSON / paket: {je}')


def _extract_media_from_zip(zip_source):
    """
    Smart paket ZIP'inden media/ altındaki dosyaları MEDIA_ROOT'a çıkar.

    ZipSlip korumalı (MediaPackager.extract_from_zip ile aynı mantık).

    Args:
        zip_source: bytes (RAM-içi) veya str path (büyük dosya).

    Returns:
        int — başarılı çıkarılan dosya sayısı.
    """
    if not zip_source:
        return 0
    if isinstance(zip_source, (str, Path)):
        # Disk üzerinden aç (RAM'e yüklemeden)
        with zipfile.ZipFile(str(zip_source), 'r') as zf:
            return MediaPackager.extract_from_zip(
                zf, dj_settings.MEDIA_ROOT, base_path='media/',
            )
    # bytes
    with zipfile.ZipFile(io.BytesIO(zip_source)) as zf:
        return MediaPackager.extract_from_zip(
            zf, dj_settings.MEDIA_ROOT, base_path='media/',
        )


def _to_decimal(val):
    if val is None or val == '':
        return None
    try:
        return Decimal(str(val))
    except Exception:
        return None


# ==============================================================================
#  ANA SERVİS
# ==============================================================================

class SmartRestoreService:
    """
    Smart Restore — paketleri merge mantığıyla yükler.
    """

    def __init__(self, store_id, backup_id=None):
        from apps.stores.models import Stores
        self.store = Stores.objects.get(id=store_id)
        self.store_id = store_id
        self.backup_id = backup_id  # RestoreAuditLog için
        self.mapper = IdMapper()
        self.resolver = ConflictResolver(self.store)
        # ZIP paket bayrakları — restore_from_payload ZIP açtığında set edilir.
        # Direct dict çağrılarında False kalır → image path'leri sadece dosya
        # gerçekten varsa atanır.
        self._zip_has_media = False
        self._media_extracted = 0
        self._media_extract_error = None

    # --------------------------------------------------------------------------
    #  Auto-route — payload export_type'a göre uygun restore çağırır
    #  Girdi:
    #    - dict           → eski akış (saf JSON payload)
    #    - bytes/str ZIP  → media içeren akıllı paket (manifest+payload+media/)
    #    - bytes/str JSON → düz JSON paket
    # --------------------------------------------------------------------------
    def restore_from_payload(self, payload, user=None, dry_run=False):
        # Girdiyi normalize et — ZIP varsa media çıkarmadan önce parse
        try:
            parsed_payload, zip_bytes = _parse_smart_package(payload)
        except Exception as e:
            return {'success': False, 'error': str(e)}

        # Media çıkarma — sadece gerçek restore'da (dry-run değil) yapılır
        media_extracted = 0
        if zip_bytes and not dry_run:
            try:
                media_extracted = _extract_media_from_zip(zip_bytes)
            except Exception as e:
                # Media hatası restore'u durdurmasın, sadece raporla
                media_extracted = 0
                self._media_extract_error = str(e)
        # Restore akışlarında image path'leri kullanmak için flag
        self._zip_has_media = bool(zip_bytes)
        self._media_extracted = media_extracted

        export_type = parsed_payload.get('export_type', '')
        if export_type == 'gold_purchases_smart':
            report = self.restore_gold_purchases(parsed_payload, user, dry_run)
        elif export_type == 'customers_smart':
            report = self.restore_customers(parsed_payload, user, dry_run)
        elif export_type == 'settings_smart':
            report = self.restore_settings(parsed_payload, user, dry_run)
        else:
            return {
                'success': False,
                'error': f"Bilinmeyen export_type: {export_type}",
            }

        # Rapora media bilgisi ekle
        report['media_attached'] = bool(zip_bytes)
        report['media_extracted'] = media_extracted
        if hasattr(self, '_media_extract_error') and self._media_extract_error:
            report.setdefault('warnings', []).append(
                f"Media çıkarma hatası: {self._media_extract_error}"
            )
        return report

    # --------------------------------------------------------------------------
    #  Barkodlu Ürün Paketi Restore
    # --------------------------------------------------------------------------
    def restore_gold_purchases(self, payload, user=None, dry_run=False):
        """
        Karar 1: Merge mode.
          - Tedarikçi yoksa oluştur, varsa eşleştir (Karar 5)
          - Müşteri yoksa oluştur, varsa eşleştir
          - Barkod çakışmasını çöz
          - Idempotency check ile çift yazımı engelle
        """
        from apps.products.models import Products
        from apps.gold_purchases.models import GoldPurchases
        from apps.suppliers.models import Suppliers, SupplierLedger
        from apps.customers.models import Customers, CustomerLedger
        from apps.process.models import Process

        items = payload.get('items', [])
        report = self._init_report(dry_run)

        # Dry-run: hiçbir yazım yapma
        if dry_run:
            for item in items:
                self._dry_run_gold_purchase_item(item, report)
            return report

        # Gerçek restore
        try:
            with transaction.atomic():
                for item in items:
                    self._apply_gold_purchase_item(item, report, user)
            report['success'] = True
        except Exception as e:
            report['success'] = False
            report['error'] = str(e)
            report['traceback'] = traceback.format_exc()
        return report

    def _dry_run_gold_purchase_item(self, item, report):
        """Sadece raporlama — mevcut DB'ye dokunmaz."""
        meta = item.get('_meta', {})
        idem = meta.get('idempotency_key', '')

        if idem and ConflictResolver.is_already_restored(idem):
            report['idempotency_skips'].append({
                'barcode': meta.get('barcode', ''),
                'idempotency_key': idem,
            })
            return

        # Tedarikçi
        sup_data = item.get('supplier')
        if sup_data:
            match, warnings = self.resolver.match_supplier(sup_data)
            if match:
                report['will_match_suppliers'] += 1
            else:
                report['will_create_suppliers'] += 1
                if warnings:
                    report['similarity_warnings'].extend([
                        {'kind': 'supplier', 'incoming': sup_data.get('company_name', ''), 'matches': warnings}
                    ])

        # Barkod çakışma
        product_data = item.get('product') or {}
        barcode = product_data.get('barcode', '')
        if barcode:
            new_barcode, was_changed = self.resolver.resolve_barcode_conflict(barcode)
            if was_changed:
                report['barcode_conflicts'].append({
                    'original': barcode, 'resolved': new_barcode,
                })

        # Müşteri (satıldıysa)
        sale_info = item.get('sale_info')
        if sale_info and sale_info.get('customer'):
            cust_data = sale_info['customer']
            match, warnings = self.resolver.match_customer(cust_data)
            if match:
                report['will_match_customers'] += 1
            else:
                report['will_create_customers'] += 1
                if warnings:
                    report['similarity_warnings'].append({
                        'kind': 'customer',
                        'incoming': f"{cust_data.get('first_name', '')} {cust_data.get('last_name', '')}".strip(),
                        'matches': warnings,
                    })

        report['will_create_products'] += 1

    def _apply_gold_purchase_item(self, item, report, user):
        """Gerçek yazım — atomic block içinde çağrılır."""
        from apps.products.models import Products
        from apps.gold_purchases.models import GoldPurchases
        from apps.definitions.categories.models import Categories
        from apps.suppliers.models import Suppliers, SupplierLedger
        from apps.customers.models import Customers
        from apps.process.models import Process
        from apps.backups.models import RestoreAuditLog

        meta = item.get('_meta', {})
        idem = meta.get('idempotency_key', '')

        # Idempotency
        if idem and ConflictResolver.is_already_restored(idem):
            report['idempotency_skips'].append({
                'barcode': meta.get('barcode', ''),
                'idempotency_key': idem,
            })
            return

        # 1) Tedarikçi
        sup_data = item.get('supplier')
        supplier_obj = None
        sup_warnings = []
        if sup_data:
            matched, sup_warnings = self.resolver.match_supplier(sup_data)
            if matched:
                supplier_obj = matched
                report['matched_suppliers'] += 1
            else:
                supplier_obj = Suppliers.objects.create(
                    store=self.store,
                    company_name=sup_data.get('company_name', ''),
                    person_name=sup_data.get('person_name', ''),
                    person_surname=sup_data.get('person_surname', ''),
                    phone=sup_data.get('phone', ''),
                    email=sup_data.get('email', ''),
                    tax_number=sup_data.get('tax_number', ''),
                    tax_payer_type=sup_data.get('tax_payer_type', 'CORPORATE'),
                    account_type=sup_data.get('account_type', 'SUPPLIER'),
                    company_address=sup_data.get('company_address', ''),
                    is_active=True,
                    is_deleted=False,
                )
                report['created_suppliers'] += 1
            self.mapper.add('Suppliers', sup_data.get('id'), supplier_obj.id)

        # 2) Kategori (ürün için)
        # Products.category → Categories (apps.definitions.categories), store alanı yok.
        product_data = item.get('product') or {}
        category_name = (product_data.get('category_name') or '').strip()
        category_obj = None
        if category_name:
            category_obj, _ = Categories.objects.get_or_create(
                name=category_name,
                defaults={'is_active': True, 'is_deleted': False, 'order': 0},
            )

        # 3) Barkod çakışma
        original_barcode = product_data.get('barcode', '')
        final_barcode, was_changed = self.resolver.resolve_barcode_conflict(original_barcode)
        if was_changed:
            report['barcode_conflicts'].append({
                'original': original_barcode, 'resolved': final_barcode,
            })

        # 4) Products
        # Görsel alanı: paket ZIP ise media/ çıkarıldıktan sonra path geçerli
        # olur. Saf JSON pakette path saklanır ama dosya yoksa ImageField
        # boş gibi davranır (Django ImageField path'i veritabanına yazar,
        # dosya kontrolü açıldığında yapılır).
        img_path = (product_data.get('image_path') or '').strip()
        if img_path in DEFAULT_IMAGE_PATHS or not img_path:
            img_path = ''

        # FAZ 65.1 — 1.05 EŞİK NORMALIZASYONU (SSOT, FAZ 44 ile aynı kural):
        # Eski sistem (FAZ 34 öncesi) Products.buy_price_hs alanını TOPLAM HAS
        # olarak tutuyordu. Restore edilen paket bu legacy değeri taşıyor olabilir.
        # Yeni sistemde alan BİRİM HAS (HS/gram) bekliyor; saf altın fraksiyonu
        # ≤ 1.000 olduğundan 1.05 üzeri değer kesinlikle legacy toplamdır.
        # Bunu disk'ten doğru girdir → sonraki self-heal/perakende akışları zaten
        # snapshot'ı ürünün buy_price_hs'inden okuyor.
        from decimal import ROUND_HALF_UP as _RHU
        _gram_dec = _to_decimal(product_data.get('gram'))
        _buy_hs_raw = _to_decimal(product_data.get('buy_price_hs'))
        _buy_tl_raw = _to_decimal(product_data.get('buy_price_eur'))
        _buy_hs_norm = _buy_hs_raw
        _buy_tl_norm = _buy_tl_raw
        if _buy_hs_raw is not None and _gram_dec is not None and _gram_dec > 0 and _buy_hs_raw > Decimal('1.05'):
            _buy_hs_norm = (_buy_hs_raw / _gram_dec).quantize(
                Decimal('0.0001'), rounding=_RHU
            )
            if _buy_tl_raw is not None and _buy_tl_raw > 0:
                _buy_tl_norm = (_buy_tl_raw / _gram_dec).quantize(
                    Decimal('0.01'), rounding=_RHU
                )

        product_kwargs = dict(
            store=self.store,
            barcode=final_barcode,
            name=product_data.get('name', ''),
            jewelry_type=product_data.get('jewelry_type', ''),
            material_type=product_data.get('material_type', 'GOLD'),
            brand=product_data.get('brand', ''),
            gold_dry=_to_decimal(product_data.get('gold_dry')),
            gold_rate=_to_decimal(product_data.get('gold_rate')),
            product_mileage=_to_decimal(product_data.get('product_mileage')),
            labor_mileage=_to_decimal(product_data.get('labor_mileage')),
            piece_labor=_to_decimal(product_data.get('piece_labor')),
            gram=_gram_dec,
            sale_price_hs=_to_decimal(product_data.get('sale_price_hs')),
            sale_price_eur=_to_decimal(product_data.get('sale_price_eur')),
            buy_price_hs=_buy_hs_norm,
            buy_price_eur=_buy_tl_norm,
            product_hs=_to_decimal(product_data.get('product_hs')),
            is_currency=bool(product_data.get('is_currency', False)),
            is_scrap=bool(product_data.get('is_scrap', False)),
            is_gram_bullion=bool(product_data.get('is_gram_bullion', False)),
            is_completed=bool(product_data.get('is_completed', False)),
            is_active=True,
            is_deleted=False,
            category=category_obj,
            created_by=user,
        )
        # Görsel: sadece ZIP'ten çıkarılmış gerçek dosya varsa veya
        # paket JSON ise (path metadata için) atanır.
        if img_path:
            abs_p = Path(dj_settings.MEDIA_ROOT) / img_path
            # ZIP'ten çıkarıldıysa dosya artık mevcut → path'i ata.
            # JSON paket ise dosya yok ama path bilgisi korunur (manuel taşıma).
            if abs_p.exists() or self._zip_has_media:
                product_kwargs['image'] = img_path

        # Restore verisi kendi sistemimizden geldiği için full_clean() atlanır.
        # Products.save() skip_validation=True ile UI validasyonunu (max_length,
        # clean() hook vb.) devre dışı bırakır; DB kısıtları hâlâ geçerlidir.
        # Barkod zaten ConflictResolver tarafından BARCODE_MAX_LEN'e sığdırıldı.
        product_obj = Products(**product_kwargs)
        product_obj.save(skip_validation=True)
        self.mapper.add('Products', product_data.get('id'), product_obj.id)
        report['created_products'] += 1
        if img_path and self._zip_has_media:
            report.setdefault('attached_product_images', 0)
            report['attached_product_images'] += 1

        # 5) GoldPurchases (envanter kaydı)
        if supplier_obj or product_obj:
            GoldPurchases.objects.create(
                store=self.store,
                product=product_obj,
                supplier=supplier_obj,
                created_by=user,
                count_is_status=True,
                is_status=True,
                is_active=True,
                is_deleted=False,
                is_labeled=False,
            )
            report['created_gold_purchases'] += 1

        # 6) SupplierLedger giriş satırları
        if supplier_obj:
            for sl_entry in item.get('purchase_ledger_entries', []):
                desc = (sl_entry.get('description') or sl_entry.get('notes') or '') + \
                       f" (Yedekten Yüklendi: {timezone.now().strftime('%d.%m.%Y')} - Smart Restore)"
                try:
                    SupplierLedger.objects.create(
                        supplier=supplier_obj,
                        product=product_obj,
                        transaction_type=sl_entry.get('transaction_type', 'ENTRY'),
                        quantity_piece=_to_decimal(sl_entry.get('quantity_piece')) or Decimal('0'),
                        quantity_gram=_to_decimal(sl_entry.get('quantity_gram')) or Decimal('0'),
                        amount_value=_to_decimal(sl_entry.get('amount_value')) or Decimal('0'),
                        currency=sl_entry.get('currency', 'HS'),
                        process_no=sl_entry.get('process_no', ''),
                        description=desc,
                        is_active=True,
                    )
                    report['created_supplier_ledger_entries'] += 1
                except Exception:
                    # Tek satır hatası tüm akışı bozmasın — atla, raporla
                    report['warnings'].append(
                        f"SupplierLedger satırı yazılamadı: {sl_entry.get('process_no', '')}"
                    )

        # 7) Müşteri (satılmışsa)
        sale_info = item.get('sale_info')
        customer_obj = None
        if sale_info and sale_info.get('customer'):
            cust_data = sale_info['customer']
            matched, cust_warnings = self.resolver.match_customer(cust_data)
            if matched:
                customer_obj = matched
                report['matched_customers'] += 1
            else:
                customer_obj = Customers.objects.create(
                    first_name=cust_data.get('first_name', ''),
                    last_name=cust_data.get('last_name', ''),
                    identification_number=cust_data.get('identification_number', ''),
                    customer_number=cust_data.get('customer_number', ''),
                    phone=cust_data.get('phone', ''),
                    email=cust_data.get('email', ''),
                    address=cust_data.get('address', ''),
                    is_active=True,
                    is_deleted=False,
                )
                customer_obj.store.add(self.store)
                report['created_customers'] += 1
            if cust_warnings:
                report['similarity_warnings'].append({
                    'kind': 'customer',
                    'incoming': f"{cust_data.get('first_name', '')} {cust_data.get('last_name', '')}".strip(),
                    'matches': cust_warnings,
                })
            self.mapper.add('Customers', cust_data.get('id'), customer_obj.id)

            # 8) Satış Process (kaydı oluştur — stok hareketi YAZILMAZ, audit için)
            proc_data = sale_info.get('process', {})
            try:
                sale_proc = Process.objects.create(
                    store=self.store,
                    customer=customer_obj,
                    product=product_obj,
                    process_no=proc_data.get('process_no', ''),
                    transaction_type='SALE',
                    process_type=proc_data.get('process_type', 'RETAIL'),
                    piece=_to_decimal(proc_data.get('piece')) or Decimal('1'),
                    gram=_to_decimal(proc_data.get('gram')) or Decimal('0'),
                    price_hs=_to_decimal(proc_data.get('price_hs')) or Decimal('0'),
                    amount=_to_decimal(proc_data.get('amount')) or Decimal('0'),
                    unit_price=_to_decimal(proc_data.get('unit_price')) or Decimal('0'),
                    is_status=True,
                    is_deleted=False,
                )
                report['created_processes'] += 1
            except Exception:
                report['warnings'].append(
                    f"Process kaydı yazılamadı: {proc_data.get('process_no', '')}"
                )

            # 9) CustomerLedger satırları
            for cl_entry in sale_info.get('customer_ledger_entries', []):
                desc = (cl_entry.get('description') or '') + \
                       f" (Yedekten Yüklendi: {timezone.now().strftime('%d.%m.%Y')} - Smart Restore)"
                try:
                    CustomerLedger.objects.create(
                        customer=customer_obj,
                        store=self.store,
                        transaction_type=cl_entry.get('transaction_type', 'DEBT'),
                        amount_hs=_to_decimal(cl_entry.get('amount_hs')) or Decimal('0'),
                        amount_eur=_to_decimal(cl_entry.get('amount_eur')) or Decimal('0'),
                        currency=cl_entry.get('currency', 'HS'),
                        exchange_rate_eur=_to_decimal(cl_entry.get('exchange_rate_eur')),
                        process_no=cl_entry.get('process_no', ''),
                        description=desc,
                        is_active=True,
                        created_by=user,
                    )
                    report['created_customer_ledger_entries'] += 1
                except Exception:
                    report['warnings'].append(
                        f"CustomerLedger satırı yazılamadı: {cl_entry.get('process_no', '')}"
                    )

        # 10) RestoreAuditLog (her item için ayrı satır)
        if self.backup_id:
            try:
                RestoreAuditLog.objects.create(
                    backup_id=self.backup_id,
                    restore_type='SMART',
                    content_type=ContentType.objects.get_for_model(Products),
                    object_id=product_obj.id,
                    idempotency_key=idem,
                    original_created_at=None,
                    original_created_by='',
                    restored_by=user,
                    restore_notes=(
                        f"Smart Restore — Barkod: {final_barcode} "
                        f"({'satıldı' if product_obj.is_completed else 'aktif'}). "
                        f"Tedarikçi: {'eşleşti' if supplier_obj and not (sup_data and self.resolver.match_supplier(sup_data)[0] is None) else 'yeni'}."
                    ),
                    similarity_warnings={
                        'supplier': sup_warnings or [],
                        'customer': cust_warnings if 'cust_warnings' in locals() and customer_obj else [],
                    },
                )
            except Exception:
                pass  # audit log hatası akışı durdurmasın

    # --------------------------------------------------------------------------
    #  Müşteri Paketi Restore
    # --------------------------------------------------------------------------
    def restore_customers(self, payload, user=None, dry_run=False):
        from apps.customers.models import Customers, CustomerLedger
        from apps.backups.models import RestoreAuditLog

        items = payload.get('items', [])
        report = self._init_report(dry_run)

        if dry_run:
            for item in items:
                cust_data = item.get('customer') or {}
                idem = item.get('_meta', {}).get('idempotency_key', '')
                if idem and ConflictResolver.is_already_restored(idem):
                    report['idempotency_skips'].append({'idempotency_key': idem})
                    continue
                matched, warnings = self.resolver.match_customer(cust_data)
                if matched:
                    report['will_match_customers'] += 1
                else:
                    report['will_create_customers'] += 1
                    if warnings:
                        report['similarity_warnings'].append({
                            'kind': 'customer',
                            'incoming': f"{cust_data.get('first_name', '')} {cust_data.get('last_name', '')}".strip(),
                            'matches': warnings,
                        })
                report['will_create_ledger_entries'] += len(item.get('ledger_entries', []))
            return report

        try:
            with transaction.atomic():
                for item in items:
                    cust_data = item.get('customer') or {}
                    idem = item.get('_meta', {}).get('idempotency_key', '')

                    if idem and ConflictResolver.is_already_restored(idem):
                        report['idempotency_skips'].append({'idempotency_key': idem})
                        continue

                    matched, warnings = self.resolver.match_customer(cust_data)
                    if matched:
                        customer_obj = matched
                        report['matched_customers'] += 1
                    else:
                        # Kimlik görselleri — ZIP'ten çıkarılmışsa path geçerli
                        identity_imgs = item.get('identity_images') or {}
                        front_path = (identity_imgs.get('front') or '').strip()
                        back_path = (identity_imgs.get('back') or '').strip()
                        if front_path in DEFAULT_IMAGE_PATHS:
                            front_path = ''
                        if back_path in DEFAULT_IMAGE_PATHS:
                            back_path = ''

                        cust_kwargs = dict(
                            first_name=cust_data.get('first_name', ''),
                            last_name=cust_data.get('last_name', ''),
                            identification_number=cust_data.get('identification_number', ''),
                            customer_number=cust_data.get('customer_number', ''),
                            phone=cust_data.get('phone', ''),
                            email=cust_data.get('email', ''),
                            address=cust_data.get('address', ''),
                            is_active=True, is_deleted=False,
                        )
                        if front_path:
                            abs_p = Path(dj_settings.MEDIA_ROOT) / front_path
                            if abs_p.exists() or self._zip_has_media:
                                cust_kwargs['identification_front_image'] = front_path
                        if back_path:
                            abs_p = Path(dj_settings.MEDIA_ROOT) / back_path
                            if abs_p.exists() or self._zip_has_media:
                                cust_kwargs['identification_back_image'] = back_path

                        customer_obj = Customers.objects.create(**cust_kwargs)
                        customer_obj.store.add(self.store)
                        report['created_customers'] += 1
                        if (front_path or back_path) and self._zip_has_media:
                            report.setdefault('attached_customer_images', 0)
                            report['attached_customer_images'] += int(bool(front_path)) + int(bool(back_path))

                    self.mapper.add('Customers', cust_data.get('id'), customer_obj.id)

                    # Tüm ledger satırlarını yaz (Karar 3: müşteri paketi tam geçmiş)
                    for cl_entry in item.get('ledger_entries', []):
                        desc = (cl_entry.get('description') or '') + \
                               f" (Yedekten Yüklendi: {timezone.now().strftime('%d.%m.%Y')} - Smart Restore)"
                        try:
                            CustomerLedger.objects.create(
                                customer=customer_obj,
                                store=self.store,
                                transaction_type=cl_entry.get('transaction_type', 'DEBT'),
                                amount_hs=_to_decimal(cl_entry.get('amount_hs')) or Decimal('0'),
                                amount_eur=_to_decimal(cl_entry.get('amount_eur')) or Decimal('0'),
                                currency=cl_entry.get('currency', 'HS'),
                                exchange_rate_eur=_to_decimal(cl_entry.get('exchange_rate_eur')),
                                process_no=cl_entry.get('process_no', ''),
                                description=desc,
                                is_active=bool(cl_entry.get('is_active', True)),
                                created_by=user,
                            )
                            report['created_ledger_entries'] += 1
                        except Exception:
                            report['warnings'].append(
                                f"CustomerLedger satırı yazılamadı: {cl_entry.get('process_no', '')}"
                            )

                    # Audit
                    if self.backup_id:
                        try:
                            from django.contrib.contenttypes.models import ContentType
                            RestoreAuditLog.objects.create(
                                backup_id=self.backup_id,
                                restore_type='SMART',
                                content_type=ContentType.objects.get_for_model(Customers),
                                object_id=customer_obj.id,
                                idempotency_key=idem,
                                restored_by=user,
                                restore_notes=(
                                    f"Smart Restore — Müşteri: "
                                    f"{customer_obj.first_name} {customer_obj.last_name} "
                                    f"({len(item.get('ledger_entries', []))} hareket)."
                                ),
                                similarity_warnings={'customer': warnings} if warnings else {},
                            )
                        except Exception:
                            pass

            report['success'] = True
        except Exception as e:
            report['success'] = False
            report['error'] = str(e)
            report['traceback'] = traceback.format_exc()
        return report

    # --------------------------------------------------------------------------
    #  Mağaza Ayarları Restore
    # --------------------------------------------------------------------------
    def restore_settings(self, payload, user=None, dry_run=False):
        from apps.settings.models import StoreConfiguration, StoreLabelSettings
        from apps.banking.models import BankAccount
        from apps.gold_purchases.models import ProductCategory, BarcodeTemplate
        from apps.suppliers.models import Suppliers
        from apps.backups.models import RestoreAuditLog

        report = self._init_report(dry_run)
        idem = payload.get('idempotency_key', '')

        if dry_run:
            if idem and ConflictResolver.is_already_restored(idem):
                report['idempotency_skips'].append({'idempotency_key': idem})
            else:
                report['will_apply_settings'] = True
                report['will_create_categories'] = len(payload.get('product_categories', []))
                report['will_create_bank_accounts'] = len(payload.get('bank_accounts', []))
                report['will_create_barcode_templates'] = len(payload.get('barcode_templates', []))
            return report

        try:
            with transaction.atomic():
                if idem and ConflictResolver.is_already_restored(idem):
                    report['idempotency_skips'].append({'idempotency_key': idem})
                    report['success'] = True
                    return report

                # 1) StoreConfiguration (upsert)
                config_data = payload.get('configuration')
                if config_data:
                    sc, _ = StoreConfiguration.objects.get_or_create(store=self.store)
                    for field in [
                        'language_code', 'base_spot_currency', 'base_spot_unit',
                        'is_safe_approval_required',
                        'enforce_customer_always', 'require_customer_phone',
                        'debt_currency_mode',
                        'allow_overpayment_default',
                    ]:
                        if field in config_data and hasattr(sc, field):
                            try:
                                setattr(sc, field, config_data[field])
                            except Exception:
                                pass
                    sc.save()
                    report['applied_configuration'] = True

                # 2) StoreLabelSettings (upsert)
                label_data = payload.get('label_settings')
                if label_data:
                    sls, _ = StoreLabelSettings.objects.get_or_create(store=self.store)
                    for field in [
                        'active_size', 'small_design', 'large_design',
                        'diamond_small_design', 'diamond_large_design',
                        'watch_small_design', 'watch_large_design',
                        'label_bottom_left_type', 'label_layout_mode', 'rfid_mode',
                    ]:
                        if field in label_data and hasattr(sls, field):
                            try:
                                setattr(sls, field, label_data[field])
                            except Exception:
                                pass
                    sls.save()
                    report['applied_label_settings'] = True

                # 3) BankAccount (mevcut isim+iban kombosu varsa atla)
                for ba_data in payload.get('bank_accounts', []):
                    name = ba_data.get('name', '')
                    iban = ba_data.get('iban', '')
                    exists = BankAccount.objects.filter(
                        store=self.store, name=name, iban=iban
                    ).exists()
                    if exists:
                        continue
                    BankAccount.objects.create(
                        store=self.store,
                        name=name,
                        bank_name=ba_data.get('bank_name', ''),
                        iban=iban,
                        currency=ba_data.get('currency', 'TRY'),
                        account_type=ba_data.get('account_type', 'CASH'),
                        reconciliation_tolerance=_to_decimal(ba_data.get('reconciliation_tolerance')) or Decimal('0'),
                        is_inter_branch_transit_account=bool(ba_data.get('is_inter_branch_transit_account', False)),
                        is_active=True, is_deleted=False,
                    )
                    report['created_bank_accounts'] += 1

                # 4) ProductCategory (upsert by name)
                for c_data in payload.get('product_categories', []):
                    name = c_data.get('name', '')
                    if not name:
                        continue
                    _, was_created = ProductCategory.objects.get_or_create(
                        store=self.store,
                        name=name,
                        defaults={
                            'barcode_prefix': c_data.get('barcode_prefix', ''),
                            'is_active': True, 'is_deleted': False,
                        },
                    )
                    if was_created:
                        report['created_categories'] += 1

                # 5) BarcodeTemplate
                for bt_data in payload.get('barcode_templates', []):
                    sup_tax = bt_data.get('supplier_tax_number', '')
                    sup_obj = None
                    if sup_tax:
                        sup_obj = Suppliers.objects.filter(
                            store=self.store, tax_number=sup_tax, is_deleted=False
                        ).first()
                    try:
                        BarcodeTemplate.objects.create(
                            store=self.store,
                            material_type=bt_data.get('material_type', 'GOLD'),
                            jewelry_type=bt_data.get('jewelry_type', ''),
                            gold_rate=_to_decimal(bt_data.get('gold_rate')),
                            product_mileage=_to_decimal(bt_data.get('product_mileage')),
                            labor_mileage=_to_decimal(bt_data.get('labor_mileage')),
                            piece_labor=_to_decimal(bt_data.get('piece_labor')),
                            ring_size=_to_decimal(bt_data.get('ring_size')),
                            supplier=sup_obj,
                            extra_data=bt_data.get('extra_data', {}) or {},
                            is_active=True, is_deleted=False,
                        )
                        report['created_barcode_templates'] += 1
                    except Exception:
                        report['warnings'].append(
                            f"BarcodeTemplate yazılamadı: {bt_data.get('jewelry_type', '')}"
                        )

                # 6) Audit
                if self.backup_id:
                    try:
                        RestoreAuditLog.objects.create(
                            backup_id=self.backup_id,
                            restore_type='SMART',
                            idempotency_key=idem,
                            restored_by=user,
                            restore_notes=(
                                f"Smart Restore — Mağaza Ayarları: "
                                f"config={'✓' if report['applied_configuration'] else '✗'}, "
                                f"labels={'✓' if report['applied_label_settings'] else '✗'}, "
                                f"+{report['created_bank_accounts']} kasa, "
                                f"+{report['created_categories']} kategori, "
                                f"+{report['created_barcode_templates']} şablon."
                            ),
                        )
                    except Exception:
                        pass

            report['success'] = True
        except Exception as e:
            report['success'] = False
            report['error'] = str(e)
            report['traceback'] = traceback.format_exc()
        return report

    # --------------------------------------------------------------------------
    #  Yardımcı: Boş rapor iskeleti
    # --------------------------------------------------------------------------
    @staticmethod
    def _init_report(dry_run):
        return {
            'success': None,
            'dry_run': dry_run,
            'created_suppliers': 0,
            'matched_suppliers': 0,
            'created_customers': 0,
            'matched_customers': 0,
            'created_products': 0,
            'created_gold_purchases': 0,
            'created_processes': 0,
            'created_supplier_ledger_entries': 0,
            'created_customer_ledger_entries': 0,
            'created_ledger_entries': 0,
            'created_bank_accounts': 0,
            'created_categories': 0,
            'created_barcode_templates': 0,
            'will_create_suppliers': 0,
            'will_match_suppliers': 0,
            'will_create_customers': 0,
            'will_match_customers': 0,
            'will_create_products': 0,
            'will_create_ledger_entries': 0,
            'will_apply_settings': False,
            'will_create_bank_accounts': 0,
            'will_create_categories': 0,
            'will_create_barcode_templates': 0,
            'applied_configuration': False,
            'applied_label_settings': False,
            'idempotency_skips': [],
            'barcode_conflicts': [],
            'similarity_warnings': [],
            'warnings': [],
            'error': None,
        }
