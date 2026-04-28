import uuid
from django.db import models


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
    show_currency_section = models.BooleanField(
        default=True,
        verbose_name='Döviz Bölümü Aktif',
    )
    show_sarrafiye_section = models.BooleanField(
        default=True,
        verbose_name='Sarrafiye Bölümü Aktif',
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'LiveBoardSettings'
        verbose_name = 'Canlı Ekran Ayarı'
        verbose_name_plural = 'Canlı Ekran Ayarları'

    def __str__(self):
        store_label = self.store.title or str(self.store.id)
        return f"Canlı Ekran Ayarları: {store_label}"
