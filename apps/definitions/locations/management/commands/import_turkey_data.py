import json
import os
import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from apps.definitions.locations.models import City, TaxOffice, District


class Command(BaseCommand):
    help = 'Excel dosyasından İl ve Vergi Dairelerini yükler, ardından JSON dosyasından İlçeleri eşleştirip ekler.'

    def tr_upper(self, text):
        """Türkçe karakter duyarlı büyük harf çevirici"""
        if not text:
            return ""
        text = str(text)
        table = str.maketrans("iığüşöç", "İIĞÜŞÖÇ")
        return text.translate(table).upper().strip()

    def clean_code(self, code_raw):
        """Excel'den gelen veriyi temizler ve 5 haneli string yapar"""
        if pd.isna(code_raw) or not code_raw:
            return None
        # Float gelen veriyi (1201.0) stringe çevirip noktadan kurtar
        s = str(code_raw).split('.')[0]
        return s.zfill(5)

    def handle(self, *args, **kwargs):
        base_dir = settings.BASE_DIR

        # Dosya Yolları
        excel_file_path = os.path.join(base_dir, 'static', 'geo', 'kod-bilgileri-3.xls')
        json_file_path = os.path.join(base_dir, 'static', 'geo', 'turkiye_il_ilce_vergi_daireleri.json')

        self.stdout.write('1. ADIM: Excel verileri işleniyor (Şehirler ve Vergi Daireleri)...')

        if not os.path.exists(excel_file_path):
            self.stdout.write(self.style.ERROR(f"Excel Dosyası bulunamadı: {excel_file_path}"))
            return

        try:
            with transaction.atomic():
                # --- EXCEL İŞLEMLERİ ---
                # Pandas ile Excel'i oku. dtype=str diyerek kodların (01201) bozulmamasını sağlıyoruz.
                # sheet_name=None parametresi ile tüm sayfaları okuruz (dict olarak döner: {'Sayfa1': df, 'Sayfa2': df})
                xls_data = pd.read_excel(excel_file_path, sheet_name=None, dtype=str)

                for sheet_name, df in xls_data.items():
                    self.stdout.write(f"  > Sayfa işleniyor: {sheet_name}")

                    # Sütun isimlerini normalize et (boşlukları sil, büyük harf yap)
                    df.columns = [str(col).strip().upper() for col in df.columns]

                    # Bu sayfanın tipini (Vergi Dairesi mi Malmüdürlüğü mü) sütunlardan anla
                    office_type = 'VD'  # Varsayılan
                    col_code = None
                    col_city = None
                    col_name = None

                    # Vergi Dairesi Listesi Sütun Kontrolü
                    if 'VERGİ DAİRESİ ADI' in df.columns:
                        office_type = 'VD'
                        col_code = 'VD KODU'
                        col_city = 'İLİ'
                        col_name = 'VERGİ DAİRESİ ADI'
                    # Malmüdürlüğü Listesi Sütun Kontrolü
                    elif 'MAL MD ADI' in df.columns:
                        office_type = 'MAL'
                        col_code = 'VDKODU'  # Excelde bazen bitişik yazabilirler, kontrol etmek lazım
                        col_city = 'İL'
                        col_name = 'MAL MD ADI'

                    # Eğer sütunlar eşleşmiyorsa bu sayfayı atla (Örn: Açıklama sayfası olabilir)
                    if not col_name or col_name not in df.columns:
                        self.stdout.write(
                            self.style.WARNING(f"    - '{sheet_name}' sayfası uygun formatta değil, atlanıyor."))
                        continue

                    # Satırları Dön
                    for index, row in df.iterrows():
                        raw_code = row.get(col_code)
                        raw_city = row.get(col_city)
                        raw_name = row.get(col_name)

                        # Veri yoksa geç
                        if pd.isna(raw_code) or pd.isna(raw_city):
                            continue

                        clean_code = self.clean_code(raw_code)
                        city_name = self.tr_upper(raw_city)
                        office_name = self.tr_upper(raw_name)

                        # Plaka kodunu vergi kodunun ilk 2 hanesinden al
                        if clean_code and len(clean_code) >= 2:
                            plate_code = clean_code[:2]
                        else:
                            plate_code = None

                        # A) Şehri Bul veya Oluştur
                        city, created = City.objects.get_or_create(
                            name=city_name,
                            defaults={'plate_code': plate_code}
                        )

                        # Plaka kodu eksikse tamamla
                        if not created and not city.plate_code and plate_code:
                            city.plate_code = plate_code
                            city.save()

                        # B) Vergi Dairesini Oluştur
                        if clean_code:
                            TaxOffice.objects.update_or_create(
                                code=clean_code,
                                defaults={
                                    'city': city,
                                    'name': office_name,
                                    'office_type': office_type
                                }
                            )

                self.stdout.write(self.style.SUCCESS('Excel işlemleri tamamlandı.'))

                # 2. ADIM: JSON DOSYASINDAN İLÇELERİ YÜKLEME
                # ---------------------------------------------------------
                if not os.path.exists(json_file_path):
                    self.stdout.write(self.style.ERROR(f'JSON Dosyası bulunamadı: {json_file_path}'))
                else:
                    self.stdout.write('2. ADIM: JSON dosyasından İlçe bilgileri işleniyor...')

                    with open(json_file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)

                    # JSON yapısı kontrolü (Liste mi Dict mi?)
                    cities_list = data if isinstance(data, list) else data.get('iller', []) or data.get('cities', [])

                    count_districts = 0

                    for item in cities_list:
                        # JSON'daki şehir adını al
                        json_city_name_raw = item.get('name') or item.get('il') or item.get('city')
                        if not json_city_name_raw:
                            continue

                        json_city_name = self.tr_upper(json_city_name_raw)

                        # Veritabanında bu isimle kayıtlı bir şehir var mı? (Excel adımında eklenmiş olmalı)
                        try:
                            city_obj = City.objects.get(name=json_city_name)
                        except City.DoesNotExist:
                            # Excel'de olmayan ama JSON'da olan şehir varsa atla
                            continue

                        # İlçeleri al
                        districts_list = item.get('districts') or item.get('ilceleri') or []

                        for dist_data in districts_list:
                            # İlçe adı string mi obje mi?
                            if isinstance(dist_data, str):
                                dist_name_raw = dist_data
                            else:
                                dist_name_raw = dist_data.get('name') or dist_data.get('ilce')

                            if not dist_name_raw:
                                continue

                            dist_name = self.tr_upper(dist_name_raw)

                            # İlçeyi oluştur
                            District.objects.get_or_create(
                                city=city_obj,
                                name=dist_name
                            )
                            count_districts += 1

                    self.stdout.write(
                        self.style.SUCCESS(f'JSON işlemleri tamamlandı. Toplam {count_districts} ilçe eklendi.'))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Kritik Hata: {str(e)}'))
            import traceback
            self.stdout.write(traceback.format_exc())