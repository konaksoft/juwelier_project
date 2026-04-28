# apps/products/importers.py
import os
import shutil
import uuid
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, Union, IO

import pandas as pd
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.definitions.categories.models import Categories
from apps.products.models import Products
from apps.suppliers.models import Suppliers
from apps.accounts.models import Stores, Users
from apps.gold_purchases.models import GoldPurchases


def _norm(s: object) -> str:
    """Türkçe diakritikleri ve boşluk sapmalarını normalize et."""
    txt = str(s or "").strip()
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = txt.lower()
    txt = re.sub(r"\s+", " ", txt)
    return txt

def _find_header_row(raw: pd.DataFrame) -> int:
    """İlk 20 satırda 'barkod' geçen bir hücre bul ve o satırı başlık say."""
    for i, row in raw.head(20).iterrows():
        vals = [_norm(v) for v in row.values if pd.notna(v)]
        if any("barkod" in v for v in vals):
            return i
    return 0

def _pick_col(cols, *cands) -> Optional[str]:
    nmap = {_norm(c): c for c in cols}
    keys = [_norm(c) for c in cands]
    # tam eşleşme
    for k in keys:
        if k in nmap:
            return nmap[k]
    # alt string eşleşmesi (örn. 'barkod' ∈ 'barkod no')
    for nc, orig in nmap.items():
        if any(k in nc for k in keys):
            return orig
    return None

def _dec(x) -> Optional[Decimal]:
    if pd.isna(x) or x == "":
        return None
    s = str(x).strip().replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None

