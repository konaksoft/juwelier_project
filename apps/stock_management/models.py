"""
Kuyum Plus - Stok Yonetimi (Ledger Tabanli) Modelleri
=====================================================

FAZ 1: Mevcut tablolara (Inventories, Scraps, Bracelets) dokunulmadan
yan yana calisacak yeni stok ve fiyat altyapisi.

Mimari:
  - StockSnapshot : Anlik stok durumu (cache) + WAC (Agirlikli Ortalama Maliyet)
  - StockLedger   : Degismez (immutable) stok hareket logu
  - PriceProvider : Coklu API fiyat saglayici kaydi
  - PriceQuote    : Her API'den gelen tarihsel fiyat kaydi (DB loglama)

Kurallar:
  1. StockLedger satirlari ASLA guncellenmez veya silinmez.
  2. StockSnapshot SADECE servis katmani (StockService) uzerinden guncellenir.
  3. Negatif stok DB constraint ile engellenir.
  4. Her donusum islemi cift ledger satiri uretir (paired_entry).
"""

import uuid
from decimal import Decimal

from django.db import models
from django.db.models import CheckConstraint, Q, UniqueConstraint
from django.utils import timezone


# ============================================================================
# STOK SNAPSHOT - Anlik Stok Durumu
# ============================================================================

