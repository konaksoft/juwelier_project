import sys
import traceback
from django.core import serializers
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.conf import settings
from django.utils import timezone

# --- MODELLERİN İMPORT EDİLMESİ ---
from apps.bracelets.models import *
from apps.custody.models import *

# Sayım modülü kontrolü (Opsiyonel)
try:
    from apps.counts.models import *
except ImportError:
    pass
from apps.customers.models import *
from apps.gold_purchases.models import GoldPurchases
# --- FAZ 4: StockSnapshot ve StockLedger entegrasyonu (Inventories yerine) ---
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.invoices.models import *
from apps.pavo.models import PavoAccount, PavoTerminal, PavoPayment, PavoLocalSale, PavoWebhookEvent
from apps.process.models import Process, Payment
from apps.repairs.models import *
from apps.suppliers.models import *
from apps.workshops.models import *
from apps.accounts.models import *
from apps.stores.models import *


class BackupService:
    def __init__(self, company_id):
        self.company = Company.objects.get(id=company_id)
        # Firmaya bağlı mağazaları çekiyoruz (Silinmemiş olanlar)
        self.stores = Stores.objects.filter(company=self.company, is_deleted=False)
        self.store_ids = self.stores.values_list('id', flat=True)

    def _filter_active(self, queryset):
        """
        YARDIMCI METOD:
        Gönderilen QuerySet'in modelinde 'is_deleted' alanı var mı bakar.
        Varsa -> .filter(is_deleted=False) uygular.
        Yoksa -> Olduğu gibi geri döndürür.
        """
        try:
            # Modelin tüm alan isimlerini al
            field_names = [f.name for f in queryset.model._meta.get_fields()]
            # is_deleted varsa filtrele
            if 'is_deleted' in field_names:
                return queryset.filter(is_deleted=False)
            return queryset
        except Exception:
            return queryset

    def create_backup(self, note="", user=None):
        """
        Veriyi toplar, JSON yapar ve CompanyBackup modeline kaydeder.
        """
        # Eğer ana firma silinmişse yedek alma
        if hasattr(self.company, 'is_deleted') and self.company.is_deleted:
            return None

        all_objects = []

        # === 1. Temel Yapı ===
        all_objects.extend(list([self.company]))
        all_objects.extend(list(self.stores))

        # Mağaza Ayarları
        all_objects.extend(list(self._filter_active(StorePriceCache.objects.filter(store__in=self.store_ids))))
        all_objects.extend(list(self._filter_active(StoreEInvoiceSettings.objects.filter(store__in=self.store_ids))))
        all_objects.extend(list(self._filter_active(InvoiceSequence.objects.filter(store__in=self.store_ids))))
        all_objects.extend(list(self._filter_active(EInvoiceCreditRequest.objects.filter(store__in=self.store_ids))))

        # === 2. Kullanıcılar ve Roller ===
        users = self._filter_active(Users.objects.filter(store__in=self.store_ids))
        all_objects.extend(list(users))

        # Generic İlişkiler
        user_ids_str = [str(u.id) for u in users]
        store_ids_str = [str(s) for s in self.store_ids]

        all_objects.extend(
            list(self._filter_active(ContactConsent.objects.filter(owner_type='store', owner_id__in=store_ids_str))))
        all_objects.extend(
            list(self._filter_active(ContactConsent.objects.filter(owner_type='user', owner_id__in=user_ids_str))))
        all_objects.extend(
            list(self._filter_active(OtpCode.objects.filter(owner_type='store', owner_id__in=store_ids_str))))

        # === 3. Paydaşlar ===
        customers = self._filter_active(Customers.objects.filter(store__in=self.store_ids).distinct())
        all_objects.extend(list(customers))

        suppliers = self._filter_active(Suppliers.objects.filter(store__in=self.store_ids))
        all_objects.extend(list(suppliers))

        workshops = self._filter_active(Workshops.objects.filter(store__in=self.store_ids))
        all_objects.extend(list(workshops))

        # === 4. Ürünler ve Stok ===
        products = self._filter_active(Products.objects.filter(store__in=self.store_ids))
        all_objects.extend(list(products))

        all_objects.extend(list(self._filter_active(Bracelets.objects.filter(store__in=self.store_ids))))
        all_objects.extend(list(StockSnapshot.objects.filter(store__in=self.store_ids)))
        all_objects.extend(list(StockLedger.objects.filter(store__in=self.store_ids)))
        all_objects.extend(list(self._filter_active(Scraps.objects.filter(store__in=self.store_ids))))
        all_objects.extend(list(self._filter_active(GoldPurchases.objects.filter(store__in=self.store_ids))))

        # Sayım işlemleri
        try:
            from apps.counts.models import InventoryCountSession, InventoryCountItem
            count_sessions = self._filter_active(InventoryCountSession.objects.filter(store__in=self.store_ids))
            all_objects.extend(list(count_sessions))
            all_objects.extend(list(self._filter_active(InventoryCountItem.objects.filter(session__in=count_sessions))))
        except (ImportError, NameError):
            pass

        # === 5. İşlemler ve Finans ===
        processes = self._filter_active(Process.objects.filter(store__in=self.store_ids))
        all_objects.extend(list(processes))

        # Payment (Process'e bağlı olanlar)
        proc_nos = processes.values_list('process_no', flat=True).exclude(process_no__isnull=True)
        payments = self._filter_active(Payment.objects.filter(process_no__in=list(proc_nos)))
        all_objects.extend(list(payments))

        # Faturalar
        invoices = self._filter_active(Invoice.objects.filter(store__in=self.store_ids))
        all_objects.extend(list(invoices))
        all_objects.extend(list(self._filter_active(InvoiceItem.objects.filter(invoice__in=invoices))))
        all_objects.extend(list(self._filter_active(InvoicePaymentAllocation.objects.filter(invoice__in=invoices))))

        # Defterler
        all_objects.extend(list(self._filter_active(CustomerCustodyLedger.objects.filter(store__in=self.store_ids))))
        all_objects.extend(list(self._filter_active(SupplierLedger.objects.filter(supplier__in=suppliers))))

        # Tamir
        all_objects.extend(list(self._filter_active(Repairs.objects.filter(store__in=self.store_ids))))

        # === 6. Entegrasyonlar (Pavo) ===
        pavo_accounts = self._filter_active(PavoAccount.objects.filter(store__in=self.store_ids))
        all_objects.extend(list(pavo_accounts))

        terminals = self._filter_active(PavoTerminal.objects.filter(store__in=self.store_ids))
        all_objects.extend(list(terminals))

        all_objects.extend(list(self._filter_active(PavoPayment.objects.filter(invoice__in=invoices))))
        all_objects.extend(list(self._filter_active(PavoLocalSale.objects.filter(invoice__in=invoices))))
        all_objects.extend(list(self._filter_active(PavoWebhookEvent.objects.filter(invoice__in=invoices))))

        # === SERİLEŞTİRME VE KAYDETME ===
        json_data = serializers.serialize('json', all_objects, indent=2, use_natural_foreign_keys=True)

        from apps.backups.models import CompanyBackup
        filename = f"backup_{self.company.id}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json"

        backup = CompanyBackup(
            company=self.company,
            note=note,
            created_by_user=str(user) if user else "System"
        )
        backup.backup_file.save(filename, ContentFile(json_data))
        backup.save()

        return backup

    def restore_backup(self, backup_id):
        """
        1. Yedeği belleğe alır.
        2. Firmanın mevcut verilerini temizler (Hata almamak için sondan başa doğru siler).
        3. Yedeği geri yükler.
        """
        from apps.backups.models import CompanyBackup

        # Temizlik için gerekli modeller (Explicit import)
        from apps.stores.models import Stores
        from apps.process.models import Process, Payment
        from apps.invoices.models import Invoice
        from apps.pavo.models import PavoAccount
        # Diğer gerekli modelleri zaten dosyanın başında import ettik

        try:
            backup = CompanyBackup.objects.get(id=backup_id)
        except CompanyBackup.DoesNotExist:
            return False, "Yedek kaydı bulunamadı."

        try:
            # 1. Dosyayı oku
            with backup.backup_file.open('r') as f:
                data = f.read()

            if isinstance(data, bytes):
                data = data.decode('utf-8')

            # 2. Transaction başlat
            with transaction.atomic():
                # === TEMİZLİK (HARD DELETE - Child to Parent) ===
                # Bu bölüm "ForeignKeyViolation" hatasını önlemek için kritik öneme sahiptir.

                # A. En uçtaki işlem tabloları
                # Process tablosu Supplier ve Customer'a bağlıdır. Store silinmeden önce bunlar gitmeli.
                # NOT: Sadece 'store__company' filtresi yetmeyebilir, 'store' alanı boş olan (yetim) kayıtları da temizlemek gerekebilir.
                # Bu yüzden firmaya ait tüm mağazaları bulup, onlarla ilişkili tüm process'leri siliyoruz.

                stores_qs = Stores.objects.filter(company=self.company)

                # 1. İşlemler (Process) - Suppliers ve Customers ile ilişkili
                # Hem store'a hem supplier'a hem customer'a bakarak siliyoruz (Q objects ile)
                Process.objects.filter(
                    Q(store__in=stores_qs) |
                    Q(supplier__store__in=stores_qs) |
                    Q(customer__store__in=stores_qs)
                ).delete()

                # 2. Faturalar (Invoices)
                Invoice.objects.filter(store__in=stores_qs).delete()

                # 3. Sayım İşlemleri (Eğer varsa)
                try:
                    from apps.counts.models import InventoryCountSession
                    InventoryCountSession.objects.filter(store__in=stores_qs).delete()
                except (ImportError, NameError):
                    pass

                # 4. Pavo Hesapları (Company veya Store bazlı olabilir)
                PavoAccount.objects.filter(Q(company=self.company) | Q(store__in=stores_qs)).delete()

                # 5. Ödemeler (Process ile silinmemiş olanlar varsa)
                # Payment modelinde 'store' alanı yoksa 'process_no' üzerinden bulmaya çalışabiliriz ama process silindiği için zordur.
                # Eğer Payment modeline 'store' eklediyseniz: Payment.objects.filter(store__in=stores_qs).delete()

                # 6. Defterler (Ledgers) - Genelde Cascade silinir ama garanti olsun
                SupplierLedger.objects.filter(supplier__store__in=stores_qs).delete()
                CustomerCustodyLedger.objects.filter(store__in=stores_qs).delete()

                # 7. ANA SİLME İŞLEMİ (Stores)
                # Artık bağlı Process ve Invoice'lar gittiği için Store'u silebiliriz.
                # Store silinince -> Products, Suppliers, Customers, Inventories vb. otomatik silinir (Cascade).
                stores_qs.delete()  # Toplu silme (QuerySet üzerinden)

                # === YÜKLEME ===
                objects = serializers.deserialize('json', data, ignorenonexistent=True)

                for obj in objects:
                    # obj.save() işlemi UUID olduğu için çakışma yaratmaz, insert yapar.
                    obj.save()

            return True, "Firma verileri temizlendi ve yedek başarıyla yüklendi."

        except Exception as e:
            import traceback
            return False, f"Restore Hatası: {str(e)}\n{traceback.format_exc()}"
