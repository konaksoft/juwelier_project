"""
Müşteri Kimlik Numarası Doğrulama Utility'leri.

- TCKN (T.C. Kimlik Numarası) → 11 hane, resmi matematiksel algoritma
- VKN (Vergi Kimlik Numarası) → 10 hane, sadece format/rakam kontrolü
- Birleşik dispatcher → uzunluğa göre uygun kontrolü çalıştırır
"""
from typing import Tuple


def _is_all_digits(value: str) -> bool:
    """String'in yalnızca 0-9 rakamlarından oluşup oluşmadığını kontrol eder."""
    return bool(value) and value.isdigit()


def validate_tckn(value: str) -> bool:
    """
    Resmi T.C. Kimlik Numarası doğrulama algoritması.

    Kurallar:
      1. Tam 11 rakamdan oluşmalı.
      2. İlk rakam 0 olamaz.
      3. 10. hane denetimi:
         odd_sum  = 1.+3.+5.+7.+9. haneler
         even_sum = 2.+4.+6.+8. haneler
         (odd_sum * 7 - even_sum) mod 10 == 10. hane
      4. 11. hane denetimi:
         ilk 10 hanenin toplamı mod 10 == 11. hane
    """
    if not isinstance(value, str):
        return False
    if len(value) != 11 or not _is_all_digits(value):
        return False
    if value[0] == '0':
        return False

    d = [int(c) for c in value]

    odd_sum = d[0] + d[2] + d[4] + d[6] + d[8]
    even_sum = d[1] + d[3] + d[5] + d[7]

    check_10 = (odd_sum * 7 - even_sum) % 10
    if check_10 != d[9]:
        return False

    check_11 = sum(d[:10]) % 10
    if check_11 != d[10]:
        return False

    return True


def validate_vkn(value: str) -> bool:
    """
    Vergi Kimlik Numarası temel format kontrolü.

    Türkiye'de VKN için kamuya açık standart bir matematiksel algoritma
    yoktur (Maliye Bakanlığı içsel olarak doğrular). Bu nedenle yalnızca
    uzunluk ve karakter tipi kontrolü yapılır:
      - Tam 10 hane
      - Tamamı rakam
    """
    if not isinstance(value, str):
        return False
    return len(value) == 10 and _is_all_digits(value)


def validate_identification_number(value: str) -> Tuple[bool, str]:
    """
    Müşteri kimlik numarası birleşik doğrulayıcısı.

    Davranış:
      - Boş/None giriş → (True, "")  — zorunluluk kontrolü çağıran tarafta yapılır
      - 11 hane        → TCKN algoritması
      - 10 hane        → VKN format kontrolü
      - Diğer uzunluk  → reddedilir

    Returns:
        (is_valid: bool, error_message: str)
        Geçerli ise error_message = "" döner.
    """
    if value is None:
        return True, ""

    value = str(value).strip()
    if not value:
        return True, ""

    if not _is_all_digits(value):
        return False, "Kimlik numarası yalnızca rakamlardan oluşmalıdır."

    length = len(value)

    if length == 11:
        if validate_tckn(value):
            return True, ""
        return False, "Girilen T.C. Kimlik Numarası geçersizdir. Lütfen kontrol ediniz."

    if length == 10:
        if validate_vkn(value):
            return True, ""
        return False, "Girilen Vergi Kimlik Numarası geçersizdir."

    return False, "Kimlik numarası bireysel için 11 hane (TCKN) veya kurumsal için 10 hane (VKN) olmalıdır."