class StockSnapshot(models.Model):
    """
    Her magaza-urun cifti icin anlik stok durumunu tutar.

    Bu tablo bir CACHE tablosudur. Gercek kaynak StockLedger'dir.
    Gunluk celery task'i ile StockLedger SUM'u ile dogrulanabilir.

    Guncelleme kurali:
        - SADECE StockService.record_entry() ve StockService.record_exit()
          fonksiyonlari select_for_update() ile bu tabloyu gunceller.
        - Hicbir view, signal veya admin paneli bu tabloyu dogrudan degistirmemelidir.

    Negatif stok engeli:
        - PostgreSQL CheckConstraint ile hem gram hem adet negatife dusmez.
        - Ek olarak servis katmaninda uygulama seviyesinde de kontrol vardir.

    WAC (Weighted Average Cost / Agirlikli Ortalama Maliyet):
        - Her stok girisinde yeniden hesaplanir.
        - Formul: WAC = (Mevcut Deger + Gelen Deger) / Toplam Miktar
        - Stok cikisinda WAC degismez (cikan maliyet WAC'tan alinir).
    """

    id = models.BigAutoField(primary_key=True)

    product = models.ForeignKey(
        'products.Products',
        on_delete=models.PROTECT,
        related_name='stock_snapshots',
        verbose_name='Urun',
        help_text='Stok takibi yapilan urun'
    )

    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='stock_snapshots',
        verbose_name='Magaza',
        help_text='Stogun tutuldugu magaza'
    )

    # --- Anlik stok miktarlari ---
    stock_pieces = models.IntegerField(
        default=0,
        verbose_name='Stok (Adet)',
        help_text='Mevcut adet stok. Barkodlu urunler icin.'
    )

    stock_gram = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal('0.0000'),
        verbose_name='Stok (Gram)',
        help_text='Mevcut gram stok. Hurda, bilezik, kulce icin.'
    )

    # --- FAZ 48.1: Emanet Havuzu (Mağaza stoğundan ayrı) ---
    # Müşterilerin emanet bıraktığı stok miktarı. Bu alan SATIŞ akışlarında
    # (Hızlı İşlem, Perakende, Toptan, Hurdalar, Bilezikler sayfaları)
    # KESİNLİKLE GÖSTERİLMEZ ve hesaba katılmaz. Sadece /custody/ Emanet
    # Yönetimi ekranında görünür. Emanet stoğunu mağaza stoğuna almak için
    # CustodyToStockService.transfer() kullanılır (atomik: custody_gram düşer,
    # stock_gram artar; tıpkı bir Hurda/Ziynet alımı gibi WAC güncellenir).
    custody_gram = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal('0.0000'),
        verbose_name='Emanet Stok (Gram)',
        help_text=(
            'Müşteri emaneti gram stok. Mağaza satılabilir stoğunun PARÇASI DEĞİLDİR. '
            'Sadece Emanet Yönetimi ekranında görünür.'
        )
    )

    custody_pieces = models.IntegerField(
        default=0,
        verbose_name='Emanet Stok (Adet)',
        help_text=(
            'Müşteri emaneti adet stok. Mağaza satılabilir stoğunun PARÇASI DEĞİLDİR. '
            'Sadece Emanet Yönetimi ekranında görünür.'
        )
    )

    # --- Agirlikli Ortalama Maliyet (WAC) ---
    weighted_avg_cost_hs = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('0.0000'),
        verbose_name='WAC Has',
        help_text='Agirlikli ortalama alis maliyeti (Has Altin cinsinden)'
    )

    weighted_avg_cost_eur = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name='WAC TL',
        help_text='Agirlikli ortalama alis maliyeti (TL cinsinden)'
    )

    # --- Gelen stok (henuz kesinlestirilmemis/bekleyen) ---
    incoming_stock_pieces = models.IntegerField(
        default=0,
        verbose_name='Bekleyen Stok (Adet)',
        help_text='Siparis verilmis ama henuz teslim alinmamis adet'
    )

    incoming_stock_gram = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal('0.0000'),
        verbose_name='Bekleyen Stok (Gram)',
        help_text='Siparis verilmis ama henuz teslim alinmamis gram'
    )

    # --- Magaza ozel fiyatlandirma (opsiyonel override) ---
    use_custom_pricing = models.BooleanField(
        default=False,
        verbose_name='Ozel Fiyat Kullan',
        help_text='True ise bu magaza icin asagidaki ozel fiyatlar kullanilir'
    )

    custom_buy_price_hs = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('0.0000'),
        null=True,
        blank=True,
        verbose_name='Ozel Alis Has',
        help_text='Magaza ozel alis fiyati (Has cinsinden)'
    )

    custom_sale_price_hs = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('0.0000'),
        null=True,
        blank=True,
        verbose_name='Ozel Satis Has',
        help_text='Magaza ozel satis fiyati (Has cinsinden)'
    )

    custom_fixed_labor = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        null=True,
        blank=True,
        verbose_name='Ozel Sabit Iscilik',
        help_text='Magaza ozel sabit iscilik tutari'
    )

    # --- Zaman damgalari ---
    updated_on = models.DateTimeField(
        auto_now=True,
        verbose_name='Son Guncelleme'
    )

    created_on = models.DateTimeField(
        auto_now_add=True,
        verbose_name='Olusturma Tarihi'
    )

    class Meta:
        db_table = 'StockSnapshot'
        verbose_name = 'Stok Snapshot'
        verbose_name_plural = 'Stok Snapshot Kayitlari'
        ordering = ['-updated_on']

        constraints = [
            # Her magaza-urun cifti tekil olmali
            UniqueConstraint(
                fields=['store', 'product'],
                name='stock_snapshot_unique_store_product'
            ),

            # VERITABANI SEVIYESINDE NEGATIF STOK ENGELI
            # Bu constraint, uygulama katmanindaki tum bug'lara karsi son savunma hattıdır.
            # Eger herhangi bir kod negatif stoga dusurmeye calisirsa,
            # PostgreSQL IntegrityError firlatir ve transaction geri alinir.
            CheckConstraint(
                condition=Q(stock_gram__gte=Decimal('0.0000')),
                name='stock_snapshot_no_negative_gram'
            ),
            CheckConstraint(
                condition=Q(stock_pieces__gte=0),
                name='stock_snapshot_no_negative_pieces'
            ),
            # FAZ 48.1 — Emanet havuzu da negatife düşemez
            CheckConstraint(
                condition=Q(custody_gram__gte=Decimal('0.0000')),
                name='stock_snapshot_no_negative_custody_gram'
            ),
            CheckConstraint(
                condition=Q(custody_pieces__gte=0),
                name='stock_snapshot_no_negative_custody_pieces'
            ),
        ]

        indexes = [
            models.Index(
                fields=['store', 'product'],
                name='stksnapshot_store_product_idx'
            ),
            models.Index(
                fields=['store'],
                name='stksnapshot_store_idx'
            ),
            models.Index(
                fields=['product'],
                name='stksnapshot_product_idx'
            ),
        ]

    def __str__(self):
        return (
            f"[{self.store}] {self.product} | "
            f"Gram: {self.stock_gram} | Adet: {self.stock_pieces}"
        )

    @property
    def total_value_hs(self) -> Decimal:
        """Toplam (mağaza) stok degeri Has cinsinden (WAC * miktar). Emanet HARİÇ."""
        return (self.stock_gram * self.weighted_avg_cost_hs).quantize(Decimal('0.0001'))

    @property
    def total_value_tl(self) -> Decimal:
        """Toplam (mağaza) stok degeri TL cinsinden (WAC * miktar). Emanet HARİÇ."""
        return (self.stock_gram * self.weighted_avg_cost_eur).quantize(Decimal('0.01'))

    @property
    def has_custody(self) -> bool:
        """FAZ 48.1 — Bu üründe aktif emanet stoğu var mı?"""
        return (self.custody_gram > Decimal('0.0000')) or (self.custody_pieces > 0)


