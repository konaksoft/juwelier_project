"""
==============================================================================
 FAZ D — Conflict Resolver
==============================================================================

Smart Restore sırasında karşılaşılabilecek 3 ana çakışma türü:

1. Tedarikçi Eşleşmesi (Karar 5: Konservatif)
   - tax_number eşleşiyorsa BİRLEŞTİR
   - Yoksa YENİ kayıt + similarity_warnings'a benzer kayıtlar listele

2. Müşteri Eşleşmesi (Karar 5: Konservatif)
   - identification_number veya tax_number eşleşiyorsa BİRLEŞTİR
   - Sadece phone/ad ile eşleşiyorsa YENİ kayıt + similarity warning

3. Barkod Çakışması
   - Restore edilecek barkod mevcut DB'de var
   - Çözüm: '_restored' suffix + uyarı log
==============================================================================
"""


class ConflictResolver:
    """
    Restore Edge-case'lerini ele alır.
    """

    def __init__(self, store):
        self.store = store
        self._existing_barcodes = None

    # --------------------------------------------------------------------------
    #  Tedarikçi Eşleştirme
    # --------------------------------------------------------------------------
    def match_supplier(self, supplier_data):
        """
        Karar 5: Konservatif eşleşme.
        Sadece tax_number ile eşleşir.

        Args:
            supplier_data: dict (yedekten gelen tedarikçi alanları)

        Returns:
            (matched_supplier | None, similarity_warnings: list)
        """
        from apps.suppliers.models import Suppliers

        warnings = []
        tax_no = (supplier_data.get('tax_number') or '').strip()

        if not tax_no:
            # Vergi No yok — eşleşemez, yeni oluştur (warning ile)
            similar = self._find_similar_suppliers(supplier_data)
            if similar:
                warnings = [self._supplier_warning(s, ['phone', 'company_name']) for s in similar]
            return None, warnings

        # tax_number eşleşmesi
        match = Suppliers.objects.filter(
            store=self.store,
            tax_number=tax_no,
            is_deleted=False,
        ).first()

        if match:
            return match, []

        # tax_number var ama eşleşmedi → yeni oluştur, ama benzer var mı kontrol et
        similar = self._find_similar_suppliers(supplier_data)
        if similar:
            warnings = [self._supplier_warning(s, ['phone', 'company_name']) for s in similar]
        return None, warnings

    def _find_similar_suppliers(self, supplier_data, max_results=3):
        from apps.suppliers.models import Suppliers
        from django.db.models import Q
        phone = (supplier_data.get('phone') or '').strip()
        company = (supplier_data.get('company_name') or '').strip()
        if not phone and not company:
            return []
        q = Q()
        if phone:
            q |= Q(phone=phone)
        if company:
            q |= Q(company_name__iexact=company)
        return list(
            Suppliers.objects.filter(store=self.store, is_deleted=False)
                             .filter(q)[:max_results]
        )

    @staticmethod
    def _supplier_warning(supplier, matched_fields):
        return {
            'existing_id': str(supplier.id),
            'company_name': supplier.company_name or '',
            'tax_number': supplier.tax_number or '',
            'phone': supplier.phone or '',
            'matched_on': matched_fields,
            'match_score': 0.7,  # konservatif tahmin
        }

    # --------------------------------------------------------------------------
    #  Müşteri Eşleştirme
    # --------------------------------------------------------------------------
    def match_customer(self, customer_data):
        """
        Karar 5: Konservatif eşleşme.
        identification_number (TCKN/Pasaport) veya tax_number ile eşleşir.

        Returns:
            (matched_customer | None, similarity_warnings: list)
        """
        from apps.customers.models import Customers

        warnings = []
        id_no = (customer_data.get('identification_number') or '').strip()
        tax_no = (customer_data.get('tax_number') or '').strip()

        # Önce TCKN/Pasaport
        if id_no:
            match = Customers.objects.filter(
                store=self.store,
                identification_number=id_no,
                is_deleted=False,
            ).distinct().first()
            if match:
                return match, []

        # Sonra Vergi No (kurumsal müşteri)
        if tax_no and hasattr(Customers, 'tax_number'):
            try:
                match = Customers.objects.filter(
                    store=self.store,
                    tax_number=tax_no,
                    is_deleted=False,
                ).distinct().first()
                if match:
                    return match, []
            except Exception:
                pass

        # Eşleşme yok — benzer kayıtları topla (warning için)
        similar = self._find_similar_customers(customer_data)
        if similar:
            warnings = [self._customer_warning(c, ['phone', 'first_name+last_name']) for c in similar]
        return None, warnings

    def _find_similar_customers(self, customer_data, max_results=3):
        from apps.customers.models import Customers
        from django.db.models import Q
        phone = (customer_data.get('phone') or '').strip()
        first = (customer_data.get('first_name') or '').strip()
        last = (customer_data.get('last_name') or '').strip()
        q = Q()
        if phone:
            q |= Q(phone=phone)
        if first and last:
            q |= Q(first_name__iexact=first, last_name__iexact=last)
        if not q:
            return []
        return list(
            Customers.objects.filter(store=self.store, is_deleted=False)
                             .filter(q)
                             .distinct()[:max_results]
        )

    @staticmethod
    def _customer_warning(customer, matched_fields):
        return {
            'existing_id': str(customer.id),
            'name': f"{customer.first_name or ''} {customer.last_name or ''}".strip(),
            'phone': customer.phone or '',
            'identification_number': customer.identification_number or '',
            'matched_on': matched_fields,
            'match_score': 0.7,
        }

    # --------------------------------------------------------------------------
    #  Barkod Çakışma
    # --------------------------------------------------------------------------
    def _load_existing_barcodes(self):
        if self._existing_barcodes is None:
            from apps.products.models import Products
            self._existing_barcodes = set(
                Products.objects.filter(store=self.store)
                                .exclude(barcode__isnull=True)
                                .exclude(barcode__exact='')
                                .values_list('barcode', flat=True)
            )
        return self._existing_barcodes

    # Products.barcode max_length — DB VARCHAR sınırı.
    # Çakışma suffixleri (en kısa önce) bu sınıra sığmalıdır.
    BARCODE_MAX_LEN = 10

    def resolve_barcode_conflict(self, barcode):
        """
        Barkod çözümleme:
          1. Orijinal barkod BARCODE_MAX_LEN'i aşıyorsa kısalt.
          2. Kısaltılmış barkod zaten DB'de varsa kısa suffix ekle.
          3. Kısaltma veya suffix durumunda was_changed=True döner.

        Suffix stratejisi (Products.barcode max_length=10):
          _R  → 2 char  → base maks 8
          _R2 → 3 char  → base maks 7  …vb.

        Bu sayede Products.save() içindeki full_clean() max_length
        hatasını üretmez ve DB VARCHAR(10) sınırı aşılmaz.

        Returns:
            (final_barcode, was_changed: bool)
        """
        if not barcode:
            return barcode, False

        existing = self._load_existing_barcodes()
        max_len = self.BARCODE_MAX_LEN

        # Adım 1: max_length'e truncate
        truncated = barcode[:max_len]
        was_changed = truncated != barcode  # uzunsa değişti

        # Adım 2: truncated barkod DB'de yoksa olduğu gibi dön
        if truncated not in existing:
            return truncated, was_changed

        # Adım 3: Çakışma var — kısa suffix ile yer aç
        n = 2
        suffix = '_R'                           # '_R' = 2 char
        base = truncated[:max_len - len(suffix)]
        candidate = base + suffix
        while candidate in existing and n < 100:
            suffix = f'_R{n}'                   # '_R2' = 3 char, vb.
            base = truncated[:max_len - len(suffix)]
            candidate = base + suffix
            n += 1
        existing.add(candidate)
        return candidate, True

    # --------------------------------------------------------------------------
    #  Idempotency Check
    # --------------------------------------------------------------------------
    @staticmethod
    def is_already_restored(idempotency_key):
        """RestoreAuditLog tablosunda bu key var mı?"""
        if not idempotency_key:
            return False
        from apps.backups.models import RestoreAuditLog
        return RestoreAuditLog.objects.filter(
            idempotency_key=idempotency_key
        ).exists()
