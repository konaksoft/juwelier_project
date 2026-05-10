"""
==============================================================================
 FAZ A + FAZ B — Yedekleme & Tam Geri Yükleme Servisi
==============================================================================

Tarih: 2026-05-05
FAZ A: Mevcut sistemi onar — eksik 11 model + ordered restore + parent-first.
FAZ B: Format çeşitliliği — ZIP + multi-file JSON + opsiyonel media.

Mimar Notları:
  Bu servis YALNIZCA "Tam Firma Yedeği" (Full Backup) ve "Tam Geri Yükleme"
  (Full Restore = Wipe & Load) için kullanılır. Smart Export / Smart Restore
  ileriki fazlarda (FAZ C, D) ayrı servislerde implemente edilecektir.

Public API:
  - BackupService(company_id)
      .create_backup(note, user)                          # FAZ A: tek dosyalı JSON
      .create_backup_zip(note, user, include_media=False) # FAZ B: ZIP (+media opsiyonel)
      .restore_backup(backup_id, user)                    # ZIP/JSON otomatik tespit
==============================================================================
"""

import hashlib
import io
import json
import traceback
import zipfile
from collections import OrderedDict

from django.conf import settings
from django.core import serializers
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

# --- Modellerin İmport Edilmesi -----------------------------------------------
from apps.bracelets.models import Bracelets
from apps.custody.models import CustomerCustodyLedger
from apps.customers.models import Customers, CustomerLedger
from apps.gold_purchases.models import GoldPurchases, ProductCategory, BarcodeTemplate
from apps.stock_management.models import StockSnapshot, StockLedger
# Invoice, Pavo modelleri kaldırıldı — invoices ve pavo app Juwelier Plus'ta yok
from apps.process.models import Process, Payment, ProcessGroup
from apps.repairs.models import Repairs
from apps.suppliers.models import Suppliers, SupplierLedger
from apps.workshops.models import Workshops
from apps.accounts.models import Users, ContactConsent, OtpCode
from apps.roles.models import Roles, RoleDetail
from apps.stores.models import (
    Company, Stores, StorePriceCache,
)
from apps.banking.models import (
    BankAccount, BankTransaction, CashboxLedger, IncomeExpenseLedger,
)
from apps.settings.models import StoreConfiguration, StoreLabelSettings
from apps.products.models import Products

# Pırlanta / Saat detayları (opsiyonel)
try:
    from apps.products.models import DiamondDetail, DiamondStone, WatchDetail
    DIAMOND_WATCH_AVAILABLE = True
except ImportError:
    DIAMOND_WATCH_AVAILABLE = False

# Hurda (opsiyonel)
try:
    from apps.scraps.models import Scraps
    SCRAPS_AVAILABLE = True
except ImportError:
    SCRAPS_AVAILABLE = False

# Sayım (opsiyonel)
try:
    from apps.counts.models import InventoryCountSession, InventoryCountItem
    COUNTS_AVAILABLE = True
except ImportError:
    COUNTS_AVAILABLE = False

# FAZ B — Media packager
from apps.backups.media_packager import MediaPackager


# ==============================================================================
#  YARDIMCI FONKSİYONLAR (modül seviyesi)
# ==============================================================================

def _filter_active(queryset):
    """
    Modelin alanları arasında 'is_deleted' varsa is_deleted=False filtresi uygular.
    Ledger satırları için bu helper KULLANILMAZ — Karar 3 gereği yasal saklama
    için tüm satırlar (silinmiş bile olsa) yedeklenir.
    """
    try:
        field_names = [f.name for f in queryset.model._meta.get_fields()]
        if 'is_deleted' in field_names:
            return queryset.filter(is_deleted=False)
        return queryset
    except Exception:
        return queryset


def _ordered_self_ref(qs, parent_field='parent'):
    """
    [FAZ A.3] Self-referencing append-only ledger satırları için
    parent-first sıralama. parent=None önce, REVERSAL satırları sonra.
    """
    try:
        null_filter = {f"{parent_field}__isnull": True}
        not_null_filter = {f"{parent_field}__isnull": False}
        parents = list(qs.filter(**null_filter).order_by('created_on'))
        children = list(qs.filter(**not_null_filter).order_by('created_on'))
        return parents + children
    except Exception:
        return list(qs)


def _delete_self_ref_safely(model_cls, **filter_kwargs):
    """
    [FAZ A.2] Self-ref PROTECT zincirini bozmadan silmek için:
    önce child rows (parent IS NOT NULL), sonra parent rows (parent=None).
    """
    try:
        model_cls.objects.filter(parent__isnull=False, **filter_kwargs).delete()
        model_cls.objects.filter(parent__isnull=True, **filter_kwargs).delete()
    except Exception:
        model_cls.objects.filter(**filter_kwargs).delete()


