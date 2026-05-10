import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _


class LiveBoardSettings(models.Model):
    """
    Canlı Piyasa ekranının (index.html) GÖRSEL özelleştirme ayarları.
    Her mağaza için tek bir kayıt (OneToOne).

    NOT: Manuel Has değerleri bu modelde TUTULMAZ.
    O veriler Products tablosundaki buy_price_hs / sale_price_hs alanlarında
    zaten mevcuttur. Fiyat modu (Dernek/Manuel/API) ise StoreConfiguration
    tablosundaki use_manual_has_calculation ve active_pricing_chamber
    alanlarıyla belirlenir.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.OneToOneField(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='live_board_settings',
        verbose_name='Mağaza',
    )

    # Özel ekran adı — kuyumcu mağaza adı yerine farklı bir isim göstermek isterse
    custom_board_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='Özel Ekran Adı',
        help_text='Boş bırakılırsa mağaza adı gösterilir.',
    )

    # Özel logo
    custom_board_logo = models.ImageField(
        upload_to='LiveBoard/logos/',
        null=True,
        blank=True,
        verbose_name='Özel Ekran Logosu',
        help_text='Boş bırakılırsa mağaza logosu gösterilir.',
    )

    # Görünürlük toggle'ları
    show_custom_name = models.BooleanField(
        default=True,
        verbose_name='Özel Mağaza Adı Göster',
    )
    show_custom_logo = models.BooleanField(
        default=True,
        verbose_name='Özel Logo Göster',
    )

    # ── Kitco Canlı Spot Fiyat Panel Ayarları (juwelier_plus port) ──
    show_kitco_section = models.BooleanField(
        default=True,
        verbose_name=_('Kitco Spot Fiyat Panelini Göster'),
        help_text=_(
            'Kitco uluslararası değerli metal spot fiyat panelini '
            '(Altın, Gümüş, Platin, Paladyum, Rodyum) gösterir/gizler.'
        ),
    )

    class KitcoDisplayCurrency(models.TextChoices):
        EUR = 'EUR', _('EUR')
        USD = 'USD', _('USD')
        GBP = 'GBP', _('GBP')
        CHF = 'CHF', _('CHF')
        CAD = 'CAD', _('CAD')
        AUD = 'AUD', _('AUD')
        JPY = 'JPY', _('JPY')

    class KitcoDisplayUnit(models.TextChoices):
        GRAM = 'GRAM', _('Gram')
        OZ = 'OZ', _('Troy Ons')

    kitco_display_currency = models.CharField(
        max_length=8,
        choices=KitcoDisplayCurrency.choices,
        default=KitcoDisplayCurrency.EUR,
        verbose_name=_('Kitco Gösterim Para Birimi'),
        help_text=_('Kitco spot fiyatlarının live board\'da gösterildiği para birimi.'),
    )

    kitco_display_unit = models.CharField(
        max_length=8,
        choices=KitcoDisplayUnit.choices,
        default=KitcoDisplayUnit.GRAM,
        verbose_name=_('Kitco Gösterim Birimi'),
        help_text=_('Kitco spot fiyatlarının live board\'da gösterildiği birim.'),
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'LiveBoardSettings'
        verbose_name = 'Canlı Ekran Ayarı'
        verbose_name_plural = 'Canlı Ekran Ayarları'

    def __str__(self):
        store_label = self.store.title or str(self.store.id)
        return f"Canlı Ekran Ayarları: {store_label}"


class KitcoPriceCache(models.Model):
    """
    Kitco canlı spot fiyat önbelleği (juwelier_plus port).

    Kitco'dan çekilen spot değerli maden fiyatlarını live_board'da
    bilgi amaçlı göstermek için tutan tablo. Üretim fiyatları, maliyet
    ve kâr hesapları bu tablodan türetilmez.

    İZOLASYON: Products, ChamberProductPrice, StockSnapshot, Rates
    tabloları bu modelden tamamen bağımsız çalışır.

    Aynı (metal_type, currency, unit) üçlüsü için tek kayıt; update_or_create
    ile sürekli güncellenir, UniqueConstraint DB seviyesinde garanti eder.
    """

    class MetalType(models.TextChoices):
        GOLD = 'GOLD', _('Altın')
        SILVER = 'SILVER', _('Gümüş')
        PLATINUM = 'PLATINUM', _('Platin')
        PALLADIUM = 'PALLADIUM', _('Paladyum')
        RHODIUM = 'RHODIUM', _('Rodyum')

    class Currency(models.TextChoices):
        USD = 'USD', _('USD')
        EUR = 'EUR', _('EUR')
        GBP = 'GBP', _('GBP')
        CAD = 'CAD', _('CAD')
        AUD = 'AUD', _('AUD')
        JPY = 'JPY', _('JPY')
        CHF = 'CHF', _('CHF')

    class Unit(models.TextChoices):
        OZ = 'OZ', _('Troy Ons')
        GRAM = 'GRAM', _('Gram')

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    metal_type = models.CharField(
        max_length=16,
        choices=MetalType.choices,
        verbose_name=_('Metal'),
    )

    currency = models.CharField(
        max_length=8,
        choices=Currency.choices,
        verbose_name=_('Para Birimi'),
    )

    unit = models.CharField(
        max_length=8,
        choices=Unit.choices,
        default=Unit.OZ,
        verbose_name=_('Birim'),
    )

    bid_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        verbose_name=_('Alış Fiyatı (Bid)'),
        help_text=_(
            "Kitco'dan çekilen ham spot alış fiyatı. "
            "İşçilik ve kâr marjı DAHİL DEĞİLDİR."
        ),
    )

    ask_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        verbose_name=_('Satış Fiyatı (Ask)'),
        help_text=_(
            "Kitco'dan çekilen ham spot satış fiyatı. "
            "İşçilik ve kâr marjı DAHİL DEĞİLDİR."
        ),
    )

    source_timestamp = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Kaynak Zaman Damgası'),
        help_text=_(
            "Kitco'nun ham yanıtındaki piyasa zamanı (originalTime alanı)."
        ),
    )

    last_updated = models.DateTimeField(
        auto_now=True,
        verbose_name=_('Son Güncelleme (DB)'),
    )

    class Meta:
        db_table = 'KitcoPriceCache'
        verbose_name = _('Kitco Fiyat Önbellek Kaydı')
        verbose_name_plural = _('Kitco Fiyat Önbellek Kayıtları')
        constraints = [
            models.UniqueConstraint(
                fields=['metal_type', 'currency', 'unit'],
                name='uniq_kitco_metal_ccy_unit',
            ),
        ]
        indexes = [
            models.Index(
                fields=['metal_type', 'currency', 'unit'],
                name='idx_kitco_lookup',
            ),
        ]

    def __str__(self):
        return f'{self.metal_type} / {self.currency} / {self.unit}'