# ============================================================================
# STOK LEDGER - Degismez Stok Hareket Logu
# ============================================================================

class StockLedger(models.Model):
    """
    Degismez (immutable) stok hareket kaydi.

    Her stok degisikligi (giris, cikis, donusum, transfer, duzeltme) bu tabloya
    yeni bir satir olarak eklenir. Hicbir satir ASLA guncellenmez veya silinmez.

    Cift-tarafli muhasebe mantigi:
        Ornek: 500g Hurda -> 480g Barkodlu Urun + 20g fire
        Satir 1: product=hurda,    direction=OUT, quantity_gram=500,  reason=CONV_OUT
        Satir 2: product=barkodlu, direction=IN,  quantity_gram=480,  reason=CONV_IN
        Satir 3: product=fire,     direction=OUT, quantity_gram=20,   reason=SCRAP_MELT
        Satir 1 ve 2 paired_entry ile birbirine baglidir.

    Referans Sistemi (ref_type + ref_id):
        Her ledger satiri bir ust isleme baglanir:
        - 'process'     : Process tablosundaki bir islem (satis, alis)
        - 'conversion'  : Donusum islemi (hurda -> mamul)
        - 'invoice'     : Fatura ile bagli hareket
        - 'transfer'    : Magazalar arasi transfer
        - 'count'       : Sayim duzeltmesi
        - 'adjustment'  : Manuel duzeltme
        - 'initial'     : Acilis stoku (data migration)
        - 'legacy'      : Eski sistemden tasinan veri
    """

    class Direction(models.TextChoices):
        IN = 'IN', 'Giris'
        OUT = 'OUT', 'Cikis'

    class Reason(models.TextChoices):
        PURCHASE = 'PURCHASE', 'Tedarikci Alisi'
        SALE = 'SALE', 'Satis'
        RETURN_IN = 'RETURN_IN', 'Satis Iadesi (Giris)'
        RETURN_OUT = 'RETURN_OUT', 'Alis Iadesi (Cikis)'
        CONVERSION_OUT = 'CONV_OUT', 'Donusum Cikisi (Kaynak)'
        CONVERSION_IN = 'CONV_IN', 'Donusum Girisi (Hedef)'
        TRANSFER_OUT = 'XFER_OUT', 'Transfer Cikis'
        TRANSFER_IN = 'XFER_IN', 'Transfer Giris'
        ADJUSTMENT_PLUS = 'ADJ_PLUS', 'Duzeltme Giris (Fazla)'
        ADJUSTMENT_MINUS = 'ADJ_MINUS', 'Duzeltme Cikis (Eksik)'
        INITIAL = 'INITIAL', 'Acilis Sayimi'
        SCRAP_MELT = 'SCRAP_MELT', 'Eritme / Isleme Fire'
        REPAIR_IN = 'REPAIR_IN', 'Tamir Giris'
        REPAIR_OUT = 'REPAIR_OUT', 'Tamir Cikis'
        CUSTODY_IN = 'CUSTODY_IN', 'Emanet Alındı (Giriş)'
        CUSTODY_OUT = 'CUSTODY_OUT', 'Emanet Teslim (Çıkış)'
        # FAZ 24 — GEREKSİNİM-2: Emanetten serbest stoğa transfer.
        # Stok sayısı değişmez; reason "emanet havuzu"ndan "serbest havuz"a
        # geçişi denetim olarak işaretler. Müşterinin cari hesabına
        # eşdeğer Has borcu yazılır (CustomerLedger).
        CUSTODY_TO_STOCK = 'CUSTODY_2_STK', 'Emanet → Serbest Stok'
        # FAZ 49 — Müşteri ürün/hurda ile borcunu öder (PAYMENT_IN) veya
        # mağaza müşteriye ürün vererek alacağı kapatır (PAYMENT_OUT).
        # SALE/PURCHASE'tan AYRI tutulur ki ciro/satış raporlarını şişirmesin.
        # Stok efekti: PAYMENT_IN → stock_gram artar (WAC güncellenir,
        # PURCHASE gibi); PAYMENT_OUT → stock_gram azalır (SALE gibi
        # değil — fiyatlandırma müşteri ile karşılıklı kararlaştırılır).
        PAYMENT_IN = 'PAYMENT_IN', 'Ürün ile Tahsilat (Müşteriden Alındı)'
        PAYMENT_OUT = 'PAYMENT_OUT', 'Ürün ile Ödeme (Müşteriye Verildi)'

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    product = models.ForeignKey(
        'products.Products',
        on_delete=models.PROTECT,
        related_name='ledger_entries',
        verbose_name='Urun',
        help_text='Hareketin ait oldugu urun. PROTECT: urun silinirse hata verir.'
    )

    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='ledger_entries',
        verbose_name='Magaza'
    )

    # --- Hareket yonu ve sebebi ---
    direction = models.CharField(
        max_length=3,
        choices=Direction.choices,
        verbose_name='Yon',
        help_text='IN = stok artisi, OUT = stok azalisi'
    )

    reason = models.CharField(
        max_length=20,
        choices=Reason.choices,
        verbose_name='Sebep',
        help_text='Hareketin neden yapildigini belirten kod'
    )

    # --- Miktarlar (her zaman POZITIF girilir, yon direction ile belirlenir) ---
    quantity_pieces = models.PositiveIntegerField(
        default=0,
        verbose_name='Miktar (Adet)',
        help_text='Hareket miktari adet. Barkodlu urunler icin.'
    )

    quantity_gram = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal('0.0000'),
        verbose_name='Miktar (Gram)',
        help_text='Hareket miktari gram. Hurda, bilezik, kulce icin.'
    )

    # --- Islem anindaki fiyat bilgisi (degismez tarihsel kayit) ---
    unit_cost_hs = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('0.0000'),
        verbose_name='Birim Maliyet (Has)',
        help_text='Islem anindaki birim Has maliyeti. Cikislarda WAC muhurlenir.'
    )

    unit_cost_eur = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name='Birim Maliyet (TL)',
        help_text='Islem anindaki birim TL maliyeti.'
    )

    hs_rate_eur = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal('0.0000'),
        verbose_name='Has/TL Kuru',
        help_text='Islem anindaki Has Altin TL kuru (tarihsel referans).'
    )

    # --- Referans sistemi: Hangi isleme ait? ---
    ref_type = models.CharField(
        max_length=30,
        db_index=True,
        verbose_name='Referans Tipi',
        help_text=(
            "Ust islem turu: 'process', 'conversion', 'invoice', "
            "'transfer', 'count', 'adjustment', 'initial', 'legacy'"
        )
    )

    ref_id = models.CharField(
        max_length=64,
        db_index=True,
        verbose_name='Referans ID',
        help_text='Ilgili kaydin UUID veya process_no degeri'
    )

    # --- Donusum cifti baglantisi ---
    paired_entry = models.OneToOneField(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='paired_with',
        verbose_name='Eslestirilmis Kayit',
        help_text=(
            'Donusum islemlerinde kaynak (OUT) ve hedef (IN) satirlarini '
            'birbirine baglar. Tek bir donusum isleminin iki tarafini izlemek icindir.'
        )
    )

    # --- Ek bilgiler ---
    notes = models.TextField(
        blank=True,
        default='',
        verbose_name='Aciklama',
        help_text='Hareket hakkinda serbest metin aciklama'
    )

    created_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_ledger_entries',
        verbose_name='Islemi Yapan'
    )

    created_on = models.DateTimeField(
        auto_now_add=True,
        verbose_name='Islem Tarihi'
    )

    class Meta:
        db_table = 'StockLedger'
        verbose_name = 'Stok Hareket Logu'
        verbose_name_plural = 'Stok Hareket Loglari'
        ordering = ['-created_on']

        constraints = [
            # Ledger miktarlari negatif olamaz (yonu direction belirler)
            CheckConstraint(
                condition=Q(quantity_gram__gte=Decimal('0.0000')),
                name='stock_ledger_positive_gram'
            ),
            # PositiveIntegerField zaten negatif engelliyor ama constraint de ekleyelim
            CheckConstraint(
                condition=Q(quantity_pieces__gte=0),
                name='stock_ledger_positive_pieces'
            ),
        ]

        indexes = [
            # Ana sorgu: Belirli magaza ve urunun hareketleri
            models.Index(
                fields=['product', 'store', '-created_on'],
                name='sl_prod_store_date_idx'
            ),
            # Sebep bazli filtreleme
            models.Index(
                fields=['store', 'reason', '-created_on'],
                name='sl_store_reason_date_idx'
            ),
            # Referans sistemi sorgulari
            models.Index(
                fields=['ref_type', 'ref_id'],
                name='sl_ref_idx'
            ),
            # Yon bazli filtreleme (giris/cikis raporlari)
            models.Index(
                fields=['store', 'direction', '-created_on'],
                name='sl_store_dir_date_idx'
            ),
            # Kullanici bazli islem gecmisi
            models.Index(
                fields=['created_by', '-created_on'],
                name='sl_user_date_idx'
            ),
        ]

    def __str__(self):
        direction_symbol = '+' if self.direction == self.Direction.IN else '-'
        return (
            f"{direction_symbol}{self.quantity_gram}g | "
            f"{self.product} | {self.get_reason_display()} | "
            f"{self.created_on}"
        )

    @property
    def total_cost_hs(self) -> Decimal:
        """Toplam islem degeri Has cinsinden."""
        return (self.quantity_gram * self.unit_cost_hs).quantize(Decimal('0.0001'))

    @property
    def total_cost_tl(self) -> Decimal:
        """Toplam islem degeri TL cinsinden."""
        return (self.quantity_gram * self.unit_cost_eur).quantize(Decimal('0.01'))

    def save(self, *args, **kwargs):
        """
        Degismezlik kontrolu: Mevcut kayitlarin guncellenmesini engeller.
        Yeni kayitlar icin normal save islemi yapilir.
        paired_entry guncellenmesine izin verilir (ilk olusturma sonrasi baglama).
        """
        if self.pk:
            # Kayit zaten varsa, sadece paired_entry guncellenmesine izin ver
            allowed_update_fields = kwargs.get('update_fields', None)
            if allowed_update_fields and set(allowed_update_fields) <= {'paired_entry'}:
                super().save(*args, **kwargs)
                return
            elif allowed_update_fields:
                raise ValueError(
                    f"StockLedger kayitlari degismezdir (immutable). "
                    f"Guncelleme girisimi engellendi. "
                    f"Guncellenmek istenen alanlar: {allowed_update_fields}"
                )
            else:
                # update_fields belirtilmemis genel save — sadece paired_entry icin izin ver
                # Bu durumda hata firlatmiyoruz cunku ORM ilk save sonrasi tekrar cagirabiliyor
                pass
        super().save(*args, **kwargs)


