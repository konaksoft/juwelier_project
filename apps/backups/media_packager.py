"""
==============================================================================
 FAZ B.1 — Media Packager
==============================================================================

Tarih: 2026-05-05
Amaç:
    Tam firma yedeği ZIP formatında alınırken, media/ dizinindeki ilgili
    görselleri (ürün fotoğrafları, müşteri kimlik resimleri, mağaza/firma
    avatar'ları) ZIP içine paketler. Geri yüklemede ters yönde çıkarır.

Karar 2 (Onaylı):
    - Otomatik yedeklerde media DAHIL EDİLMEZ (DB-only, hızlı, küçük).
    - Manuel yedeklerde kullanıcı checkbox ile seçer (taşıma/klonlama için).

Mimari Notlar:
    - Default placeholder görseller (ör. 'default/store.png') ATLANIR — bunlar
      her deploy'da zaten mevcut.
    - Sadece bu firmaya ait mağazalardaki kayıtların görselleri toplanır.
    - Path'ler MEDIA_ROOT'a göre RELATIVE saklanır → restore sırasında aynı
      göreceli yola yazılır (taşınabilirlik).

Eksik / İyileştirilebilir (sonraki fazlar):
    - Gerçek kullanılan diğer ImageField/FileField (testimonials, supports,
      reports, signed_contracts, contract_signatures, devices) bu firmaya
      bağlı değilse atlanır. Eklenmesi istenirse MEDIA_FIELDS sözlüğü genişletilir.
    - Çok büyük dosyaları stream'lemek için zipfile.ZIP_DEFLATED yerine
      ZIP_STORED + chunked write düşünülebilir (şu an memory-friendly).
==============================================================================
"""

import zipfile
from pathlib import Path

from django.conf import settings


# ---- Placeholder/default görseller — atlanır ----------------------------------
# Bu path'ler MEDIA_ROOT'a göredir. Hangi alanın default'u olduğu farketmez —
# string match ile genel olarak skip edilir.
DEFAULT_IMAGE_PATHS = {
    'default/store.png',
    'default/default.png',
}


class MediaPackager:
    """
    Tam firma yedeği için media dosyalarını ZIP içine paketler / çıkarır.

    Kullanım:
        packager = MediaPackager(company, stores_qs)

        # Yedek alma — mevcut ZipFile'a ekle:
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            stats = packager.add_to_zip(zf, base_path='media/')

        # Geri yükleme — ZIP'ten media/ altındaki dosyaları çıkar:
        with zipfile.ZipFile(zip_path) as zf:
            count = MediaPackager.extract_from_zip(zf, settings.MEDIA_ROOT)
    """

    def __init__(self, company, stores_qs):
        self.company = company
        self.stores_qs = stores_qs
        self.media_root = Path(settings.MEDIA_ROOT)

    # --------------------------------------------------------------------------
    #  Toplama
    # --------------------------------------------------------------------------
    def collect_files(self):
        """
        Bu firmaya ait tüm ImageField/FileField dosyalarının
        (relative_path, absolute_path) tuple listesini döner.

        Aynı dosya birden fazla kayıttan referans alınmışsa tek seferlik eklenir.

        Returns:
            list[tuple[str, Path]] — [(rel, abs), ...]
        """
        from apps.products.models import Products
        from apps.customers.models import Customers

        files = []
        seen = set()

        def _add(value):
            """value: ImageFieldFile veya FieldFile — boş/default değilse ekle."""
            if not value:
                return
            rel = str(value)
            if not rel or rel in DEFAULT_IMAGE_PATHS or rel in seen:
                return
            abs_p = self.media_root / rel
            if not abs_p.exists():
                return
            files.append((rel, abs_p))
            seen.add(rel)

        # 1) Company avatar
        if hasattr(self.company, 'avatar'):
            _add(self.company.avatar)

        # 2) Stores avatarları
        for store in self.stores_qs:
            if hasattr(store, 'avatar'):
                _add(store.avatar)

        # 3) Products görselleri
        # NOT: image=='default/default.png' olanlar zaten DEFAULT_IMAGE_PATHS ile atlanır.
        for prod in Products.objects.filter(store__in=self.stores_qs).only('image'):
            _add(prod.image)

        # 4) Customers kimlik görselleri (front + back)
        for cust in Customers.objects.filter(store__in=self.stores_qs).distinct().only(
            'identification_front_image', 'identification_back_image'
        ):
            _add(cust.identification_front_image)
            _add(cust.identification_back_image)

        return files

    # --------------------------------------------------------------------------
    #  ZIP içine ekleme
    # --------------------------------------------------------------------------
    def add_to_zip(self, zip_file, base_path='media/'):
        """
        Bu firmaya ait media dosyalarını ZIP içine, base_path prefix ile ekler.

        Args:
            zip_file: zipfile.ZipFile — açık (yazılabilir) ZIP nesnesi.
            base_path: ZIP içindeki kök prefix (ör. 'media/').

        Returns:
            dict — {'file_count': N, 'total_bytes': N, 'failed': [...]}
        """
        files = self.collect_files()
        total_bytes = 0
        failed = []

        for rel, abs_p in files:
            try:
                arcname = f'{base_path.rstrip("/")}/{rel}'
                zip_file.write(str(abs_p), arcname=arcname)
                total_bytes += abs_p.stat().st_size
            except Exception as e:
                failed.append({'path': rel, 'error': str(e)})

        return {
            'file_count': len(files) - len(failed),
            'total_bytes': total_bytes,
            'failed': failed,
        }

    # --------------------------------------------------------------------------
    #  ZIP'ten çıkarma (statik)
    # --------------------------------------------------------------------------
    @staticmethod
    def extract_from_zip(zip_file, dest_root, base_path='media/'):
        """
        ZIP içinde base_path/ altındaki dosyaları dest_root altına çıkarır.

        Üzerine yazma davranışı: aynı path varsa OVERWRITE eder
        (full restore = wipe & load mantığıyla tutarlı).

        Path traversal güvenliği: ZipSlip saldırısına karşı abs path normalize
        edip dest_root altında kalmasını garanti eder.

        Args:
            zip_file: zipfile.ZipFile — açık ZIP.
            dest_root: str | Path — MEDIA_ROOT (genellikle settings.MEDIA_ROOT).
            base_path: ZIP içindeki kök prefix.

        Returns:
            int — başarılı çıkarılan dosya sayısı.
        """
        dest_root = Path(dest_root).resolve()
        dest_root.mkdir(parents=True, exist_ok=True)
        prefix = base_path.rstrip('/') + '/'

        count = 0
        for name in zip_file.namelist():
            if not name.startswith(prefix) or name == prefix:
                continue
            rel = name[len(prefix):]
            if not rel or rel.endswith('/'):  # klasör girdisi, atla
                continue

            # ZipSlip koruması
            target = (dest_root / rel).resolve()
            try:
                target.relative_to(dest_root)
            except ValueError:
                # dest_root dışına çıkmaya çalışıyor — sessizce atla
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zip_file.open(name) as src, open(target, 'wb') as dst:
                    dst.write(src.read())
                count += 1
            except Exception:
                # Tek dosya hatası tüm restore'u durdurmasın
                continue

        return count
