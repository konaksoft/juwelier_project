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

    # FAZ 20 (2026-04-30): Ürün bazında görünürlük & sıralama
    # ----------------------------------------------------------------------
    # hidden_items: Live Board'da gizlenen ürün adları listesi (DB name).
    #   Format: ["Eski Çeyrek", "EURTRY", "Reşat Altın"]
    #   Set lookup için JS/Python tarafında set()'e çevrilir.
    #
    # live_board_item_order: Live Board'a özgü sıralama (Products.display_order'dan
    # bağımsız). Hızlı/Perakende ekranındaki sıralamayı bozmadan canlı ekranda
    # farklı bir sıra kurmak için.
    #   Format: {"Yeni Çeyrek": 1, "Eski Çeyrek": 2, "Gram Altın": 3, ...}
    #   Boş dict ise Products.display_order'a fallback edilir.
    hidden_items = models.JSONField(
        default=list,
        blank=True,
        verbose_name='Gizlenen Ürünler',
        help_text='Live Board ekranında gizlenecek ürün adlarının listesi.',
    )
    live_board_item_order = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='Live Board Özel Sıralama',
        help_text='Live Board\'a özgü ürün sıralaması (ad → tamsayı sıra).',
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'LiveBoardSettings'
        verbose_name = 'Canlı Ekran Ayarı'
        verbose_name_plural = 'Canlı Ekran Ayarları'

    def __str__(self):
        store_label = self.store.title or str(self.store.id)
        return f"Canlı Ekran Ayarları: {store_label}"
