from django.db import models
from apps.accounts.models import *
from apps.definitions.categories.models import *
from decimal import Decimal

from apps.stores.models import Stores


class CurrencyChoices(models.TextChoices):
    # Metaller
    HS  = 'HS',  'Has Altın'
    HG  = 'HG',  'Has Gümüş'
    # Yerel
    TRY = 'TRY', 'Türk Lirası'
    # Hotfix 2026-04-27: tasks.py CURRENCY_CODE_MAP'teki tüm dövizler
    # CurrencyChoices listesinde olmalı; aksi halde Products.full_clean()
    # eski OMR/SAR/AED gibi kayıtlar üzerinde "geçerli seçim değil" fırlatır.
    USD = 'USD', 'Amerikan Doları'
    EUR = 'EUR', 'Euro'
    GBP = 'GBP', 'İngiliz Sterlini'
    CAD = 'CAD', 'Kanada Doları'
    QAR = 'QAR', 'Katar Riyali'
    CHF = 'CHF', 'İsviçre Frangı'
    JPY = 'JPY', 'Japon Yeni'
    SAR = 'SAR', 'Suudi Arabistan Riyali'
    AED = 'AED', 'BAE Dirhemi'
    AUD = 'AUD', 'Avustralya Doları'
    KWD = 'KWD', 'Kuveyt Dinarı'
    OMR = 'OMR', 'Umman Riyali'
    RUB = 'RUB', 'Rus Rublesi'
    BGN = 'BGN', 'Bulgar Levası'
    NOK = 'NOK', 'Norveç Kronu'
    SEK = 'SEK', 'İsveç Kronu'
    DKK = 'DKK', 'Danimarka Kronu'
    CNY = 'CNY', 'Çin Yuanı'
    ILS = 'ILS', 'Yeni İsrail Şekeli'
    MAD = 'MAD', 'Fas Dirhemi'
    JOD = 'JOD', 'Ürdün Dinarı'


# ============================================================================
# FAZ A: ÇOKLU MADEN/ÜRÜN ENTEGRASYONU - Materyal Tipi Seçenekleri
# ============================================================================

class MaterialType(models.TextChoices):
    """
    Ürünün fiziksel materyal/kategori tipini belirten bayrak.

    - GOLD    : Mevcut altın ürünleri (DEFAULT, geriye dönük tam uyumlu).
                gold_dry, gold_rate, product_mileage, gram alanlarını kullanır.
                Cari ledger birimi: HS (Has Altın).
    - SILVER  : Gümüş ürünler. Altınla aynı alan yapısını kullanır
                (product_mileage 925/835/800, gram). Cari ledger birimi: HG.
    - WATCH   : Saat. Adet bazlıdır; gram/milyem kavramı yoktur.
                Detaylar: WatchDetail (OneToOne uzantı). Cari ledger: TRY/USD/EUR.
    - DIAMOND : Pırlanta/Elmas. Adet bazlıdır; 4C + Montür + Çoklu Taş.
                Detaylar: DiamondDetail (OneToOne) + DiamondStone (1:N).
                Cari ledger: TRY/USD/EUR.

    Önemli: Mevcut tüm Products kayıtları default='GOLD' ile otomatik olarak
    altın sınıfına düşer. Migration sonrası hiçbir eski kayıt bozulmaz.
    """
    GOLD    = 'GOLD',    'Altın'
    SILVER  = 'SILVER',  'Gümüş'
    WATCH   = 'WATCH',   'Saat'
    DIAMOND = 'DIAMOND', 'Pırlanta'