def _wipe_cashbox_by_ids(cb_root_ids):
    """
    Verilen CashboxLedger ID listesi (root/parent kayıtlar) ve bunların
    TÜM çocuklarını (REVERSAL vb.) store/banka ayrımı gözetmeksizin siler.

    PROTECT self-ref (parent→self) nedeniyle önce yaprak, sonra kök silinir.
    3 seviyeye kadar derinliği destekler (yeterli).
    """
    if not cb_root_ids:
        return
    ids = set(cb_root_ids)

    # 2. seviye (direct children)
    lvl2 = set(CashboxLedger.objects.filter(parent__in=ids).values_list('id', flat=True))
    # 3. seviye (grandchildren)
    lvl3 = set(CashboxLedger.objects.filter(parent__in=lvl2).values_list('id', flat=True)) if lvl2 else set()
    # 4. seviye (safety net)
    lvl4 = set(CashboxLedger.objects.filter(parent__in=lvl3).values_list('id', flat=True)) if lvl3 else set()

    if lvl4:
        CashboxLedger.objects.filter(id__in=lvl4).delete()
    if lvl3:
        CashboxLedger.objects.filter(id__in=lvl3).delete()
    if lvl2:
        CashboxLedger.objects.filter(id__in=lvl2).delete()
    CashboxLedger.objects.filter(id__in=ids).delete()


def _wipe_cashbox_for_stores(stores_qs):
    """
    Mağazaların tüm CashboxLedger kayıtlarını güvenle temizler.

    Sadece store FK filtresi YETMİYOR çünkü REVERSAL (child) kayıtları
    farklı store'a veya NULL store ile yazılmış olabilir. Bu fonksiyon
    üç ayrı eksenden kesişim alır:

      1. CashboxLedger.store   → doğrudan mağaza
      2. CashboxLedger.bank_account → mağazanın banka hesapları
      3. CashboxLedger.related_payment → mağazanın Payment'ları

    Her üç kaynaktan toplanan root ID'lerin TÜM çocukları (_wipe_cashbox_by_ids)
    ile birlikte silinir.
    """
    # 1) store bazlı root ID'ler
    root_ids = set(CashboxLedger.objects.filter(store__in=stores_qs).values_list('id', flat=True))

    # 2) BankAccount bazlı — store FK yanlış/NULL olan eski kayıtlar
    # (CashboxLedger.cashbox → BankAccount; field adı 'cashbox', 'bank_account' değil)
    ba_ids = list(BankAccount.objects.filter(store__in=stores_qs).values_list('id', flat=True))
    if ba_ids:
        root_ids |= set(
            CashboxLedger.objects.filter(cashbox__in=ba_ids).values_list('id', flat=True)
        )

    # 3) Payment bazlı — Payment.store FK YOK; bank_account/process_group/process_no
    # üzerinden mağaza Payment'larını topla
    from apps.process.models import Process
    _pay_filter = Q(process_group__store__in=stores_qs)
    if ba_ids:
        _pay_filter |= Q(bank_account_id__in=ba_ids)
    _proc_nos = list(
        Process.objects.filter(store__in=stores_qs)
        .exclude(process_no__isnull=True).exclude(process_no__exact='')
        .values_list('process_no', flat=True).distinct()
    )
    if _proc_nos:
        _pay_filter |= Q(process_no__in=_proc_nos)
    pay_ids = list(Payment.objects.filter(_pay_filter).values_list('id', flat=True))
    if pay_ids:
        root_ids |= set(
            CashboxLedger.objects.filter(related_payment_id__in=pay_ids).values_list('id', flat=True)
        )

    _wipe_cashbox_by_ids(list(root_ids))


# ==============================================================================
#  ANA SERVİS SINIFI
# ==============================================================================

