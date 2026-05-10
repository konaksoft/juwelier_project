import random
import decimal
import uuid
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from faker import Faker

# Modeller
from apps.stores.models import Stores, Company
from apps.accounts.models import Users
from apps.suppliers.models import Suppliers
from apps.products.models import Products, Categories
from apps.gold_purchases.models import GoldPurchases
from apps.inventories.models import Inventories
from apps.customers.models import Customers

# Türkçe veri üreticisi
fake = Faker('tr_TR')


class Command(BaseCommand):
    help = 'Veritabanına gerçekçi barkod yapısı ile test verisi yükler'

    def add_arguments(self, parser):
        parser.add_argument('total', type=int, help='Kaç adet ürün oluşturulacak?')
        parser.add_argument('--store_id', type=str, help='Verilerin ekleneceği Mağaza ID (UUID)', required=False)
        parser.add_argument('--company_id', type=str, help='Verilerin ekleneceği Firma ID (UUID)', required=False)

    def _generate_barcode(self, product_name, store):
        """
        KuyumPlus sistemindeki orijinal barkod üretim mantığı.
        İsimden prefix çıkarır (Örn: Kelepçe -> KE) ve sıradaki numarayı verir (KE0001).
        """
        s = (product_name or '').strip().upper()
        if not s:
            prefix = "PR"
        else:
            first_char = ""
            last_char = ""
            for ch in s:
                if ch.isalnum():
                    first_char = ch
                    break
            for ch in reversed(s):
                if ch.isalnum():
                    last_char = ch
                    break
            prefix = (first_char + last_char).strip() or "PR"

        width = 4

        # Mevcut barkodları çek
        existing = Products.objects.filter(
            is_deleted=False,
            store=store,
            barcode__istartswith=prefix
        ).exclude(barcode__isnull=True).exclude(barcode__exact="").values_list('barcode', flat=True)

        max_n = 0
        pref_len = len(prefix)

        for b in existing:
            bb = (str(b) if b is not None else "").strip().upper()
            if not bb.startswith(prefix):
                continue
            suf = bb[pref_len:]

            # Sadece sayısal ve doğru uzunlukta olanları dikkate al
            if len(suf) != width or not suf.isdigit():
                continue

            n = int(suf)
            if n > max_n:
                max_n = n

        n = max_n + 1
        while True:
            candidate = f"{prefix}{str(n).zfill(width)}".upper()
            # Çakışma kontrolü (DB'de var mı?)
            if not Products.objects.filter(is_deleted=False, store=store, barcode__iexact=candidate).exists():
                return candidate
            n += 1

    def handle(self, *args, **kwargs):
        total = kwargs['total']
        store_id_arg = kwargs.get('store_id')
        company_id_arg = kwargs.get('company_id')

        store = None
        company = None

        try:
            # 1. MAĞAZA SEÇİMİ
            if store_id_arg:
                store = Stores.objects.get(id=store_id_arg)
                self.stdout.write(self.style.SUCCESS(f'Hedef Mağaza: {store.title}'))
            else:
                store = Stores.objects.filter(is_active=True, is_deleted=False).first()
                if store:
                    self.stdout.write(self.style.WARNING(f'Mağaza ID belirtilmedi. Varsayılan: {store.title}'))

            # 2. FİRMA SEÇİMİ
            if company_id_arg:
                company = Company.objects.get(id=company_id_arg)
                self.stdout.write(self.style.SUCCESS(f'Hedef Firma: {company.title}'))
            else:
                if store and store.company:
                    company = store.company
                else:
                    company = Company.objects.filter(is_active=True, is_deleted=False).first()
                if company:
                    self.stdout.write(self.style.WARNING(f'Firma ID belirtilmedi. Varsayılan: {company.title}'))

            if not store or not company:
                self.stdout.write(self.style.ERROR('HATA: Geçerli Mağaza/Firma bulunamadı!'))
                return

            # 3. KULLANICI SEÇİMİ
            user = Users.objects.filter(store=store, is_active=True).first()
            if not user:
                user = Users.objects.filter(is_active=True).first()

            if user:
                self.stdout.write(self.style.SUCCESS(f'İşlem Yapan Kullanıcı: {user.username}'))
            else:
                self.stdout.write(self.style.ERROR('HATA: Kullanıcı bulunamadı!'))
                return

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Başlangıç Hatası: {str(e)}'))
            return

        # İŞLEM BAŞLANGICI
        with transaction.atomic():

            # --- TEDARİKÇİ OLUŞTURMA (5 Adet) ---
            self.stdout.write('Tedarikçiler oluşturuluyor...')
            suppliers = []
            for _ in range(5):
                sup = Suppliers.objects.create(
                    company_name=fake.company(),
                    person_name=fake.first_name(),
                    person_surname=fake.last_name(),
                    email=fake.email(),
                    phone=fake.phone_number()[:15],
                    company_address=fake.address(),
                    store=store,
                    is_active=True,
                    is_deleted=False
                )
                suppliers.append(sup)

            # --- MÜŞTERİ OLUŞTURMA (10 Adet) ---
            self.stdout.write('Müşteriler oluşturuluyor...')
            for _ in range(10):
                cust = Customers.objects.create(
                    first_name=fake.first_name(),
                    last_name=fake.last_name(),
                    customer_number=str(random.randint(100000, 999999)),
                    phone=fake.phone_number()[:15],
                    gender=random.choice(['Erkek', 'Kadın']),
                    email=fake.email(),
                    address=fake.address(),
                    is_active=True,
                    is_deleted=False
                )
                cust.store.add(store)

            # --- KATEGORİ ---
            category = None
            try:
                category = Categories.objects.filter(store=store).first()
                if not category:
                    category = Categories.objects.create(name="Genel", store=store)
            except:
                pass

                # --- ÜRÜN OLUŞTURMA ---
            self.stdout.write(f'{total} adet ürün oluşturuluyor (Barkod sistemi aktif)...')

            jewelry_types = ['Alyans', 'Bileklik', 'Bilezik', 'Gerdanlık', 'Kelepçe', 'Kolye', 'Küpe', 'Saat', 'Takı',
                             'Yüzük', 'Zincir']
            gold_rates = [995, 916, 875, 750, 585, 416]

            for i in range(total):
                j_type = random.choice(jewelry_types)
                g_rate = random.choice(gold_rates)
                gram = decimal.Decimal(random.uniform(1.50, 25.00)).quantize(decimal.Decimal('0.001'))

                # Rastgele ama mantıklı ürün ismi
                # Örn: "Bilezik - Hasır", "Yüzük - Baget"
                product_name = f"{j_type} - {fake.word().title()}"

                # --- BARKOD OLUŞTURMA (SENİN SİSTEMİN) ---
                # Fonksiyonu çağırıp gerçek bir barkod alıyoruz (Örn: BZ0042)
                barcode = self._generate_barcode(product_name, store)

                milyem = g_rate
                has_maliyet = (decimal.Decimal(milyem) / 1000) * gram

                # 1. Ürün Kaydı
                product = Products.objects.create(
                    store=store,
                    category=category,
                    barcode=barcode,
                    name=product_name,
                    jewelry_type=j_type,
                    gram=gram,
                    gold_rate=str(g_rate),
                    product_mileage=str(milyem),
                    labor_mileage="0",
                    piece_labor="0",
                    buy_price_hs=has_maliyet,
                    sale_price_hs=has_maliyet * decimal.Decimal(1.2),
                    sale_price_eur=0,
                    profit=random.uniform(10.0, 35.0),
                    image="default/default.png",
                    is_active=True,
                    is_deleted=False,
                    created_by=user,
                    created_on=timezone.now()
                )

                # 2. Alım Kaydı
                supplier = random.choice(suppliers)
                GoldPurchases.objects.create(
                    store=store,
                    product=product,
                    supplier=supplier,
                    is_status=True,
                    is_active=True,
                    count_is_status=0,
                    created_by=user
                )

                # 3. Stok Kaydı
                Inventories.objects.create(
                    store=store,
                    product=product,
                    stock_pieces=1,
                    stock_weight=gram,
                    incoming_stock_pieces=0,
                    incoming_stock_weight=0,
                    created_by=user,
                    created_on=timezone.now()
                )

                if i % 50 == 0:
                    self.stdout.write(f'... {i} ürün oluşturuldu (Son Barkod: {barcode})')

        self.stdout.write(self.style.SUCCESS(f'BAŞARILI! {store.title} mağazasına {total} adet ürün yüklendi.'))