class Products(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    image = models.ImageField(default="default/default.png", upload_to='Products/CustomProducts/', null=True, blank=True)
    barcode = models.CharField(max_length=10, null=True, blank=True)
    name = models.CharField(max_length=255, blank=True, null=True)
    jewelry_type = models.CharField(max_length=255, blank=True, null=True)
    brand = models.CharField(max_length=255, blank=True, null=True)
    retail_lower_limit = models.DecimalField(max_digits=12, decimal_places=4, blank=True, null=True)
    retail_top_limit = models.DecimalField(max_digits=12, decimal_places=4, blank=True, null=True)
    wholesale_lower_limit = models.DecimalField(max_digits=12, decimal_places=4, blank=True, null=True)
    wholesale_top_limit = models.DecimalField(max_digits=12, decimal_places=4, blank=True, null=True)
    height = models.CharField(max_length=255, blank=True, null=True)
    order = models.CharField(max_length=255, blank=True, null=True)
    price_currency = models.CharField(
        max_length=10, choices=CurrencyChoices.choices, default='HS'
    )
    fixed_labor_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        null=True,
        blank=True,
        verbose_name="Sabit İşçilik Tutarı (Birim)"
    )

    # --- FAZ A: Çoklu Maden/Ürün Bayrağı ---
    # Bu alan, ürünün hangi fiziksel kategoriye ait olduğunu belirtir.
    # GOLD (default) mevcut altın davranışını birebir korur.
    # SILVER aynı gram/milyem alan yapısını kullanır, sadece ledger birimi HG olur.
    # WATCH ve DIAMOND için uzantı tabloları (WatchDetail, DiamondDetail) devreye girer.
    material_type = models.CharField(
        max_length=10,
        choices=MaterialType.choices,
        default=MaterialType.GOLD,
        db_index=True,
        verbose_name="Materyal Tipi",
        help_text=(
            "Ürünün fiziksel kategorisi. Mevcut tüm altın kayıtları "
            "otomatik olarak GOLD değerine düşer. Saat ve Pırlanta için "
            "WatchDetail/DiamondDetail uzantı tablolarına veri girilir."
        ),
    )

    workmanship_type = models.BooleanField(default=True, null=True, blank=True)
    gold_rate = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0.000000'), blank=True, null=True)
    product_mileage = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0.0000'), blank=True, null=True)
    labor_mileage = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0.0000'), blank=True, null=True)
    piece_labor = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), blank=True, null=True)
    rfid_code = models.CharField(max_length=32, blank=True, null=True, unique=True, verbose_name="RFID Kodu")
    ring_size = models.CharField(max_length=10, blank=True, null=True, verbose_name="Alyans Numarası")

    sale_price_hs = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), blank=True,
                                        null=True)
    sale_price_eur = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), blank=True,
                                        null=True)
    product_hs = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), blank=True, null=True)
    buy_price_hs = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), blank=True, null=True)
    buy_price_eur = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), blank=True, null=True)

    gram = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), blank=True, null=True)
    profit = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), blank=True, null=True)

    description = models.TextField(blank=True, null=True)
    currency = models.CharField(max_length=50, blank=True, null=True)
    certificate = models.CharField(max_length=50, default=0, blank=True, null=True)
    gender = models.CharField(max_length=50, default=0, blank=True, null=True)

    category = models.ForeignKey(Categories, on_delete=models.CASCADE,related_name='products', null=True, blank=True)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    created_by = models.ForeignKey(Users, on_delete=models.CASCADE, null=True, blank=True)
    created_on = models.DateTimeField(blank=True, null=True)
    is_scrap = models.BooleanField(default=False, null=True, blank=True)
    is_gram_bullion = models.BooleanField(default=True, null=True, blank=True)
    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)
    is_protected = models.BooleanField(default=False, null=True, blank=True)
    is_completed = models.BooleanField(default=False, null=True, blank=True)

    # --- FAZ 16: Para Birimi Ürün Bayrağı ---
    # TRY, USDTRY, EURTRY gibi para birimi ürünleri gerçek fiziksel ürün değildir.
    # Bu ürünler Stok Yönetimi listesinden gizlenir ve update_product_stock()
    # ile stok hareketi oluşturulmaz. Kasa/banka hareketleri yalnızca
    # Payment tablosu üzerinden takip edilir.
    is_currency = models.BooleanField(
        default=False,
        verbose_name="Para Birimi Ürünü",
        help_text=(
            "True ise bu ürün bir para birimi temsilidir (TRY, USD, EUR vb.). "
            "Stok Yönetimi listesinde gösterilmez ve nakit işlemlerinde "
            "update_product_stock() çağrılmaz."
        ),
    )

    # --- T2 (2026-04-29): Özel Ürün Sıralaması ---
    # Mevcut `order` alanı CharField olduğu için lexicographic sıralama yapıyordu
    # ("10" < "9" gibi). Yeni `display_order` IntegerField, hem Product Index
    # tablosunda hem de Hızlı/Perakende işlem ekranlarında ürün sıralamasını
    # sayısal olarak ve kullanıcı tarafından özelleştirilebilir hale getirir.
    # Per-store izolasyon: Products zaten `store` FK içerir; her mağaza kendi
    # ürün listesinde bağımsız sıralayabilir. db_index sayesinde order_by hızlı.
    display_order = models.IntegerField(
        default=0,
        db_index=True,
        verbose_name="Görünüm Sırası",
        help_text=(
            "Ürün listelerinde gösterim sıralaması (artan). 0 default. "
            "UI'dan drag-drop ile güncellenir."
        ),
    )

    def __str__(self):
        return self.name

    def sale_price_hs_for(self, store) -> Decimal:
        base = self.sale_price_hs or Decimal('0')
        return base.quantize(Decimal('0.001'))

    def sale_price_eur_for(self, store) -> Decimal:
        base = self.sale_price_eur or Decimal('0')
        return base.quantize(Decimal('0.01'))

    # ========================================================================
    # FAZ B2 / ONARIM FAZI 1 - Veri Butunlugu ve Frontend Spoofing Koruma
    # ========================================================================
    # Bu clean() metodu iki kritik acigi kapatir:
    #   1) material_type IMMUTABILITY: Urun bir kez olusturulduktan sonra
    #      material_type'i degistirilemez. Aksi halde WatchDetail/DiamondDetail
    #      orphan kalir ve SupplierLedger gecmisi kirlenir.
    #   2) SANITIZATION (Spoofing Guard): material_type=WATCH/DIAMOND iken DOM
    #      manupulasyonuyla gonderilen gram/gold_rate/product_mileage gibi
    #      altin-ozel alanlar sessizce sifirlanir. StockService validasyonuna
    #      ek ikinci savunma hatti.
    #
    # NOT: clean() yalnizca full_clean() cagrildiginda calisir. Bu nedenle
    # save() override edilerek full_clean() zorunlu hale getirilmistir.
    # Fixture loaddata veya bulk_create gibi ozel akislar icin
    # save(skip_validation=True) ile atlanabilir.
    # ========================================================================
    def clean(self):
        """
        Ürün veri bütünlüğü doğrulamaları.

        1) material_type immutability (update'te değiştirilemez).
        2) WATCH/DIAMOND için altın-özel alanların zorla sıfırlanması.
        3) Negatif gram koruması.
        """
        from django.core.exceptions import ValidationError

        super().clean() if hasattr(super(), 'clean') else None

        # --- Kural 1: IMMUTABILITY ---
        # Eger urun zaten veritabanindaysa (pk var) orijinal material_type
        # ile simdi gelen degeri karsilastir.
        if self.pk:
            try:
                original_type = (
                    type(self).objects
                    .filter(pk=self.pk)
                    .values_list('material_type', flat=True)
                    .first()
                )
                if original_type and original_type != self.material_type:
                    raise ValidationError({
                        'material_type': (
                            f"Ürün tipi (material_type) oluşturulduktan sonra "
                            f"değiştirilemez. Mevcut: '{original_type}', "
                            f"İstenilen: '{self.material_type}'. "
                            f"Tipi değiştirmek yerine yeni bir ürün oluşturun."
                        )
                    })
            except type(self).DoesNotExist:
                # pk var ama DB'de kayit yok - ilk save; sessizce gec.
                pass
            except ValidationError:
                raise
            except Exception:
                # Herhangi bir beklenmedik sorgu hatasinda immutability
                # kontrolunu atla; sistem calismaya devam etsin.
                pass

        # --- Kural 2: SANITIZATION (WATCH / DIAMOND) ---
        # DOM spoofing'e karsi ikinci savunma hatti. Hata firlatmak yerine
        # sessizce sifirla - UX'i bozmamak ve eski formlardan gelen
        # beklenmedik payload'lari tolere etmek icin.
        if self.material_type in (MaterialType.WATCH, MaterialType.DIAMOND):
            self.gram = Decimal('0.000')
            self.gold_rate = Decimal('0.000000')
            self.product_mileage = Decimal('0.0000')
            self.labor_mileage = Decimal('0.0000')
            self.product_hs = Decimal('0.000')
            self.buy_price_hs = Decimal('0.000')
            self.sale_price_hs = Decimal('0.000')
            # piece_labor ve fixed_labor_amount WATCH/DIAMOND icin
            # ANLAMLIDIR - dokunulmaz. Fiyatlandirmasi adet bazlidir.
            # sale_price_eur / buy_price_eur DA DOKUNULMAZ - urun TL/USD
            # cinsinden degerlidir (PIVOT: sale_currency × kur = TL).

        # --- Kural 3: NEGATIF GRAM KORUMASI ---
        if self.gram is not None and self.gram < Decimal('0'):
            raise ValidationError({'gram': "Gram negatif olamaz."})

        # --- Kural 4: Negatif fiyat koruması ---
        for field_name in ('sale_price_eur', 'buy_price_eur',
                           'sale_price_hs', 'buy_price_hs'):
            val = getattr(self, field_name, None)
            if val is not None and val < Decimal('0'):
                raise ValidationError({
                    field_name: f"{field_name} negatif olamaz."
                })

    def save(self, *args, **kwargs):
        """
        full_clean()'i zorunlu hale getirir.

        Fixture loaddata veya bulk_create gibi ozel durumlar icin
        skip_validation=True kwargi ile atlanabilir:

            product.save(skip_validation=True)
        """
        skip = kwargs.pop('skip_validation', False)
        if not skip:
            # full_clean() tum alanlari + clean() metodunu calistirir.
            # exclude listesi bos - tum alanlar dogrulanir.
            try:
                self.full_clean()
            except Exception:
                # Eger full_clean() ValidationError firlatirsa bunu
                # dogrudan yukariya at; diger beklenmedik hatalarda
                # validation'i atla (eski davranisla uyum).
                raise
        super().save(*args, **kwargs)

    class Meta:
        db_table = 'Products'