class BackupService:
    """
    Tam Firma Yedek Alma & Tam Geri Yükleme servisi.
    """

    # FAZ B — Section sırası. ZIP içindeki dosyalar bu sırayla concat edilir
    # ve restore'da sırayla yüklenir. FK bağımlılığını korur.
    SECTIONS = [
        '01_foundation',
        '02_users',
        '03_stakeholders',
        '04_products',
        '05_stock',
        '06_process',
        '07_finance',
        '08_ledgers',
        '09_repairs',
        '11_counts',
    ]

    def __init__(self, company_id, store_id=None):
        """
        store_id verilirse yedek/silme yalnızca o mağazaya sınırlı kalır.
        store_id=None (varsayılan) → firma geneli tüm mağazalar.
        """
        self.company = Company.objects.get(id=company_id)
        self._store_id = store_id  # None = firma geneli

        # Yedek kapsam: aktif mağazalar
        if store_id:
            self.stores = Stores.objects.filter(
                company=self.company, id=store_id, is_deleted=False,
            )
        else:
            self.stores = Stores.objects.filter(company=self.company, is_deleted=False)

        self.store_ids = list(self.stores.values_list('id', flat=True))

    # ==========================================================================
    #  KAPSAM TOPLAMA — Section bazlı (FAZ B)
    # ==========================================================================
    def _collect_objects_by_section(self):
        """
        FK bağımlılığına göre section'lara böler. OrderedDict döner —
        section sırası FK sırasıdır.

        Returns:
            OrderedDict[str, list] — {section_name: [obj1, obj2, ...]}
        """
        sections = OrderedDict((s, []) for s in self.SECTIONS)

        # ----- 01: foundation -----
        sections['01_foundation'].append(self.company)
        sections['01_foundation'].extend(list(self.stores))

        # Mağazaya özel Roller ve yetki detayları.
        # Users.role → Roles FK (DEFERRED) olduğundan Roles mutlaka
        # Users'tan ÖNCE yüklenmelidir. stores_qs.delete() CASCADE ile
        # bu rolleri siler; backup'ta olmasa restore sırasında FK patlar.
        # RoleDetail önce silinmeli (role FK bağımlılığı) bu yüzden önce ekliyoruz.
        _store_roles_qs = Roles.objects.filter(store__in=self.store_ids)
        sections['01_foundation'].extend(list(_store_roles_qs))
        sections['01_foundation'].extend(list(
            RoleDetail.objects.filter(role__in=_store_roles_qs)
        ))

        sections['01_foundation'].extend(list(_filter_active(
            BankAccount.objects.filter(store__in=self.store_ids)
        )))
        sections['01_foundation'].extend(list(StoreConfiguration.objects.filter(store__in=self.store_ids)))
        sections['01_foundation'].extend(list(StoreLabelSettings.objects.filter(store__in=self.store_ids)))
        sections['01_foundation'].extend(list(_filter_active(StorePriceCache.objects.filter(store__in=self.store_ids))))
        # StoreEInvoiceSettings, InvoiceSequence, EInvoiceCreditRequest kaldırıldı — invoices app yok

        # ----- 02: users -----
        users = _filter_active(Users.objects.filter(store__in=self.store_ids))
        sections['02_users'].extend(list(users))
        user_ids_str = [str(u.id) for u in users]
        store_ids_str = [str(s) for s in self.store_ids]
        sections['02_users'].extend(list(_filter_active(
            ContactConsent.objects.filter(owner_type='store', owner_id__in=store_ids_str)
        )))
        sections['02_users'].extend(list(_filter_active(
            ContactConsent.objects.filter(owner_type='user', owner_id__in=user_ids_str)
        )))
        sections['02_users'].extend(list(_filter_active(
            OtpCode.objects.filter(owner_type='store', owner_id__in=store_ids_str)
        )))

        # ----- 03: stakeholders -----
        # ProductCategory önce (Products'tan önce), sonra Suppliers (BarcodeTemplate'ten önce),
        # sonra BarcodeTemplate, Customers, Workshops.
        sections['03_stakeholders'].extend(list(_filter_active(
            ProductCategory.objects.filter(store__in=self.store_ids)
        )))
        suppliers = _filter_active(Suppliers.objects.filter(store__in=self.store_ids))
        sections['03_stakeholders'].extend(list(suppliers))
        sections['03_stakeholders'].extend(list(_filter_active(
            BarcodeTemplate.objects.filter(store__in=self.store_ids)
        )))
        customers = _filter_active(Customers.objects.filter(store__in=self.store_ids).distinct())
        sections['03_stakeholders'].extend(list(customers))
        sections['03_stakeholders'].extend(list(_filter_active(
            Workshops.objects.filter(store__in=self.store_ids)
        )))

        # ----- 04: products -----
        # NOT: _filter_active KULLANILMAZ — is_deleted=True ürünler de yedeklenir.
        # Aksi hâlde StockSnapshot/StockLedger satırları soft-delete edilmiş ürünlere
        # bakar, ürün yedekte yoksa restore COMMIT'te DEFERRED FK ihlaliyle patlar.
        products = Products.objects.filter(store__in=self.store_ids)
        sections['04_products'].extend(list(products))
        if DIAMOND_WATCH_AVAILABLE:
            diamond_details = DiamondDetail.objects.filter(product__in=products)
            sections['04_products'].extend(list(diamond_details))
            sections['04_products'].extend(list(
                DiamondStone.objects.filter(diamond_detail__in=diamond_details)
            ))
            sections['04_products'].extend(list(WatchDetail.objects.filter(product__in=products)))

        # ----- 05: stock -----
        sections['05_stock'].extend(list(_filter_active(
            Bracelets.objects.filter(store__in=self.store_ids)
        )))
        if SCRAPS_AVAILABLE:
            sections['05_stock'].extend(list(_filter_active(
                Scraps.objects.filter(store__in=self.store_ids)
            )))
        sections['05_stock'].extend(list(_filter_active(
            GoldPurchases.objects.filter(store__in=self.store_ids)
        )))
        sections['05_stock'].extend(list(StockSnapshot.objects.filter(store__in=self.store_ids)))
        sections['05_stock'].extend(list(StockLedger.objects.filter(store__in=self.store_ids)))

        # ----- 06: process -----
        sections['06_process'].extend(list(
            ProcessGroup.objects.filter(store__in=self.store_ids)
        ))
        processes = _filter_active(Process.objects.filter(store__in=self.store_ids))
        sections['06_process'].extend(list(processes))
        proc_nos = list(processes.exclude(process_no__isnull=True)
                                 .exclude(process_no__exact='')
                                 .values_list('process_no', flat=True))
        if proc_nos:
            payments = _filter_active(Payment.objects.filter(process_no__in=proc_nos))
            sections['06_process'].extend(list(payments))

        # ----- 07: finance -----
        # Invoice, InvoiceItem, InvoicePaymentAllocation kaldırıldı — invoices app yok
        sections['07_finance'].extend(list(
            BankTransaction.objects.filter(store__in=self.store_ids)
        ))

        # ----- 08: ledgers (parent-first) -----
        # Karar 3: TÜM satırlar yedeklenir (yasal saklama).
        cust_ledger_qs = CustomerLedger.objects.filter(store__in=self.store_ids)
        sections['08_ledgers'].extend(_ordered_self_ref(cust_ledger_qs))

        supp_ledger_qs = SupplierLedger.objects.filter(supplier__in=suppliers)
        sections['08_ledgers'].extend(_ordered_self_ref(supp_ledger_qs))

        cash_ledger_qs = CashboxLedger.objects.filter(store__in=self.store_ids)
        sections['08_ledgers'].extend(_ordered_self_ref(cash_ledger_qs))

        ie_ledger_qs = IncomeExpenseLedger.objects.filter(store__in=self.store_ids)
        sections['08_ledgers'].extend(_ordered_self_ref(ie_ledger_qs))

        sections['08_ledgers'].extend(list(_filter_active(
            CustomerCustodyLedger.objects.filter(store__in=self.store_ids)
        )))

        # ----- 09: repairs -----
        sections['09_repairs'].extend(list(_filter_active(
            Repairs.objects.filter(store__in=self.store_ids)
        )))

        # ----- 10: pavo kaldırıldı — pavo app Juwelier Plus'ta yok -----

        # ----- 11: counts -----
        if COUNTS_AVAILABLE:
            count_sessions = _filter_active(
                InventoryCountSession.objects.filter(store__in=self.store_ids)
            )
            sections['11_counts'].extend(list(count_sessions))
            sections['11_counts'].extend(list(_filter_active(
                InventoryCountItem.objects.filter(session__in=count_sessions)
            )))

        return sections

    # ==========================================================================
    #  CREATE BACKUP — JSON (FAZ A — geriye uyum)
    # ==========================================================================
    def create_backup(self, note="", user=None):
        """
        [FAZ A] Tek dosyalı JSON yedek (geriye uyum).
        Tüm section'lar tek bir flat liste halinde JSON'a serialize edilir.

        Returns:
            CompanyBackup instance veya None.
        """
        if hasattr(self.company, 'is_deleted') and self.company.is_deleted:
            return None

        sections = self._collect_objects_by_section()
        all_objects = [obj for objs in sections.values() for obj in objs]

        json_data = serializers.serialize(
            'json', all_objects, indent=2, use_natural_foreign_keys=False
        )

        from apps.backups.models import CompanyBackup
        filename = f"backup_{self.company.id}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json"

        backup = CompanyBackup(
            company=self.company,
            note=note,
            created_by_user=str(user) if user else "System",
            status='COMPLETED',
        )
        backup.backup_file.save(filename, ContentFile(json_data))
        backup.save()
        return backup

    # ==========================================================================
    #  CREATE BACKUP — ZIP (+ opsiyonel media) — FAZ B
    # ==========================================================================
    def create_backup_zip(self, note="", user=None, include_media=False):
        """
        [FAZ B] ZIP formatında çok dosyalı yedek.

        ZIP yapısı:
            backup_FIRMAID_YYYYMMDD_HHMMSS.zip
            ├── manifest.json
            ├── db/
            │   ├── 01_foundation.json
            │   ├── 02_users.json
            │   └── ... (11 dosya)
            └── media/   ← include_media=True ise
                ├── Companies/...
                ├── Stores/...
                ├── customers/identity/...
                └── Products/CustomProducts/...

        Args:
            note: Kullanıcı notu.
            user: Yedeği alan kullanıcı (opsiyonel).
            include_media: True ise media/ dizini ZIP'e eklenir (Karar 2).

        Returns:
            CompanyBackup instance veya None.
        """
        if hasattr(self.company, 'is_deleted') and self.company.is_deleted:
            return None

        sections = self._collect_objects_by_section()

        # Manifest iskeleti
        manifest = {
            'version': '2.0',
            'backup_type': 'full_zip',
            'schema_version': 'kp_2026_05',
            'created_at': timezone.now().isoformat(),
            'company_id': str(self.company.id),
            'company_title': self.company.title or '',
            'store_count': self.stores.count(),
            'include_media': bool(include_media),
            'media_size_bytes': 0,
            'media_file_count': 0,
            'record_counts': {},
            'files': [],
        }

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            # 1) Section bazlı JSON dosyaları
            for section_name, objects in sections.items():
                json_text = serializers.serialize(
                    'json', objects, indent=2, use_natural_foreign_keys=False
                )
                json_bytes = json_text.encode('utf-8')
                file_name = f'db/{section_name}.json'

                zf.writestr(file_name, json_bytes)

                manifest['files'].append({
                    'name': file_name,
                    'sha256': hashlib.sha256(json_bytes).hexdigest(),
                    'record_count': len(objects),
                    'size_bytes': len(json_bytes),
                })
                manifest['record_counts'][section_name] = len(objects)

            # 2) Media (opsiyonel)
            if include_media:
                packager = MediaPackager(self.company, self.stores)
                stats = packager.add_to_zip(zf, base_path='media/')
                manifest['media_size_bytes'] = stats['total_bytes']
                manifest['media_file_count'] = stats['file_count']
                if stats.get('failed'):
                    manifest['media_failed'] = stats['failed']

            # 3) Manifest (en son yazılır — tüm dosyalar listelendikten sonra)
            zf.writestr(
                'manifest.json',
                json.dumps(manifest, indent=2, ensure_ascii=False).encode('utf-8'),
            )

        zip_buffer.seek(0)

        from apps.backups.models import CompanyBackup
        suffix = 'full' if include_media else 'db'
        filename = (
            f"backup_{self.company.id}_{suffix}_"
            f"{timezone.now().strftime('%Y%m%d_%H%M%S')}.zip"
        )

        backup = CompanyBackup(
            company=self.company,
            note=note,
            created_by_user=str(user) if user else "System",
            status='COMPLETED',
        )
        backup.backup_file.save(filename, ContentFile(zip_buffer.read()))
        backup.save()
        return backup

    # ==========================================================================
    #  RESTORE — JSON / ZIP otomatik tespit
    # ==========================================================================
    def restore_backup(self, backup_id, user=None):
        """
        [FAZ A + B] Yedek dosyasının formatını tespit edip uygun restore'u yapar.

        Format tespiti:
            ZIP magic (PK\\x03\\x04) ile başlıyorsa → ZIP restore.
            Aksi halde JSON (eski format) restore.

        Returns:
            (success: bool, message: str)
        """
        from apps.backups.models import CompanyBackup

        try:
            backup = CompanyBackup.objects.get(id=backup_id)
        except CompanyBackup.DoesNotExist:
            return False, "Yedek kaydı bulunamadı."

        try:
            with backup.backup_file.open('rb') as f:
                header = f.read(4)
            is_zip = header.startswith(b'PK\x03\x04')
        except Exception as e:
            return False, f"Yedek dosyası okunamadı: {e}"

        if is_zip:
            return self._restore_from_zip(backup, user)
        return self._restore_from_json(backup, user)

    # --------------------------------------------------------------------------
    #  Restore — JSON (FAZ A path)
    # --------------------------------------------------------------------------
    def _restore_from_json(self, backup, user):
        from apps.backups.models import RestoreAuditLog

        try:
            with backup.backup_file.open('r') as f:
                data = f.read()
            if isinstance(data, bytes):
                data = data.decode('utf-8')

            with transaction.atomic():
                self._delete_company_data()
                deserialized_count = self._load_backup_data(data)

                RestoreAuditLog.objects.create(
                    backup=backup,
                    restore_type='FULL',
                    restored_by=user,
                    restore_notes=(
                        f"[JSON] Tam firma geri yüklemesi tamamlandı. "
                        f"{deserialized_count} kayıt yüklendi. "
                        f"Yedek tarihi: {backup.created_at.strftime('%d.%m.%Y %H:%M')}."
                    ),
                )

            return True, (
                f"Firma verileri başarıyla temizlendi ve "
                f"{deserialized_count} kayıt geri yüklendi."
            )
        except Exception as e:
            return False, f"Restore Hatası (JSON): {str(e)}\n{traceback.format_exc()}"

    # --------------------------------------------------------------------------
    #  Restore — ZIP (FAZ B path)
    # --------------------------------------------------------------------------
    def _restore_from_zip(self, backup, user):
        """
        ZIP restore akışı:
          1) ZIP'i belleğe oku, manifest.json doğrula.
          2) Atomic transaction: delete + load DB (concat'lı JSON).
          3) Transaction commit sonrası: media dosyalarını çıkar.
          4) RestoreAuditLog'a tek toplu giriş yaz.

        Media restore TRANSACTION DIŞINDA yapılır — DB load başarısızsa
        media'ya hiç dokunulmaz; DB load başarılı ama tek tek media kopyası
        başarısız olursa toplam bütünlük korunur (media optional).
        """
        from apps.backups.models import RestoreAuditLog

        try:
            with backup.backup_file.open('rb') as f:
                zip_bytes = f.read()
        except Exception as e:
            return False, f"ZIP okunamadı: {e}"

        try:
            zip_buffer = io.BytesIO(zip_bytes)
            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                # 1) Manifest doğrula
                if 'manifest.json' not in zf.namelist():
                    return False, "Geçersiz yedek: manifest.json bulunamadı."

                with zf.open('manifest.json') as mf:
                    manifest = json.loads(mf.read().decode('utf-8'))

                # 2) DB section'larını birleştir (FK sırasına göre)
                combined_objects = []
                missing_sections = []
                for section_name in self.SECTIONS:
                    file_name = f'db/{section_name}.json'
                    if file_name not in zf.namelist():
                        missing_sections.append(section_name)
                        continue
                    with zf.open(file_name) as sf:
                        section_text = sf.read().decode('utf-8')
                    try:
                        section_objects = json.loads(section_text) if section_text.strip() else []
                    except json.JSONDecodeError as je:
                        return False, f"Bozuk yedek dosyası ({file_name}): {je}"
                    combined_objects.extend(section_objects)

                combined_json = json.dumps(combined_objects)

                # 2.b) Yedekteki mağaza ID'lerini topla — defansif temizlik için
                backup_store_ids = []
                for _obj in combined_objects:
                    if (_obj.get('model') or '').lower() == 'stores.stores':
                        _pk = _obj.get('pk')
                        if _pk:
                            backup_store_ids.append(_pk)

                # 2.c) Backup'ta Roles var mı? (yeni format → evet, eski → hayır)
                # Eski backup'larda Roles olmadığı hâlde stores_qs.delete() CASCADE
                # ile mağazaya özel Rolleri siler. Backup'tan yüklenen Users ise o
                # Role id'lerine işaret ettiği için COMMIT'te DEFERRED FK patlar.
                # Çözüm: Backup'ta Roles yoksa, mevcut Rolleri sil-önce-snapshot'la;
                # sil-sonra-yeniden-yükle — böylece Users FK'leri geçerli kalır.
                _backup_model_names = {
                    (_obj.get('model') or '').lower()
                    for _obj in combined_objects
                }
                _has_roles_in_backup = 'roles.roles' in _backup_model_names

                # Eski backup: mevcut store'a özel Rolleri şimdi serialize et
                _roles_snapshot_json = None
                if not _has_roles_in_backup:
                    from django.core import serializers as _dj_ser
                    _snap_roles = list(Roles.objects.filter(store__in=self.store_ids))
                    _snap_details = list(
                        RoleDetail.objects.filter(role__in=_snap_roles)
                    )
                    if _snap_roles:
                        # Roles önce serialize edilir; RoleDetail role FK'ye bağlı
                        # olduğundan save sırasında da bu sıra korunacak.
                        _roles_snapshot_json = serializers.serialize(
                            'json', _snap_roles + _snap_details
                        )

                # 2.d) Defansif: yedekte Products olmayan kayıtları temizle/null'la
                # (eski backup geriye uyumluluğu — FAZ 56-60 yedekleri).
                # Eski backup'larda is_deleted=True ürünler yedeklenmemişti ama
                # o ürünleri işaret eden satırlar (StockSnapshot/StockLedger/
                # InventoryCountItem/DiamondDetail/WatchDetail/StoreTransferItem...)
                # yedeğe girmişti. Restore'da Products yoksa COMMIT'te DEFERRED
                # FK ihlali patlar. Çözüm: Django registry'den Products'a FK veren
                # tüm modelleri dinamik bul; orphan referansları null'la (FK
                # nullable ise) ya da satırı at (NOT NULL ise).
                _backup_product_pks = {
                    str(_obj.get('pk'))
                    for _obj in combined_objects
                    if (_obj.get('model') or '').lower() == 'products.products'
                }

                from django.apps import apps as _django_apps
                from django.db.models import ForeignKey as _DjFK

                # {label_lower: [(field_name, is_nullable), ...]}
                _product_fk_map = {}
                for _m in _django_apps.get_models():
                    try:
                        for _f in _m._meta.get_fields():
                            if isinstance(_f, _DjFK) and _f.related_model is Products:
                                _label = _m._meta.label_lower
                                _product_fk_map.setdefault(_label, []).append(
                                    (_f.name, bool(_f.null))
                                )
                    except Exception:
                        # Bazı proxy/abstract modeller get_fields'ta sorun çıkarabilir
                        continue

                _filtered_objects = []
                _dropped_orphan = 0
                _nulled_orphan = 0
                for _obj in combined_objects:
                    _model = (_obj.get('model') or '').lower()
                    _fk_specs = _product_fk_map.get(_model)
                    if not _fk_specs:
                        _filtered_objects.append(_obj)
                        continue
                    _fields = _obj.get('fields') or {}
                    _drop = False
                    for _fk_name, _is_null in _fk_specs:
                        _pid = _fields.get(_fk_name)
                        if _pid is None:
                            continue
                        _pid_str = str(_pid)
                        if _pid_str and _pid_str not in _backup_product_pks:
                            if _is_null:
                                _fields[_fk_name] = None
                                _nulled_orphan += 1
                            else:
                                _drop = True
                                break
                    if _drop:
                        _dropped_orphan += 1
                        continue
                    _obj['fields'] = _fields
                    _filtered_objects.append(_obj)

                if _dropped_orphan or _nulled_orphan:
                    combined_objects = _filtered_objects
                    combined_json = json.dumps(combined_objects)

                # 3) DB temizle + yükle (atomic)
                with transaction.atomic():
                    self._delete_company_data()
                    # Defansif: yedekteki store_id'lere ait orphan OneToOne kayıtlarını sil
                    if backup_store_ids:
                        self._cleanup_orphan_store_records(backup_store_ids)
                    # Eski backup: Rolleri snapshot'tan geri yükle (Users FK geçerli olsun)
                    if _roles_snapshot_json:
                        for _robj in serializers.deserialize('json', _roles_snapshot_json,
                                                             ignorenonexistent=True):
                            _robj.save()
                    deserialized_count = self._load_backup_data(combined_json)

                # 4) Media restore (transaction sonrası)
                media_count = 0
                media_included = bool(manifest.get('include_media'))
                if media_included:
                    media_root = settings.MEDIA_ROOT
                    media_count = MediaPackager.extract_from_zip(
                        zf, media_root, base_path='media/'
                    )

                # 5) RestoreAuditLog
                notes_lines = [
                    f"[ZIP] Tam firma geri yüklemesi tamamlandı.",
                    f"{deserialized_count} kayıt yüklendi.",
                    f"Yedek tarihi: {backup.created_at.strftime('%d.%m.%Y %H:%M')}.",
                    f"Manifest version: {manifest.get('version', '?')}.",
                ]
                if media_included:
                    notes_lines.append(
                        f"Media dosyaları: {media_count} dosya geri yüklendi."
                    )
                if missing_sections:
                    notes_lines.append(
                        f"⚠ Eksik bölümler atlandı: {', '.join(missing_sections)}."
                    )

                RestoreAuditLog.objects.create(
                    backup=backup,
                    restore_type='FULL',
                    restored_by=user,
                    restore_notes=' '.join(notes_lines),
                )

                return True, (
                    f"ZIP yedek başarıyla geri yüklendi. "
                    f"{deserialized_count} DB kaydı"
                    + (f" + {media_count} media dosyası." if media_included else ".")
                )

        except zipfile.BadZipFile:
            return False, "Geçersiz ZIP dosyası."
        except Exception as e:
            return False, f"Restore Hatası (ZIP): {str(e)}\n{traceback.format_exc()}"

    # --------------------------------------------------------------------------
    #  YARDIMCI: Orphan mağaza kayıtlarını temizle (FAZ 60.3)
    # --------------------------------------------------------------------------
    def _cleanup_orphan_store_records(self, store_ids):
        """
        Yedekte geçen store_id'lere ait OneToOne / unique constraint'li
        kayıtları company filtresine bakmadan doğrudan siler.

        Neden gerekli:
            _delete_company_data() yalnızca self.company'ye bağlı stores'u kapsar.
            Stores.company NULLABLE olduğundan, geçmişte yarım kalmış işlemler
            sonucu company alanı NULL veya farklı bir firmaya işaret eden
            "orphan" mağazalar bulunabilir. Bu mağazaların OneToOne ayar
            tabloları (StoreConfiguration, StoreLabelSettings, StorePriceCache,
            StoreEInvoiceSettings) silinmediği için restore sırasında
            UNIQUE constraint ihlali verir.

        Bu temizlik, yalnızca yedekte ZATEN geçen store_id'lere yöneliktir;
        başka firmaların verisi üzerinde hiçbir etkisi yoktur.
        """
        if not store_ids:
            return

        # OneToOneField (store_id UNIQUE) modeller
        StoreConfiguration.objects.filter(store_id__in=store_ids).delete()
        StoreLabelSettings.objects.filter(store_id__in=store_ids).delete()
        StorePriceCache.objects.filter(store_id__in=store_ids).delete()
        # StoreEInvoiceSettings, InvoiceSequence kaldırıldı — invoices app yok

        # Roller — orphan store'a bağlı mağazaya özel roller de temizlenmeli.
        # RoleDetail önce silinmeli (role FK), ardından Roles.
        RoleDetail.objects.filter(role__store_id__in=store_ids).delete()
        Roles.objects.filter(store_id__in=store_ids).delete()

    # --------------------------------------------------------------------------
    #  YARDIMCI: Tüm firma verisini sıralı sil
    # --------------------------------------------------------------------------
    def _delete_company_data(self):
        """
        PROTECT FK zincirine göre tam silme sırası.

        Kural: bir modeli silebilmek için ona PROTECT FK ile bağlı TÜM
        modeller önce silinmiş olmalıdır.

        Kritik düzeltmeler:
          - Payment → process_no filtresi yerine store FK ile silme
            (process_no=NULL olan manuel/gider ödemeleri de yakalanır)
          - StoreTransferItem / StoreTransfer PROTECT FK zincirleri eklendi
            (ürün ve banka hesabı silimini blokluyordu)
          - CashboxLedger için banka hesabı bazlı ek guard eklendi
          - store_id ile çağrılırsa yalnızca o mağaza silinir; diğerleri dokunulmaz
        """
        # Silme kapsamı: store_id verilmişse yalnızca o mağaza, yoksa tümü.
        # is_deleted filtresiz — restore sırasında soft-delete'li kayıtlar da temizlenir.
        if self._store_id:
            stores_qs = Stores.objects.filter(company=self.company, id=self._store_id)
        else:
            stores_qs = Stores.objects.filter(company=self.company)

        # ── Adım 0: Mağaza Transferleri ───────────────────────────────────────
        # StoreTransfer.source/destination_store → PROTECT  → Stores silinmeden önce
        # StoreTransferItem.product              → PROTECT  → Products silinmeden önce
        # StoreTransferItem.source/dest_bank_account → PROTECT → BankAccount öncesi
        try:
            from apps.store_transfers.models import StoreTransfer, StoreTransferItem
            _st_qs = StoreTransfer.objects.filter(
                Q(source_store__in=stores_qs) | Q(destination_store__in=stores_qs)
            )
            StoreTransferItem.objects.filter(transfer__in=_st_qs).delete()
            _st_qs.delete()
        except Exception:
            pass  # Uygulama bu modeli kullanmıyor olabilir

        # ── Adım 1: CashboxLedger — çok eksenli tam temizlik ─────────────────
        # Store filtresi tek başına YETMİYOR: REVERSAL/child kayıtları farklı
        # store'a veya NULL store'a kayıtlı olabilir ve PROTECT self-ref nedeniyle
        # parent'ı silinmesini engeller.  _wipe_cashbox_for_stores() üç eksenden
        # (store / banka hesabı / payment) root ID'leri toplar, ardından tüm
        # çocukları (4 seviyeye kadar) child-first sırasıyla siler.
        _wipe_cashbox_for_stores(stores_qs)

        # ── Adım 2: Diğer Append-Only Ledger'lar ─────────────────────────────
        _delete_self_ref_safely(IncomeExpenseLedger, store__in=stores_qs)
        _delete_self_ref_safely(CustomerLedger, store__in=stores_qs)
        _delete_self_ref_safely(SupplierLedger, supplier__store__in=stores_qs)
        CustomerCustodyLedger.objects.filter(store__in=stores_qs).delete()

        # ── Adım 3+4: Pavo ve Fatura kaldırıldı — ilgili app'ler Juwelier Plus'ta yok ──

        # ── Adım 5: Payment ───────────────────────────────────────────────────
        # Adım 1'de tüm CashboxLedger (PROTECT kaynağı) zaten temizlendi.
        # Payment.store FK YOK — 3 yoldan filtre:
        #   a) bank_account.store    (FK)
        #   b) process_group.store   (FK)
        #   c) process_no__in=[...]  (legacy CharField)
        _ba_ids_for_pay = list(
            BankAccount.objects.filter(store__in=stores_qs).values_list('id', flat=True)
        )
        _proc_nos = list(
            Process.objects.filter(store__in=stores_qs)
                           .exclude(process_no__isnull=True)
                           .exclude(process_no__exact='')
                           .values_list('process_no', flat=True)
        )
        _pay_filter = Q(process_group__store__in=stores_qs)
        if _ba_ids_for_pay:
            _pay_filter |= Q(bank_account_id__in=_ba_ids_for_pay)
        if _proc_nos:
            _pay_filter |= Q(process_no__in=_proc_nos)
        Payment.objects.filter(_pay_filter).delete()

        # ── Adım 6: Process / İşlem Grubu ─────────────────────────────────────
        Process.objects.filter(
            Q(store__in=stores_qs) |
            Q(supplier__store__in=stores_qs) |
            Q(customer__store__in=stores_qs)
        ).delete()
        ProcessGroup.objects.filter(store__in=stores_qs).delete()

        # ── Adım 7: Tamir / Banka İşlemi ──────────────────────────────────────
        Repairs.objects.filter(store__in=stores_qs).delete()
        BankTransaction.objects.filter(store__in=stores_qs).delete()

        # ── Adım 8: Sayım ─────────────────────────────────────────────────────
        if COUNTS_AVAILABLE:
            InventoryCountItem.objects.filter(session__store__in=stores_qs).delete()
            InventoryCountSession.objects.filter(store__in=stores_qs).delete()

        # ── Adım 9: Stok ──────────────────────────────────────────────────────
        StockLedger.objects.filter(store__in=stores_qs).delete()
        StockSnapshot.objects.filter(store__in=stores_qs).delete()

        # ── Adım 10: Altın Alış & Ürünler ─────────────────────────────────────
        GoldPurchases.objects.filter(store__in=stores_qs).delete()
        BarcodeTemplate.objects.filter(store__in=stores_qs).delete()
        ProductCategory.objects.filter(store__in=stores_qs).delete()

        Bracelets.objects.filter(store__in=stores_qs).delete()
        if SCRAPS_AVAILABLE:
            Scraps.objects.filter(store__in=stores_qs).delete()

        if DIAMOND_WATCH_AVAILABLE:
            DiamondStone.objects.filter(diamond_detail__product__store__in=stores_qs).delete()
            DiamondDetail.objects.filter(product__store__in=stores_qs).delete()
            WatchDetail.objects.filter(product__store__in=stores_qs).delete()

        Products.objects.filter(store__in=stores_qs).delete()

        # ── Adım 11: Tedarikçi / Müşteri / Workshop ───────────────────────────
        Workshops.objects.filter(store__in=stores_qs).delete()
        Suppliers.objects.filter(store__in=stores_qs).delete()
        Customers.objects.filter(store__in=stores_qs).distinct().delete()

        # ── Adım 12: Mağaza Ayarları ───────────────────────────────────────────
        StoreConfiguration.objects.filter(store__in=stores_qs).delete()
        StoreLabelSettings.objects.filter(store__in=stores_qs).delete()
        StorePriceCache.objects.filter(store__in=stores_qs).delete()
        # StoreEInvoiceSettings, InvoiceSequence, EInvoiceCreditRequest kaldırıldı — invoices app yok

        # ── Adım 13: Kasa Hesapları → Mağazalar (son) ─────────────────────────
        BankAccount.objects.filter(store__in=stores_qs).delete()
        stores_qs.delete()

    # --------------------------------------------------------------------------
    #  YARDIMCI: Yedeği deserialize edip yükle
    # --------------------------------------------------------------------------
    def _load_backup_data(self, json_data):
        """
        Returns:
            int — yüklenen kayıt sayısı.
        """
        objects = serializers.deserialize(
            'json', json_data, ignorenonexistent=True
        )
        count = 0
        for obj in objects:
            obj.save()
            count += 1
        return count