# ============================================================================
# FIYAT SAGLAYICI (PRICE PROVIDER) - Coklu API Altyapisi
# ============================================================================

class PriceProvider(models.Model):
    """
    Dis fiyat API saglayicilarinin kaydini tutar.

    Her API saglayici (orn: HaremAltin, GrandBazaar, CBRT, vb.) burada
    bir kayit olarak tanimlanir. Baglanti bilgileri, oncelik sirasi
    ve aktiflik durumu yonetilir.

    Failover mantigi:
        priority alani ile birden fazla API siralangir.
        Ana API (priority=1) basarisiz olursa, sonraki denenecektir.

    Guvenlik:
        API key ve secret alanlari vardir ancak hassas bilgiler icin
        .env dosyasi veya Django settings kullanilmasi onerilir.
        Bu alanlar yedek/yonetim amaclidir.
    """

    class ProviderStatus(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Aktif'
        INACTIVE = 'INACTIVE', 'Pasif'
        ERROR = 'ERROR', 'Hata'
        MAINTENANCE = 'MAINTENANCE', 'Bakim'

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    # --- Saglayici tanim bilgileri ---
    name = models.CharField(
        max_length=100,
        unique=True,
        verbose_name='Saglayici Adi',
        help_text='Benzersiz saglayici adi. Ornek: harem_altin, grand_bazaar, cbrt'
    )

    display_name = models.CharField(
        max_length=200,
        verbose_name='Gorunen Ad',
        help_text='Kullaniciya gosterilecek isim. Ornek: Harem Altin API'
    )

    provider_type = models.CharField(
        max_length=50,
        default='api',
        verbose_name='Saglayici Tipi',
        help_text="'api' (REST API), 'websocket' (WS), 'scraper' (HTML parse), 'manual' (elle giris)"
    )

    # --- API baglanti bilgileri ---
    base_url = models.URLField(
        max_length=500,
        blank=True,
        default='',
        verbose_name='API Base URL',
        help_text='API temel adresi. Ornek: https://api.haremaltin.com/v1'
    )

    api_key_setting = models.CharField(
        max_length=100,
        blank=True,
        default='',
        verbose_name='API Key Settings Adi',
        help_text=(
            'Django settings dosyasindaki API key degisken adi. '
            'Ornek: HAREMALTIN_API_KEY (settings.HAREMALTIN_API_KEY olarak cekilir)'
        )
    )

    api_secret_setting = models.CharField(
        max_length=100,
        blank=True,
        default='',
        verbose_name='API Secret Settings Adi',
        help_text='Django settings dosyasindaki API secret degisken adi.'
    )

    extra_headers = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='Ek HTTP Baslik Bilgileri',
        help_text='JSON formatinda ek basliklar. Ornek: {"x-rapidapi-host": "..."}'
    )

    extra_config = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='Ek Konfigurasyon',
        help_text=(
            'Saglayiciya ozel ek ayarlar. Ornek: '
            '{"timeout": 10, "retry_count": 3, "rate_limit_per_min": 30}'
        )
    )

    # --- Isleyis ayarlari ---
    priority = models.PositiveSmallIntegerField(
        default=1,
        verbose_name='Oncelik',
        help_text='Dusuk numara = yuksek oncelik. Failover sirasini belirler. 1 en yuksek.'
    )

    poll_interval_seconds = models.PositiveIntegerField(
        default=60,
        verbose_name='Sorgulama Araligi (sn)',
        help_text='Bu API kac saniyede bir sorgulanacak'
    )

    cache_ttl_seconds = models.PositiveIntegerField(
        default=30,
        verbose_name='Cache Suresi (sn)',
        help_text='Redis cache te bu API nin fiyatlari kac saniye gecerli kalacak'
    )

    timeout_seconds = models.PositiveIntegerField(
        default=10,
        verbose_name='Timeout (sn)',
        help_text='API cagrisinin maksimum bekleme suresi'
    )

    # --- Durum takibi ---
    status = models.CharField(
        max_length=20,
        choices=ProviderStatus.choices,
        default=ProviderStatus.ACTIVE,
        verbose_name='Durum'
    )

    last_success_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Son Basarili Cagri',
        help_text='Bu API den en son ne zaman basarili fiyat alindi'
    )

    last_error_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Son Hata Zamani'
    )

    last_error_message = models.TextField(
        blank=True,
        default='',
        verbose_name='Son Hata Mesaji'
    )

    consecutive_errors = models.PositiveIntegerField(
        default=0,
        verbose_name='Ardisik Hata Sayisi',
        help_text='Ust uste kac kere hata alindi. Basarili cekim sifirlar.'
    )

    max_consecutive_errors = models.PositiveIntegerField(
        default=5,
        verbose_name='Maks Ardisik Hata',
        help_text='Bu sayiya ulasilirsa saglayici otomatik ERROR durumuna gecer'
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name='Aktif Mi',
        help_text='False ise bu saglayicidan fiyat cekilmez'
    )

    # --- Zaman damgalari ---
    created_on = models.DateTimeField(
        auto_now_add=True,
        verbose_name='Olusturma Tarihi'
    )

    updated_on = models.DateTimeField(
        auto_now=True,
        verbose_name='Son Guncelleme'
    )

    class Meta:
        db_table = 'PriceProvider'
        verbose_name = 'Fiyat Saglayici'
        verbose_name_plural = 'Fiyat Saglayicilar'
        ordering = ['priority', 'name']

        indexes = [
            models.Index(
                fields=['is_active', 'priority'],
                name='priceprov_active_priority_idx'
            ),
            models.Index(
                fields=['status'],
                name='priceprov_status_idx'
            ),
        ]

    def __str__(self):
        status_icon = '✓' if self.status == self.ProviderStatus.ACTIVE else '✗'
        return f"[{status_icon}] {self.display_name} (oncelik: {self.priority})"

    def mark_success(self):
        """Basarili API cagrisi sonrasi durum guncelle."""
        self.last_success_at = timezone.now()
        self.consecutive_errors = 0
        if self.status == self.ProviderStatus.ERROR:
            self.status = self.ProviderStatus.ACTIVE
        self.save(update_fields=[
            'last_success_at', 'consecutive_errors', 'status', 'updated_on'
        ])

    def mark_error(self, error_message: str = ''):
        """Basarisiz API cagrisi sonrasi durum guncelle."""
        self.last_error_at = timezone.now()
        self.last_error_message = error_message[:2000]  # Truncate
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.max_consecutive_errors:
            self.status = self.ProviderStatus.ERROR
        self.save(update_fields=[
            'last_error_at', 'last_error_message',
            'consecutive_errors', 'status', 'updated_on'
        ])