# ============================================================================
# FAZ A / PIVOT FAZ E (2026-04-23): PIRLANTA DETAY UZANTI TABLOSU — REVİZE
# ============================================================================
# PIVOT Notları:
#   - Mount (Montür Altını) bilgisi eklendi: mount_metal + mount_karat + mount_gram.
#     Bu altın Has hesabına EKLENMEZ; yalnızca etiket/sigorta için metadata.
#   - Satış döviz cinsi + fiyat alanları eklendi (sale_currency, sale_price).
#   - 4C alanları (carat_weight, shape, color_grade, clarity_grade, cut_grade)
#     LEGACY/SUMMARY olarak tutuldu; tekil (single-stone) ürünler için
#     yeterli. ÇOKLU TAŞ ürünlerde DiamondStone (1:N) kullanılır.
#   - certificate_no unique constraint'i KALDIRILDI. Çünkü bazı
#     ürünlerde piece-level cert yok, stone-level cert var (DiamondStone).
# ============================================================================

class DiamondDetail(models.Model):
    """
    Pırlanta ürünler için "ürün-kartı-seviyesi" uzantı kaydı.

    4C (Diamond Grading Standard) — Summary/Legacy alanlar:
      - Carat (Karat)    : Toplam ağırlık. Çoklu taşlarda stones toplamı.
      - Color (Renk)     : D (renksiz) -> N (sarımsı).
      - Clarity (Berrak.): FL (kusursuz) -> I3 (büyük kusur).
      - Cut (Kesim)      : Excellent -> Poor.

    Sertifika (Piece-Level):
      - GIA, IGI, HRD, AGS gibi bağımsız laboratuvarların raporu.
      - certificate_no artık tekil değil (unique kaldırıldı).
      - Taşların kendi sertifikası DiamondStone.certificate_no'da tutulur.

    Montür Altını:
      - mount_karat + mount_gram — Sigorta ve etiket amaçlı.
      - Has Altın hesabına KARIŞMAZ.

    Satış Fiyatı:
      - sale_currency (USD/EUR/GBP/TRY) + sale_price (doviz cinsinde)
      - Satış ekranı: sale_price × günlük_kur = TL fiyat
    """

    class Shape(models.TextChoices):
        ROUND     = 'ROUND',     'Yuvarlak (Round)'
        PRINCESS  = 'PRINCESS',  'Prenses (Princess)'
        OVAL      = 'OVAL',      'Oval'
        MARQUISE  = 'MARQUISE',  'Markiz (Marquise)'
        PEAR      = 'PEAR',      'Damla (Pear)'
        CUSHION   = 'CUSHION',   'Yastık (Cushion)'
        EMERALD   = 'EMERALD',   'Zümrüt (Emerald)'
        ASSCHER   = 'ASSCHER',   'Asscher'
        RADIANT   = 'RADIANT',   'Radiant'
        HEART     = 'HEART',     'Kalp (Heart)'
        BAGUETTE  = 'BAGUETTE',  'Baget'
        OTHER     = 'OTHER',     'Diğer'

    class ColorGrade(models.TextChoices):
        D = 'D', 'D (Renksiz)'
        E = 'E', 'E (Renksiz)'
        F = 'F', 'F (Renksiz)'
        G = 'G', 'G (Neredeyse Renksiz)'
        H = 'H', 'H (Neredeyse Renksiz)'
        I = 'I', 'I (Neredeyse Renksiz)'
        J = 'J', 'J (Neredeyse Renksiz)'
        K = 'K', 'K (Hafif Sarı)'
        L = 'L', 'L (Hafif Sarı)'
        M = 'M', 'M (Hafif Sarı)'
        N = 'N', 'N+ (Sarımsı)'

    class ClarityGrade(models.TextChoices):
        FL    = 'FL',    'FL (Kusursuz)'
        IF    = 'IF',    'IF (İç Kusursuz)'
        VVS1  = 'VVS1',  'VVS1'
        VVS2  = 'VVS2',  'VVS2'
        VS1   = 'VS1',   'VS1'
        VS2   = 'VS2',   'VS2'
        SI1   = 'SI1',   'SI1'
        SI2   = 'SI2',   'SI2'
        I1    = 'I1',    'I1'
        I2    = 'I2',    'I2'
        I3    = 'I3',    'I3'

    class CutGrade(models.TextChoices):
        EXCELLENT = 'EXCELLENT', 'Mükemmel'
        VERY_GOOD = 'VERY_GOOD', 'Çok İyi'
        GOOD      = 'GOOD',      'İyi'
        FAIR      = 'FAIR',      'Orta'
        POOR      = 'POOR',      'Zayıf'

    class CertificateLab(models.TextChoices):
        GIA   = 'GIA',   'GIA (Gemological Institute of America)'
        IGI   = 'IGI',   'IGI (International Gemological Institute)'
        HRD   = 'HRD',   'HRD Antwerp'
        AGS   = 'AGS',   'AGS (American Gem Society)'
        GSI   = 'GSI',   'GSI (Gemological Science International)'
        NONE  = 'NONE',  'Sertifikasız'
        OTHER = 'OTHER', 'Diğer'

    class Fluorescence(models.TextChoices):
        NONE    = 'NONE',    'Yok'
        FAINT   = 'FAINT',   'Hafif'
        MEDIUM  = 'MEDIUM',  'Orta'
        STRONG  = 'STRONG',  'Güçlü'
        VSTRONG = 'VSTRONG', 'Çok Güçlü'

    class MountMetal(models.TextChoices):
        GOLD_YELLOW = 'GOLD_YELLOW', 'Sarı Altın'
        GOLD_WHITE  = 'GOLD_WHITE',  'Beyaz Altın'
        GOLD_ROSE   = 'GOLD_ROSE',   'Rose (Pembe) Altın'
        PLATINUM    = 'PLATINUM',    'Platin'
        SILVER      = 'SILVER',      'Gümüş'
        OTHER       = 'OTHER',       'Diğer'

    class MountKarat(models.TextChoices):
        K8   = '8K',   '8 Ayar (333)'
        K14  = '14K',  '14 Ayar (585)'
        K18  = '18K',  '18 Ayar (750)'
        K22  = '22K',  '22 Ayar (916)'
        K24  = '24K',  '24 Ayar (Has)'
        NONE = 'NONE', 'Montürsüz / Yok'

    class SaleCurrency(models.TextChoices):
        USD = 'USD', 'Amerikan Doları'
        EUR = 'EUR', 'Euro'
        GBP = 'GBP', 'İngiliz Sterlini'
        TRY = 'TRY', 'Türk Lirası'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    product = models.OneToOneField(
        Products,
        on_delete=models.CASCADE,
        related_name='diamond_detail',
        verbose_name="Ürün",
        help_text="Bu detayların ait olduğu Products kaydı (material_type='DIAMOND').",
    )

    # ------------------------------------------------------------------
    # PIVOT: MONTÜR ALTINI (Bilgi amaçlı; Has hesabına girmez)
    # ------------------------------------------------------------------
    mount_metal = models.CharField(
        max_length=20,
        choices=MountMetal.choices,
        default=MountMetal.GOLD_YELLOW,
        blank=True,
        null=True,
        verbose_name="Montür Metali",
        help_text="Pırlantanın oturduğu metal (sarı/beyaz altın, platin, vb.).",
    )

    mount_karat = models.CharField(
        max_length=5,
        choices=MountKarat.choices,
        default=MountKarat.K18,
        blank=True,
        null=True,
        verbose_name="Montür Ayarı",
        help_text="Örn: 18K. Has hesabına EKLENMEZ; yalnızca etiket/sigorta amaçlı.",
    )

    mount_gram = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=Decimal('0.000'),
        null=True,
        blank=True,
        verbose_name="Montür Gramı",
        help_text="Örn: 4.100 gr. Has hesabına EKLENMEZ.",
    )

    # ------------------------------------------------------------------
    # PIVOT: SATIŞ FİYATI (Döviz bazlı)
    # ------------------------------------------------------------------
    sale_currency = models.CharField(
        max_length=5,
        choices=SaleCurrency.choices,
        default=SaleCurrency.USD,
        blank=True,
        null=True,
        verbose_name="Satış Döviz Cinsi",
        help_text="Satış ekranı bu birim × günlük kur ile TL fiyatı hesaplar.",
    )

    sale_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        null=True,
        blank=True,
        verbose_name="Satış Fiyatı (Döviz Cinsinde)",
        help_text="sale_currency biriminde ürünün satış fiyatı.",
    )

    # ------------------------------------------------------------------
    # PIECE-LEVEL 4C SUMMARY (Legacy — tekil taşlı ürünler için)
    # Çoklu taş senaryosunda bu alanlar boş bırakılabilir; DiamondStone kullanılır.
    # total_carat_weight property'si stones toplamını döner (varsa).
    # ------------------------------------------------------------------
    carat_weight = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        null=True,
        blank=True,
        verbose_name="Karat Ağırlığı (Toplam/Summary)",
        help_text="Tekil taşlı ürünlerde bu alan kullanılır. Çoklu taşta stones toplamı geçerlidir.",
    )

    shape = models.CharField(
        max_length=20,
        choices=Shape.choices,
        null=True,
        blank=True,
        verbose_name="Kesim Şekli (Summary)",
    )

    color_grade = models.CharField(
        max_length=2,
        choices=ColorGrade.choices,
        null=True,
        blank=True,
        verbose_name="Renk Derecesi (Summary)",
    )

    clarity_grade = models.CharField(
        max_length=4,
        choices=ClarityGrade.choices,
        null=True,
        blank=True,
        verbose_name="Berraklık Derecesi (Summary)",
    )

    cut_grade = models.CharField(
        max_length=12,
        choices=CutGrade.choices,
        null=True,
        blank=True,
        verbose_name="Kesim Kalitesi (Summary)",
    )

    # ------------------------------------------------------------------
    # PIECE-LEVEL SERTİFİKA (Ürünün bütününe ait — opsiyonel)
    # NOT: unique=True KALDIRILDI. Taşlar kendi sertifikasını DiamondStone'da
    # taşır; piece-level cert opsiyoneldir.
    # ------------------------------------------------------------------
    certificate_lab = models.CharField(
        max_length=10,
        choices=CertificateLab.choices,
        default=CertificateLab.NONE,
        null=True,
        blank=True,
        verbose_name="Sertifika Laboratuvarı (Piece-Level)",
    )

    certificate_no = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        verbose_name="Sertifika Numarası (Piece-Level)",
        help_text="Ürün bütünü için verilmiş sertifika. Taş bazlı sertifika DiamondStone'dadır.",
    )

    # ------------------------------------------------------------------
    # ÜRETİCİ REFERANS KODU (BÖLÜM 1 / P-07 — 2026-04-27)
    # Üretici/tedarikçi tarafından parçaya verilmiş referans kodu.
    # Örn: R2 etiketinde görülen "RAI5.61-27" formatı ({UreticiKodu}{ToplamKarat}-{RefNo}).
    # Bu kod bizim Products.barcode'umuzdan farklıdır; üreticinin kendi
    # iç envanter referansıdır. Etikette ek bilgi olarak basılabilir,
    # iade/tedarikçi iletişiminde anahtar olarak kullanılır.
    # OPSİYONEL — accordion (gelişmiş) alan. Boş bırakılması kayıt akışını engellemez.
    # ------------------------------------------------------------------
    supplier_ref = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        verbose_name="Üretici Referans Kodu",
        help_text="Üretici/tedarikçi tarafından parçaya atanmış referans kodu (örn: RAI5.61-27).",
    )

    # --- Ek Özellikler ---
    fluorescence = models.CharField(
        max_length=10,
        choices=Fluorescence.choices,
        null=True,
        blank=True,
        verbose_name="Floresans",
    )

    depth_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Derinlik (%)",
        help_text="Taşın derinlik oranı, yüzde olarak.",
    )

    table_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Tabla (%)",
        help_text="Taşın tabla genişliği oranı, yüzde olarak.",
    )

    # --- Montaj Bilgisi ---
    is_mounted = models.BooleanField(
        default=True,
        verbose_name="Monte Edilmiş Mi?",
        help_text="Varsayılan True — mount_karat/mount_gram doluysa monte sayılır.",
    )

    # --- Zaman damgaları ---
    created_on = models.DateTimeField(auto_now_add=True, verbose_name="Oluşturma Tarihi")
    updated_on = models.DateTimeField(auto_now=True, verbose_name="Son Güncelleme")

    class Meta:
        db_table = 'DiamondDetail'
        verbose_name = "Pırlanta Detayı"
        verbose_name_plural = "Pırlanta Detayları"
        indexes = [
            models.Index(fields=['certificate_no'], name='diadet_cert_no_idx'),
            models.Index(fields=['shape', 'color_grade', 'clarity_grade'], name='diadet_4c_idx'),
            models.Index(fields=['sale_currency'], name='diadet_salecur_idx'),
            models.Index(fields=['mount_karat'], name='diadet_mountk_idx'),
        ]

    @property
    def total_carat_weight(self) -> Decimal:
        """
        Tüm bağlı DiamondStone satırlarının karat toplamı.
        Stone yoksa summary'deki carat_weight döner.
        """
        if not self.pk:
            return self.carat_weight or Decimal('0')
        stones_total = self.stones.aggregate(
            total=models.Sum('carat_weight')
        )['total']
        if stones_total and stones_total > 0:
            return stones_total
        return self.carat_weight or Decimal('0')

    def __str__(self):
        stones_count = self.stones.count() if self.pk else 0
        if stones_count > 1:
            return f"{self.product} | {stones_count} Taş | {self.total_carat_weight}ct"
        cert = self.certificate_no or 'Sertifikasız'
        return f"{self.product} | {self.carat_weight or 0}ct [{cert}]"


