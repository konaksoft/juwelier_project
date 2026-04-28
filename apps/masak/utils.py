import csv
import io
from .models import MasakOfficialList


def decode_file(file_obj):
    """
    Dosyayı sırasıyla UTF-8, ISO-8859-9 (Türkçe) ve CP1254 (Windows Türkçe)
    olarak okumayı dener.
    """
    file_obj.seek(0)
    raw_data = file_obj.read()

    # Denenecek kodlamalar
    encodings = ['utf-8-sig', 'utf-8', 'cp1254', 'iso-8859-9', 'latin-1']

    for encoding in encodings:
        try:
            return raw_data.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError("Dosya kodlaması (encoding) çözülemedi. Lütfen dosyayı UTF-8 formatında kaydedin.")


def get_csv_reader(io_string):
    """
    Ayırıcıyı (delimiter) otomatik bulmaya çalışır.
    Bulamazsa manuel olarak ';' ve ',' dener.
    """
    # 1. YÖNTEM: Otomatik Sniffer (Daha büyük örneklem ile)
    try:
        sample = io_string.read(4096)  # Örneklem boyutunu artırdık
        io_string.seek(0)
        dialect = csv.Sniffer().sniff(sample)
        return csv.reader(io_string, dialect)
    except csv.Error:
        # Sniffer başarısız olduysa manuel deneme yap
        pass

    # 2. YÖNTEM: Manuel Deneme
    delimiters = [';', ',', '\t']

    for delim in delimiters:
        io_string.seek(0)
        try:
            # İlk satırı oku ve sütun sayısına bak
            reader = csv.reader(io_string, delimiter=delim)
            first_row = next(reader)

            # Eğer satır en az 2 sütuna bölündüyse bu ayırıcı doğrudur diyebiliriz
            if len(first_row) > 1:
                io_string.seek(0)  # Başa dön
                return csv.reader(io_string, delimiter=delim)
        except StopIteration:
            continue
        except Exception:
            continue

    raise ValueError("CSV dosyasının ayırıcı karakteri (virgül veya noktalı virgül) belirlenemedi.")


def import_masak_csv(csv_file, source_type):
    """
    CSV dosyasını okur ve MasakOfficialList tablosuna yazar.
    source_type: 'BMGK', 'FOREIGN', 'INTERNAL'
    """
    try:
        decoded_content = decode_file(csv_file)
    except Exception as e:
        print(f"Encoding hatası: {e}")
        return 0

    io_string = io.StringIO(decoded_content)

    try:
        reader = get_csv_reader(io_string)
    except Exception as e:
        print(f"Delimiter hatası: {e}")
        return 0

    # Başlık satırını atla
    try:
        next(reader, None)
    except Exception:
        pass

    count = 0

    for row in reader:
        # Boş satırları veya eksik sütunları atla
        if not row or len(row) < 2:
            continue

        try:
            name = ""
            identity = ""
            org = ""
            birth_date = ""
            nationality = ""

            # --- FOREIGN (Yabancı) Dosyası için Özel Kontrol ---
            if source_type == 'FOREIGN':
                # B Dosyası (Yabancı Talepler) yapısı bazen değişebiliyor.
                # Beklenen: SIRA[0], AD[1], TCKN[2], UYRUK[3]...
                # Ancak bazen sütunlar kayabilir, basit kontrol:
                if len(row) > 1: name = row[1]
                if len(row) > 2: identity = row[2]
                if len(row) > 3: nationality = row[3]
                if len(row) > 7: birth_date = row[7]
                org = "Yabancı Ülke Talebi"

            elif source_type == 'INTERNAL':  # Dosya C (İç Dondurma)
                if len(row) > 1: name = row[1]
                if len(row) > 3: identity = row[3]
                if len(row) > 4: nationality = row[4]
                if len(row) > 8: birth_date = row[8]
                if len(row) > 10: org = row[10]

            elif source_type == 'BMGK':  # Dosya A (BMGK)
                if len(row) > 1: name = row[1]
                if len(row) > 2: identity = row[2]
                if len(row) > 6: nationality = row[6]
                if len(row) > 10: birth_date = row[10]
                if len(row) > 12: org = row[12]

            # Veri Temizliği
            if not name or name.strip() == '':
                continue

            name = name.strip()
            identity = identity.strip() if identity else ''

            # Veritabanına kaydet
            MasakOfficialList.objects.update_or_create(
                full_name=name,
                source_type=source_type,
                defaults={
                    'identity_info': identity,
                    'organization': org.strip() if org else '',
                    'birth_date': birth_date.strip() if birth_date else '',
                    'nationality': nationality.strip() if nationality else ''
                }
            )
            count += 1

        except Exception as e:
            # Hatalı satırı logla (console) ama işlemi durdurma
            # print(f"Satır işleme hatası: {e} | Satır: {row}")
            continue

    return count