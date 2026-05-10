"""
==============================================================================
 FAZ D — ID Mapper (Smart Restore yardımcısı)
==============================================================================

Smart restore sırasında yedekteki UUID'ler ile mevcut DB'deki UUID'ler
farklı olabilir. Bu mapper, FK referanslarını doğru hedeflere yeniden bağlar.

Örnek:
    yedek: supplier.id = "AAA"
    DB'de eşleşen mevcut: supplier.id = "BBB"
    → IdMapper'a [Suppliers, "AAA"] → "BBB" eşlemesi eklenir
    → SupplierLedger.supplier_id yazılırken "BBB" kullanılır

Eşleşme bulunamazsa (eşleşme alanı yetersizse) yeni kayıt oluşturulur ve
yeni UUID map'e eklenir.
==============================================================================
"""


class IdMapper:
    """
    Model bazlı UUID eşleme tutucu.

    Kullanım:
        mapper = IdMapper()
        mapper.add('Suppliers', 'YEDEK_UUID', 'YENI_UUID')
        new_uuid = mapper.resolve('Suppliers', 'YEDEK_UUID')  # → 'YENI_UUID'
    """

    def __init__(self):
        # {model_name: {original_uuid_str: resolved_uuid_str}}
        self._map = {}

    def add(self, model_name, original_id, resolved_id):
        """Yeni eşleme ekle (override eder)."""
        if model_name not in self._map:
            self._map[model_name] = {}
        self._map[model_name][str(original_id)] = str(resolved_id)

    def resolve(self, model_name, original_id):
        """Eşleme döner; yoksa None."""
        if not original_id:
            return None
        return self._map.get(model_name, {}).get(str(original_id))

    def has(self, model_name, original_id):
        return self.resolve(model_name, original_id) is not None

    def to_dict(self):
        """Audit log için serializable dict döner."""
        return dict(self._map)

    def stats(self):
        """{model_name: count} özet."""
        return {k: len(v) for k, v in self._map.items()}