# ============================================================================
# FIYAT TEKLIFI (PRICE QUOTE) - Tarihsel Fiyat Kaydi
# ============================================================================

class PriceQuote(models.Model):
    """
    Her API saglayicisından gelen fiyat kaydi.

    Bu tablo iki amaca hizmet eder:
    1. TARIHSEL KAYIT: Fiyatlarin gecmis kaydi (audit trail).
    2. REDIS YEDEGI: Redis cokerse veya bos ise, son fiyatlar buradan cekilir.

    Veri akisi:
        API cevap -> PriceQuote DB kaydi -> Redis Cache -> Frontend/Backend okuma

    Temizlik:
        30 gunden eski kayitlar periyodik celery task ile silinebilir.
        Aylik ortalamalar ayri bir summary tablosuna alinabilir (Faz 2).
    """

    class MetalType(models.TextChoices):
        GOLD_24K = 'GOLD_24K', 'Has Altin (24 Ayar)'
        GOLD_22K = 'GOLD_22K', '22 Ayar Altin'
        GOLD_18K = 'GOLD_18K', '18 Ayar Altin'
        GOLD_14K = 'GOLD_14K', '14 Ayar Altin'
        GOLD_8K = 'GOLD_8K', '8 Ayar Altin'
        SILVER = 'SILVER', 'Gumus (Generic)'
        # FAZ A: Çoklu Maden entegrasyonu - Has Gümüş ayar kırılımı
        SILVER_999 = 'SILVER_999', 'Has Gumus (999 Ayar)'
        SILVER_925 = 'SILVER_925', '925 Ayar Gumus (Sterling)'
        PLATINUM = 'PLATINUM', 'Platin'
        PALLADIUM = 'PALLADIUM', 'Paladyum'
        USD = 'USD', 'Amerikan Dolari'
        EUR = 'EUR', 'Euro'
        GBP = 'GBP', 'Ingiliz Sterlini'

    class QuoteType(models.TextChoices):
        SPOT = 'SPOT', 'Anlik / Spot'
        DAILY_OPEN = 'DAILY_OPEN', 'Gunluk Acilis'
        DAILY_CLOSE = 'DAILY_CLOSE', 'Gunluk Kapanis'
        MANUAL = 'MANUAL', 'Manuel Giris'

    id = models.BigAutoField(primary_key=True)

    provider = models.ForeignKey(
        PriceProvider,
        on_delete=models.CASCADE,
        related_name='quotes',
        verbose_name='Fiyat Saglayici'
    )

    # --- Fiyat kimligi ---
    metal_type = models.CharField(
        max_length=20,
        choices=MetalType.choices,
        verbose_name='Metal / Doviz Tipi'
    )

    currency_code = models.CharField(
        max_length=20,
        verbose_name='Para Birimi Kodu',
        help_text=(
            'API den gelen orijinal kod. '
            'Ornek: ALTIN, ALTINTRY, USDTRY, HAS_ALTIN, 22_AYAR'
        )
    )

    quote_type = models.CharField(
        max_length=20,
        choices=QuoteType.choices,
        default=QuoteType.SPOT,
        verbose_name='Fiyat Tipi'
    )

    # --- Fiyat degerleri ---
    buy_price_eur = models.DecimalField(
        max_digits=15,
        decimal_places=4,
        verbose_name='Alis (TL)',
        help_text='Alis fiyati TL cinsinden'
    )

    sell_price_eur = models.DecimalField(
        max_digits=15,
        decimal_places=4,
        verbose_name='Satis (TL)',
        help_text='Satis fiyati TL cinsinden'
    )

    buy_price_hs = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=Decimal('0.000000'),
        verbose_name='Alis (Has)',
        help_text='Alis fiyati Has cinsinden (Has Altin bazinda normalize edilmis)'
    )

    sell_price_hs = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=Decimal('0.000000'),
        verbose_name='Satis (Has)',
        help_text='Satis fiyati Has cinsinden (Has Altin bazinda normalize edilmis)'
    )

    # --- Spread ve degisim ---
    spread_eur = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('0.0000'),
        verbose_name='Spread (TL)',
        help_text='Alis-satis farki TL cinsinden (sell - buy)'
    )

    change_rate = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=Decimal('0.0000'),
        verbose_name='Degisim Orani (%)',
        help_text='API den gelen yuzde degisim'
    )

    # --- API ham veri ---
    raw_data = models.JSONField(
        null=True,
        blank=True,
        verbose_name='Ham API Verisi',
        help_text='API cevabinin orjinal hali (debug ve audit icin)'
    )

    # --- Zaman damgasi ---
    quoted_at = models.DateTimeField(
        verbose_name='Fiyat Zamani',
        help_text='Fiyatin gecerli oldugu an (API den gelen veya alinma zamani)'
    )

    created_on = models.DateTimeField(
        auto_now_add=True,
        verbose_name='Kayit Zamani'
    )

    class Meta:
        db_table = 'PriceQuote'
        verbose_name = 'Fiyat Kaydi'
        verbose_name_plural = 'Fiyat Kayitlari'
        ordering = ['-quoted_at']

        indexes = [
            # Ana sorgu: Belirli metal icin son fiyat
            models.Index(
                fields=['metal_type', '-quoted_at'],
                name='pricequote_metal_date_idx'
            ),
            # Saglayici bazli sorgulama
            models.Index(
                fields=['provider', 'metal_type', '-quoted_at'],
                name='pricequote_prov_metal_date_idx'
            ),
            # Para birimi kodu ile arama (eski sistemle uyumluluk)
            models.Index(
                fields=['currency_code', '-quoted_at'],
                name='pricequote_curcode_date_idx'
            ),
            # Temizlik icin: Eski kayitlarin toplu silinmesi
            models.Index(
                fields=['created_on'],
                name='pricequote_created_on_idx'
            ),
        ]

    def __str__(self):
        return (
            f"{self.get_metal_type_display()} | "
            f"Alis: {self.buy_price_eur} TL | "
            f"Satis: {self.sell_price_eur} TL | "
            f"{self.provider.name} @ {self.quoted_at}"
        )

    def save(self, *args, **kwargs):
        """Spread otomatik hesaplama."""
        if self.sell_price_eur and self.buy_price_eur:
            self.spread_eur = self.sell_price_eur - self.buy_price_eur
        super().save(*args, **kwargs)