# ============================================================================
# PIVOT FAZ E (2026-04-23): DiamondStone — Çoklu Taş (D1, D2, ...)
# ============================================================================

class DiamondStone(models.Model):
    """
    Bir pırlanta ürününde bulunan her bir taş için ayrı satır.

    Örnek kullanım (bir yüzük):
        DiamondDetail(product=yuzuk)
            ├── DiamondStone(role=CENTER, position=1, carat=0.26, color=F, clarity=SI1)
            └── DiamondStone(role=SIDE,   position=2, carat=0.08, color=F, clarity=SI2)

    Neden ayrı tablo (JSONField yerine)?
        - Kuyumcu etiketlerinde "D1: 0.26 F SI; D2: 0.08 F SI" notasyonu standart.
        - Her taş kendi sertifikasına sahip olabilir (özellikle merkez taş).
        - Fiyatlama/rapor taş bazında yapılır: "F/SI toplam karat", "GIA'lı taşlar".
        - PostgreSQL index'leri, JOIN'ler doğal çalışır.

    Konum Tekilliği:
        UniqueConstraint(diamond_detail, position) — D1 ve D2 aynı üründe
        karışmaz. Silinen bir taşın pozisyonu yeni taşa verilebilir.
    """

    class StoneRole(models.TextChoices):
        CENTER = 'CENTER', 'Merkez Taş (D1)'
        SIDE   = 'SIDE',   'Yan Taş (D2+)'
        ACCENT = 'ACCENT', 'Aksesuar Taş'

    class StoneType(models.TextChoices):
        DIAMOND     = 'DIAMOND',     'Pırlanta'
        EMERALD     = 'EMERALD',     'Zümrüt'
        RUBY        = 'RUBY',        'Yakut'
        SAPPHIRE    = 'SAPPHIRE',    'Safir'
        PEARL       = 'PEARL',       'İnci'
        ALEXANDRITE = 'ALEXANDRITE', 'İskenderit'
        OTHER       = 'OTHER',       'Diğer'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    diamond_detail = models.ForeignKey(
        DiamondDetail,
        on_delete=models.CASCADE,
        related_name='stones',
        verbose_name="Pırlanta Ürünü Detayı",
    )

    stone_type = models.CharField(
        max_length=16,
        choices=StoneType.choices,
        default=StoneType.DIAMOND,
        verbose_name="Taş Türü",
        help_text="Pırlanta veya renkli taş türü. Renkli taşlarda GIA renk skalası yerine serbest renk metni kullanılır.",
    )

    role = models.CharField(
        max_length=10,
        choices=StoneRole.choices,
        default=StoneRole.CENTER,
        verbose_name="Taş Rolü",
        help_text="CENTER = Merkez taş (D1). SIDE = Yan taşlar (D2+). ACCENT = aksesuar.",
    )

    position = models.PositiveSmallIntegerField(
        default=1,
        verbose_name="Sıra No",
        help_text="D1 için 1, D2 için 2, ... Etikette görünen sıra.",
    )

    # --- 4C (Taş bazlı) ---
    carat_weight = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        verbose_name="Karat Ağırlığı",
        help_text="Bu taşa özel karat. Örn: 0.26.",
    )

    shape = models.CharField(
        max_length=20,
        choices=DiamondDetail.Shape.choices,
        default=DiamondDetail.Shape.ROUND,
        blank=True,
        null=True,
        verbose_name="Kesim Şekli",
    )

    color_grade = models.CharField(
        max_length=24,
        blank=True,
        null=True,
        verbose_name="Renk",
        help_text="Pırlanta için GIA skalası (D-N). Renkli taşlar için serbest metin (örn: Yeşil, Kırmızı).",
    )

    clarity_grade = models.CharField(
        max_length=4,
        choices=DiamondDetail.ClarityGrade.choices,
        blank=True,
        null=True,
        verbose_name="Berraklık",
    )

    cut_grade = models.CharField(
        max_length=12,
        choices=DiamondDetail.CutGrade.choices,
        blank=True,
        null=True,
        verbose_name="Kesim",
    )

    # --- Taş-Bazlı Sertifika (Opsiyonel; merkez taşlar genellikle sertifikalı) ---
    certificate_lab = models.CharField(
        max_length=10,
        choices=DiamondDetail.CertificateLab.choices,
        default=DiamondDetail.CertificateLab.NONE,
        blank=True,
        null=True,
        verbose_name="Sertifika Laboratuvarı",
    )

    certificate_no = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        verbose_name="Sertifika No (Bu Taş İçin)",
        help_text="Merkez taşlar genellikle GIA/IGI sertifikasıyla gelir. Yan taşlar sertifikasız olabilir (null).",
    )

    # --- Diğer Metadata ---
    fluorescence = models.CharField(
        max_length=10,
        choices=DiamondDetail.Fluorescence.choices,
        null=True,
        blank=True,
        verbose_name="Floresans",
    )

    notes = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name="Taş Notu",
    )

    # --- Zaman damgaları ---
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'DiamondStone'
        verbose_name = "Pırlanta Taşı"
        verbose_name_plural = "Pırlanta Taşları"
        ordering = ['diamond_detail', 'position']
        constraints = [
            models.UniqueConstraint(
                fields=['diamond_detail', 'position'],
                name='diastone_unique_detail_position',
            ),
        ]
        indexes = [
            models.Index(fields=['diamond_detail'], name='diastone_detail_idx'),
            models.Index(fields=['color_grade', 'clarity_grade'], name='diastone_color_clarity_idx'),
            models.Index(fields=['certificate_no'], name='diastone_cert_idx'),
        ]

    def __str__(self):
        label = f"{self.role} #{self.position}"
        return (
            f"[{label}] {self.carat_weight}ct "
            f"{self.color_grade or '?'}/{self.clarity_grade or '?'}"
        )


