# apps/helpers/numbers.py
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

def parse_decimal_locale(val, default="0", places=None):
    """
    Kullanıcıdan gelen (virgül/nokta/boşluk/karma) sayı metnini güvenle Decimal'a çevirir.
    Örnek kabul edilen formatlar:
      '1,2'  '1.2'  '1.234,56'  '1 234,56'  '1,234.56'  '.5'  ',5'  '-.5'  '0,'  '0.'  '1 234'
    Kurallar:
      - Hem ',' hem '.' varsa: en SAĞDAKİ ayırıcı ondalıktır, diğeri binliktir ve silinir.
      - Sadece ',' varsa: virgül ondalık kabul edilir.
      - Sadece '.' varsa: nokta ondalık kabul edilir.
      - Boşluklar (normal ve non-breaking) temizlenir.
      - Boş/None/uygunsuz durumda default döner.
    İsteğe bağlı 'places' verildiğinde, o kadar ondalığa yuvarlar (ROUND_HALF_UP).
    """
    if val is None:
        return Decimal(default)

    s = str(val).strip()
    if not s:
        return Decimal(default)

    # boşlukları sil (normal/nbspace)
    s = s.replace(" ", "").replace("\u00A0", "")

    # işaret al
    sign = ""
    if s and s[0] in "+-":
        sign, s = s[0], s[1:]

    if not s:
        return Decimal(default)

    # Hem virgül hem nokta varsa -> en sağdaki ondalık
    if "," in s and "." in s:
        last_comma = s.rfind(",")
        last_dot = s.rfind(".")
        if last_comma > last_dot:
            # , ondalık => tüm noktalar binlik, sil; virgül => nokta
            s = s.replace(".", "").replace(",", ".")
        else:
            # . ondalık => tüm virgüller binlik, sil
            s = s.replace(",", "")
    else:
        # Sadece virgül varsa: ondalık
        if "," in s:
            s = s.replace(".", "")  # olası binlik noktaları temizle
            s = s.replace(",", ".")
        # Sadece nokta varsa: zaten ondalık kabul ediyoruz (1.234 -> 1.234)

    # Kalan marjinal durumlar
    if s in (".", "", "-"):
        return Decimal(default)

    try:
        d = Decimal(sign + s)
    except InvalidOperation:
        return Decimal(default)

    if places is not None:
        q = Decimal(1).scaleb(-places)  # 10**(-places)
        d = d.quantize(q, rounding=ROUND_HALF_UP)
    return d