# ============================================================================
# FIYAT SAGLAYICI - METAL ESLESTIRME (MAPPING)
# ============================================================================

class PriceProviderMapping(models.Model):
    """
    Her API saglayicisinin kendine ozel alan adi/kodu ile
    sistemimizin standart MetalType'ini eslestirir.

    Ornek:
        HaremAltin API'si 'ALTIN' diye gonderir -> bizim sistemde 'GOLD_24K'
        GrandBazaar API'si 'HAS_GOLD' diye gonderir -> bizim sistemde 'GOLD_24K'
        CBRT API'si 'XAU/TRY' diye gonderir -> bizim sistemde 'GOLD_24K'

    Boylece yeni bir API ekledigimizde sadece bu tabloya mapping eklememiz yeterli.
    Kod degisikligi gerektirmez.
    """

    id = models.BigAutoField(primary_key=True)

    provider = models.ForeignKey(
        PriceProvider,
        on_delete=models.CASCADE,
        related_name='mappings',
        verbose_name='Saglayici'
    )

    # API den gelen orijinal kod
    source_code = models.CharField(
        max_length=50,
        verbose_name='Kaynak Kodu',
        help_text='API den gelen ham kod. Ornek: ALTIN, ALTINTRY, XAU/TRY, HAS_GOLD'
    )

    # Bizim standart sistemimize eslestirme
    target_metal_type = models.CharField(
        max_length=20,
        choices=PriceQuote.MetalType.choices,
        verbose_name='Hedef Metal Tipi',
        help_text='Sistemdeki standart metal/doviz kodu'
    )

    # Bu eslestirme aktif mi?
    is_active = models.BooleanField(
        default=True,
        verbose_name='Aktif'
    )

    # API den gelen veriden alis/satis fiyatini cekmek icin alan adlari
    buy_field_name = models.CharField(
        max_length=50,
        default='buy',
        verbose_name='Alis Fiyati Alan Adi',
        help_text='API JSON cevabindaki alis fiyati alan adi. Ornek: buy, alis, buyPrice'
    )

    sell_field_name = models.CharField(
        max_length=50,
        default='sell',
        verbose_name='Satis Fiyati Alan Adi',
        help_text='API JSON cevabindaki satis fiyati alan adi. Ornek: sell, satis, sellPrice'
    )

    class Meta:
        db_table = 'PriceProviderMapping'
        verbose_name = 'Fiyat Eslestirme'
        verbose_name_plural = 'Fiyat Eslestirmeleri'
        ordering = ['provider', 'source_code']

        constraints = [
            # Ayni saglayici icin ayni kaynak kodu tekrar edemez
            UniqueConstraint(
                fields=['provider', 'source_code'],
                name='pricemapping_unique_prov_source'
            ),
        ]

        indexes = [
            models.Index(
                fields=['provider', 'is_active'],
                name='pricemapping_prov_active_idx'
            ),
        ]

    def __str__(self):
        return f"{self.provider.name}: {self.source_code} -> {self.target_metal_type}"
