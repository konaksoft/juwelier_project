# apps/helpers/image_resize.py
import os
import io
import math
import datetime
from typing import Tuple, Optional
from PIL import Image, ImageOps
from django.core.files.base import ContentFile


def process_image(
        file,
        max_width: int = 1200,
        max_height: int = 1200,
        quality: int = 85,
        max_kb: Optional[int] = 150,  # ← hedef boyut (KB) – None ise devre dışı
        prefer_webp: bool = True,  # WebP destekliyse önceliklendir
        keep_exif: bool = False,  # EXIF tutma (genelde False: daha küçük dosya)
) -> Tuple[str, ContentFile]:
    """
    (1) Görseli EXIF'e göre doğru yöne çevirir.
    (2) En/boy'u max_width/max_height'e sığdırır (en iyi kalite LANCZOS).
    (3) Formatı akıllıca seçer:
        - Alfa/transparan varsa: WebP (lossy) destekleniyorsa -> WEBP; değilse PNG.
        - Alfa yoksa: WEBP (varsa) -> yoksa JPEG.
    (4) max_kb hedefi için kaliteyi ikili arama ile düşürür; yetmezse hafif yeniden boyutlandırır.
    (5) EXIF metadata'yı atar (keep_exif=False).
    """
    # ------------- aç & yön düzelt -------------
    im = Image.open(file)
    im = ImageOps.exif_transpose(im)  # yanlış dönmüş fotoları düzelt

    # ------------- renk modu -------------
    has_alpha = (
            im.mode in ("RGBA", "LA") or
            ("transparency" in im.info)
    )
    # WebP alfa destekler. JPEG desteklemez.
    if not has_alpha and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")

    # ------------- yeniden boyutlandır -------------
    im.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

    # ------------- hedef format belirle -------------
    orig_ext = os.path.splitext(getattr(file, "name", "upload"))[1].lower()
    supports_webp = "WEBP" in Image.registered_extensions().values()
    target_format = None
    target_ext = None

    if has_alpha:
        if prefer_webp and supports_webp:
            target_format, target_ext = "WEBP", ".webp"
        else:
            # PNG şişebilir; yine de alfa varsa güvenli seçim PNG
            target_format, target_ext = "PNG", ".png"
    else:
        if prefer_webp and supports_webp:
            target_format, target_ext = "WEBP", ".webp"
        else:
            target_format, target_ext = "JPEG", ".jpg"

    # ------------- kaydetme yardımcıları -------------
    def _save_to_bytes(img, q):
        """Verilen kaliteyle bytes döndürür (format opsiyonlarıyla)."""
        buf = io.BytesIO()
        save_kwargs = {}
        if target_format == "JPEG":
            # EXIF tutmayalım: küçük dosya
            save_kwargs.update(dict(quality=int(q), optimize=True, progressive=True))
            if not keep_exif and "exif" in img.info:
                img.info.pop("exif", None)
            # JPEG alfa desteklemez
            to_save = img.convert("RGB") if img.mode != "RGB" else img
            to_save.save(buf, format="JPEG", **save_kwargs)
        elif target_format == "WEBP":
            # method: 6 daha agresif optimizasyon
            save_kwargs.update(dict(quality=int(q), method=6))
            # WebP'de de EXIF tutmayalım
            if not keep_exif and "exif" in img.info:
                img.info.pop("exif", None)
            to_save = img
            to_save.save(buf, format="WEBP", **save_kwargs)
        elif target_format == "PNG":
            # kalite parametresi yok; paletli/optimize deneyelim
            # Alfa varsa RGBA; yoksa P/optimize
            if has_alpha:
                to_save = img.convert("RGBA")
            else:
                # renk sayısını kısarak dosyayı küçült
                to_save = img.convert("P", palette=Image.ADAPTIVE, colors=256)
            to_save.save(buf, format="PNG", optimize=True)
        else:
            # emniyet: orijinal formatı dene
            img.save(buf, format=target_format or "PNG")
        return buf.getvalue()

    # ------------- hedef boyuta sıkıştırma -------------
    # PNG'de kalite iterasyonu yok; WebP/JPEG'te ikili arama yapalım.
    q_lo, q_hi = 40, max(quality, 85)
    data = _save_to_bytes(im, quality if target_format in ("JPEG", "WEBP") else None)
    target_bytes = max_kb * 1024 if max_kb else None

    def _fits(b):
        return (target_bytes is None) or (len(b) <= target_bytes)

    # İlk deneme uygunsa döneriz
    if _fits(data):
        filename = _make_filename(target_ext)
        return filename, ContentFile(data)

    # PNG ise: WebP denenebilir (alfa olsa da WebP alfa destekler)
    # PNG çok büyük kaldıysa ve WebP destekleniyorsa otomatik WebP'ye geç
    if target_format == "PNG" and prefer_webp and supports_webp:
        target_format, target_ext = "WEBP", ".webp"
        data = _save_to_bytes(im, quality)

        if _fits(data):
            filename = _make_filename(target_ext)
            return filename, ContentFile(data)

    # JPEG/WEBP ikili arama kalite
    if target_format in ("JPEG", "WEBP"):
        best = None
        lo, hi = q_lo, q_hi
        for _ in range(8):  # 8 tur yeterli
            mid = (lo + hi) // 2
            trial = _save_to_bytes(im, mid)
            if _fits(trial):
                best = (mid, trial)
                hi = mid - 1
            else:
                lo = mid + 1

        if best:
            _, b = best
            if _fits(b):
                filename = _make_filename(target_ext)
                return filename, ContentFile(b)

    # Hâlâ büyükse: bir miktar daha küçültüp tekrar dene
    if target_bytes:
        for _ in range(2):  # 2 kez küçültme denemesi
            factor = math.sqrt(len(data) / target_bytes) * 0.95
            if factor <= 1.02:
                break
            new_w = max(320, int(im.width / factor))
            new_h = max(320, int(im.height / factor))
            if new_w < im.width or new_h < im.height:
                im = im.copy()
                im.thumbnail((new_w, new_h), Image.Resampling.LANCZOS)
                if target_format in ("JPEG", "WEBP"):
                    # tekrar kalite araması
                    best = None
                    lo, hi = q_lo, q_hi
                    for _ in range(8):
                        mid = (lo + hi) // 2
                        trial = _save_to_bytes(im, mid)
                        if _fits(trial):
                            best = (mid, trial)
                            hi = mid - 1
                        else:
                            lo = mid + 1
                    if best:
                        _, data = best
                    else:
                        data = _save_to_bytes(im, quality)
                else:
                    data = _save_to_bytes(im, None)
                if _fits(data):
                    break

    filename = _make_filename(target_ext)
    return filename, ContentFile(data)


def _make_filename(ext: str) -> str:
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{now_str}{ext}"