def _intish_barcode(val) -> Optional[str]:
    """Excel'in 12345->12345.0 saçmalığını düzelt."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "none"):
        return None
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except Exception:
        pass
    return s

def _to_int_str(x: Optional[Decimal]) -> Optional[str]:
    if x is None:
        return None
    try:
        return str(int(Decimal(x)))
    except Exception:
        return str(x)

@transaction.atomic
def import_excel_as_products_and_purchases(
    file_obj_or_path: Union[str, IO[bytes]],
    *,
    store_id: uuid.UUID,
    created_by: Users,
    default_price_currency: str = "HS",
    image_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if not isinstance(created_by, Users):
        raise ValueError("created_by bir Users instance olmalı.")

    # Excel'i (başlıksız) oku ve başlık satırını bul
    raw = pd.read_excel(file_obj_or_path, sheet_name=0, header=None)
    hdr_idx = _find_header_row(raw)
    header = raw.iloc[hdr_idx].fillna("")
    df = raw.iloc[hdr_idx + 1 :].copy()
    df.columns = header
    df = df.dropna(how="all")

    cols = list(df.columns)

    # Kolon eşleştirmeleri (esnek)
    col_barcode = _pick_col(cols, "Barkod", "Barkod No", "Barkod no", "BARKOD")
    col_supplier = _pick_col(cols, "Tedarikçi", "Tedarikci", "Firma", "Firma Adı", "Firma Adi", "Satıcı", "Satici")
    col_name     = _pick_col(cols, "Ürün", "Urun", "Ürün Adı", "Ürün Adi", "Ürün Cinsi", "Cinsi")

    # Opsiyonel/ek kolonlar
    col_gram     = _pick_col(cols, "Gramaj", "Gram", "Ağırlık", "Agirlik")
    col_milyem   = _pick_col(cols, "Ürün Milyemi", "Milyem", "Ayar")
    col_labor    = _pick_col(cols, "İşçilik Milyemi", "Iscilik Milyemi", "İşçilik")
    col_buy_hs   = _pick_col(cols, "Maliyet(Has)", "Maliyet Has", "Has", "Maliyet - Has")
    col_buy_tl   = _pick_col(cols, "Maliyet(TL)", "Maliyet TL", "TL", "Maliyet - TL")
    col_sale_hs  = _pick_col(cols, "Satış(Has)", "Satis(Has)", "Satış Has")
    col_profit   = _pick_col(cols, "Kar(%)", "Kâr(%)", "Kâr %", "Kar %")
    col_date     = _pick_col(cols, "Tarih")

    # Yedekleme sütunları (opsiyonel)
    col_rfid       = _pick_col(cols, "RFID Kodu", "RFID", "rfid_code")
    col_image_name = _pick_col(cols, "Görsel Dosya Adı", "Gorsel Dosya Adi", "Görsel")

    missing = [n for n,c in [("Barkod", col_barcode),("Ürün",col_name),("Tedarikçi",col_supplier)] if not c]
    if missing:
        raise ValueError(f"Excel başlıkları eksik: {', '.join(missing)}. "
                         f"Bulunan başlıklar: {', '.join(map(str, cols))}")

    # Mağaza kontrolü
    Stores.objects.only("id").get(id=store_id)

    # image_map: barkod → geçici görsel dosya yolu (ZIP import'tan gelir)
    image_map = image_map or {}

    supplier_cache: Dict[str, Suppliers] = {}
    created_cnt = updated_cnt = supplier_created_cnt = purchases_created = 0
    images_restored = 0
    warnings = []

    for _, row in df.iterrows():
        barcode = _intish_barcode(row.get(col_barcode))
        name = str(row.get(col_name) or "").strip() or (barcode or "Ürün")

        # Tedarikçi
        supplier_obj = None
        sup_name = str(row.get(col_supplier) or "").strip()
        if sup_name:
            key = _norm(sup_name)
            if key in supplier_cache:
                supplier_obj = supplier_cache[key]
            else:
                supplier_obj, created = Suppliers.objects.get_or_create(
                    company_name=sup_name,
                    defaults={"store_id": store_id},
                )
                if created:
                    supplier_created_cnt += 1
                supplier_cache[key] = supplier_obj

        # Sayısallar
        buy_hs  = _dec(row.get(col_buy_hs))  if col_buy_hs  else None
        buy_tl  = _dec(row.get(col_buy_tl))  if col_buy_tl  else None
        sale_hs = _dec(row.get(col_sale_hs)) if col_sale_hs else None
        gram    = _dec(row.get(col_gram))    if col_gram    else None
        milyem  = _dec(row.get(col_milyem))  if col_milyem  else None
        labor   = _dec(row.get(col_labor))   if col_labor   else None
        profit  = _dec(row.get(col_profit))  if col_profit  else None

        created_on = None
        if col_date:
            created_on = pd.to_datetime(row.get(col_date), dayfirst=True, errors="coerce")
            if pd.isna(created_on):
                created_on = None
        category = Categories.objects.get(name='Barkodlu Ürünler')
        defaults = {
            "name": name,
            "store_id": store_id,
            "price_currency": default_price_currency,
            "buy_price_hs": buy_hs or Decimal("0"),
            "buy_price_tl": buy_tl or Decimal("0"),
            "sale_price_hs": sale_hs or Decimal("0"),
            "gram": gram or Decimal("0"),
            "product_mileage": _to_int_str(milyem),
            "labor_mileage": _to_int_str(labor),
            "profit": profit or Decimal("0"),
            "created_on": (created_on.to_pydatetime() if created_on is not None else timezone.now()),
            "is_gram_bullion": False,
            "is_active": True,
            "category_id": category.id,
        }

        # RFID değerini oku (opsiyonel)
        rfid_val = ""
        if col_rfid:
            rfid_val = str(row.get(col_rfid) or "").strip()

        if barcode:
            prod, created = Products.objects.get_or_create(barcode=barcode, defaults=defaults)
            if created:
                created_cnt += 1
            else:
                for k, v in defaults.items():
                    if k != "created_on" and v is not None:
                        setattr(prod, k, v)
                prod.save()
                updated_cnt += 1
        else:
            prod = Products.objects.create(**defaults)
            created_cnt += 1

        # ── RFID atama (unique kısıtı koruması) ──
        if rfid_val:
            existing_rfid = Products.objects.filter(rfid_code=rfid_val).exclude(id=prod.id).first()
            if existing_rfid:
                warnings.append(
                    f"{name}: RFID '{rfid_val}' zaten '{existing_rfid.barcode}' ürününe ait, atlanıyor."
                )
            else:
                prod.rfid_code = rfid_val
                prod.save(update_fields=["rfid_code"])
        elif not prod.rfid_code:
            # RFID yoksa otomatik üret
            try:
                from apps.gold_purchases.views import generate_rfid_hex
                prod.rfid_code = generate_rfid_hex()
                prod.save(update_fields=["rfid_code"])
            except Exception:
                pass

        # ── Görsel kurtarma (ZIP import'tan gelen image_map) ──
        if image_map and barcode and barcode in image_map:
            src_image_path = image_map[barcode]
            if os.path.isfile(src_image_path):
                try:
                    _, ext = os.path.splitext(src_image_path)
                    dest_rel = f"Products/CustomProducts/{barcode}{ext}"
                    dest_abs = os.path.join(settings.MEDIA_ROOT, dest_rel)
                    os.makedirs(os.path.dirname(dest_abs), exist_ok=True)
                    shutil.copy2(src_image_path, dest_abs)
                    prod.image.name = dest_rel
                    prod.save(update_fields=["image"])
                    images_restored += 1
                except Exception as e:
                    warnings.append(f"{name}: Görsel kurtarma hatası: {e}")

        if supplier_obj:
            GoldPurchases.objects.create(
                product=prod,
                store_id=store_id,
                supplier=supplier_obj,
                created_by=created_by,
                count_is_status=1,
                is_status=True,
                is_active=True,
                is_deleted=False,
            )
            purchases_created += 1
        else:
            warnings.append(f"{name}: Tedarikçi boş olduğu için GoldPurchases oluşturulmadı.")

    return {
        "rows": int(df.shape[0]),
        "products_created": created_cnt,
        "products_updated": updated_cnt,
        "suppliers_created": supplier_created_cnt,
        "gold_purchases_created": purchases_created,
        "images_restored": images_restored,
        "warnings": warnings,
        "debug": {
            "header_row": int(hdr_idx),
            "resolved_columns": {
                "barcode": col_barcode, "supplier": col_supplier, "name": col_name,
                "gram": col_gram, "milyem": col_milyem, "labor": col_labor,
                "buy_hs": col_buy_hs, "buy_tl": col_buy_tl, "sale_hs": col_sale_hs,
                "profit": col_profit, "date": col_date,
                "rfid": col_rfid, "image_name": col_image_name,
            }
        }
    }