# ============================================================================
# FAZ A / PIVOT FAZ E (2026-04-23): SAAT DETAY UZANTI TABLOSU — sale_* eklendi
# ============================================================================

class WatchDetail(models.Model):
    """
    Saat ürünleri için marka/model/seri/satış metadata uzantısı.

    Bir Products kaydının material_type='WATCH' ise bu tabloda bir OneToOne
    karşılığı bulunmalıdır.

    Saat ticaretinde kritik veriler:
      - Marka + Model + Referans No : Ürünün tekil kimliği.
      - Serial No                   : Her saatin üretim seri numarası.
      - Box & Papers                : Saatin orijinal kutu ve belgeleri
                                      mevcut mu (fiyatı büyük ölçüde etkiler).
      - Condition                   : Yeni / İkinci El / Vintage.

    PIVOT FAZ E (2026-04-23):
      - sale_currency + sale_price alanları eklendi.
      - Saat fiyatı genellikle USD/EUR/CHF cinsinden kote edilir.
      - Satış ekranı: sale_price × günlük_kur = TL fiyatı.
    """

    class MovementType(models.TextChoices):
        AUTOMATIC = 'AUTOMATIC', 'Otomatik'
        MANUAL    = 'MANUAL',    'Manuel (Kurmalı)'
        QUARTZ    = 'QUARTZ',    'Quartz (Pilli)'
        SMART     = 'SMART',     'Akıllı (Smart)'
        HYBRID    = 'HYBRID',    'Hibrit'

    class CaseMaterial(models.TextChoices):
        STEEL       = 'STEEL',       'Çelik'
        GOLD_YELLOW = 'GOLD_YELLOW', 'Sarı Altın'
        GOLD_WHITE  = 'GOLD_WHITE',  'Beyaz Altın'
        GOLD_ROSE   = 'GOLD_ROSE',   'Rose (Pembe) Altın'
        PLATINUM    = 'PLATINUM',    'Platin'
        TITANIUM    = 'TITANIUM',    'Titanyum'
        CERAMIC     = 'CERAMIC',     'Seramik'
        BRONZE      = 'BRONZE',      'Bronz'
        OTHER       = 'OTHER',       'Diğer'

    class Condition(models.TextChoices):
        NEW       = 'NEW',       'Sıfır / Yeni'
        LIKE_NEW  = 'LIKE_NEW',  'Sıfır Ayarında'
        GOOD      = 'GOOD',      'İyi Durumda'
        FAIR      = 'FAIR',      'Orta Durumda'
        VINTAGE   = 'VINTAGE',   'Vintage / Antika'
        FOR_PARTS = 'FOR_PARTS', 'Parça İçin'

    class SaleCurrency(models.TextChoices):
        USD = 'USD', 'Amerikan Doları'
        EUR = 'EUR', 'Euro'
        GBP = 'GBP', 'İngiliz Sterlini'
        CHF = 'CHF', 'İsviçre Frangı'
        TRY = 'TRY', 'Türk Lirası'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    product = models.OneToOneField(
        Products,
        on_delete=models.CASCADE,
        related_name='watch_detail',
        verbose_name="Ürün",
        help_text="Bu detayların ait olduğu Products kaydı (material_type='WATCH').",
    )

    # --- Marka / Model / Referans ---
    brand = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Marka",
        help_text="Örn: Rolex, Omega, Patek Philippe, Audemars Piguet.",
    )

    model_name = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        verbose_name="Model Adı",
        help_text="Örn: Submariner, Seamaster, Nautilus, Royal Oak.",
    )

    reference_no = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Üretici Referans No",
        help_text="Üreticinin model referans kodu. Örn: Rolex 126610LN.",
    )

    serial_no = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        verbose_name="Seri Numarası",
        help_text="Saatin tekil üretim seri numarası.",
    )

    # --- Mekanik Özellikler ---
    movement_type = models.CharField(
        max_length=15,
        choices=MovementType.choices,
        null=True,
        blank=True,
        verbose_name="Mekanizma Tipi",
    )

    case_material = models.CharField(
        max_length=15,
        choices=CaseMaterial.choices,
        null=True,
        blank=True,
        verbose_name="Kasa Malzemesi",
    )

    case_diameter = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        verbose_name="Kasa Çapı (mm)",
        help_text="Kasa genişliği mm cinsinden. Örn: 40.0, 41.5.",
    )

    # --- Tarihsel / Durum Bilgileri ---
    year_of_mfg = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Üretim Yılı",
        help_text="Saatin üretildiği yıl (4 haneli).",
    )

    warranty_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Garanti Bitiş Tarihi",
    )

    box_papers = models.BooleanField(
        default=False,
        verbose_name="Box & Papers",
        help_text=(
            "Saatin orijinal kutusu ve/veya garanti belgesi mevcut mu? "
            "İkinci el fiyatlamada kritik bir faktördür."
        ),
    )

    condition = models.CharField(
        max_length=15,
        choices=Condition.choices,
        default=Condition.NEW,
        verbose_name="Durum",
    )

    # ------------------------------------------------------------------
    # PIVOT FAZ E: SATIŞ FİYATI (Döviz bazlı)
    # ------------------------------------------------------------------
    sale_currency = models.CharField(
        max_length=5,
        choices=SaleCurrency.choices,
        default=SaleCurrency.USD,
        blank=True,
        null=True,
        verbose_name="Satış Döviz Cinsi",
        help_text="Satış ekranı bu birim × günlük kur ile TL fiyatı hesaplar.",
    )

    sale_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        null=True,
        blank=True,
        verbose_name="Satış Fiyatı (Döviz Cinsinde)",
        help_text="sale_currency biriminde saatin satış fiyatı.",
    )

    # --- Zaman damgaları ---
    created_on = models.DateTimeField(auto_now_add=True, verbose_name="Oluşturma Tarihi")
    updated_on = models.DateTimeField(auto_now=True, verbose_name="Son Güncelleme")

    class Meta:
        db_table = 'WatchDetail'
        verbose_name = "Saat Detayı"
        verbose_name_plural = "Saat Detayları"
        indexes = [
            models.Index(fields=['brand', 'model_name'], name='watchdet_brand_model_idx'),
            models.Index(fields=['reference_no'], name='watchdet_ref_no_idx'),
            models.Index(fields=['serial_no'], name='watchdet_serial_no_idx'),
            models.Index(fields=['sale_currency'], name='watchdet_salecur_idx'),
        ]

    def __str__(self):
        parts = [p for p in [self.brand, self.model_name, self.reference_no] if p]
        label = ' '.join(parts) if parts else 'Saat'
        return f"{self.product} | {label}"
