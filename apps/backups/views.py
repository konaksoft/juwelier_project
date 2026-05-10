"""
==============================================================================
 Yedekleme Views — FAZ A + B + C + D + E + 60.2
==============================================================================

FAZ A: Tam JSON yedek/restore.
FAZ B: ZIP yedek (+ opsiyonel media) + Excel raporu.
FAZ C: Smart Export (gold_purchases / customers / settings).
FAZ D: Smart Restore (match_or_create + dry-run + idempotency).
FAZ E: Permission sistemi + audit log endpoint.
FAZ 60.2: Chunked Upload (Cloudflare 413 by-pass) + Görsel optimizasyon.

Endpoint Özet:
    --- FAZ A + B (Tam Yedek) ---
    GET  /backups/api/get-all/        Yedek listesi (DataTables)
    POST /backups/api/create/         Yeni yedek (format=json|zip + include_media)
    POST /backups/api/restore/        Geri yükle (otomatik ZIP/JSON tespit)
    GET  /backups/api/check-status/   Async backup durumu
    GET  /backups/download/<uuid>/    Yedek dosyası indir
    GET  /backups/api/export-xlsx/    Anlık Excel raporu indir

    --- FAZ C + D (Smart) ---
    GET  /backups/api/smart/export/   Smart export indir (kind + ids + optimize_media)
    POST /backups/api/smart/restore/  Smart restore (eski tek-istek upload, küçük dosyalar)

    --- FAZ 60.2 (Chunked Upload — büyük dosyalar) ---
    POST /backups/api/smart/upload/init/      Yeni upload oturumu başlat
    POST /backups/api/smart/upload/chunk/     Sıralı chunk gönder
    POST /backups/api/smart/upload/finalize/  Hazır oturumu restore'a yönlendir
    POST /backups/api/smart/upload/abort/     Yarım kalan oturumu iptal et + temizle

    --- FAZ E (Audit) ---
    GET  /backups/api/audit-log/      RestoreAuditLog satırları
==============================================================================
"""

import json
import mimetypes
import os
import threading

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET

from apps.backups.models import CompanyBackup, RestoreAuditLog
from apps.backups.services import BackupService


# ==============================================================================
#  Permission Yardımcıları (FAZ E)
# ==============================================================================

def is_superuser(user):
    return user.is_authenticated and user.is_superuser


def _has_perm(user, code):
    """
    Mağaza yöneticisi rolüne atanmış permission kontrolü.
    Superuser her zaman geçer.

    Doğru pattern (decorators.py ile aynı):
        user.role_id → RoleDetail.role_id + permission__code + status=True
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    try:
        from apps.roles.models import RoleDetail
        role_id = getattr(user, 'role_id', None)
        if not role_id:
            return False
        return RoleDetail.objects.filter(
            role_id=role_id,
            permission__code=code,
            status=True,
        ).exists()
    except Exception:
        return False


def _user_can_create_backup(user):
    return is_superuser(user) or _has_perm(user, 'BACKUPS_CREATE')


def _user_can_restore_full(user):
    # TEHLİKELİ — superuser veya BACKUPS_RESTORE_FULL
    return is_superuser(user) or _has_perm(user, 'BACKUPS_RESTORE_FULL')


def _user_can_restore_smart(user):
    return is_superuser(user) or _has_perm(user, 'BACKUPS_RESTORE_SMART') or _has_perm(user, 'BACKUPS_RESTORE_FULL')


def _user_can_view_audit(user):
    return is_superuser(user) or _has_perm(user, 'BACKUPS_VIEW_AUDIT')


# ==============================================================================
#  FAZ A + B — Yedek Listele / İndir / Async Durum
# ==============================================================================
@login_required
@require_GET
def get_company_backups(request):
    if not _user_can_create_backup(request.user):
        return JsonResponse({'data': [], 'error': 'Yetki yok.'}, status=403)

    company_id = request.GET.get('company_id')
    if not company_id:
        return JsonResponse({'data': []})

    backups = CompanyBackup.objects.filter(company_id=company_id).order_by('-created_at')

    data = []
    for b in backups:
        file_name = b.backup_file.name if b.backup_file else ''
        is_zip = file_name.lower().endswith('.zip')
        is_full_zip = '_full_' in file_name.lower() and is_zip

        if is_full_zip:
            fmt_label = 'ZIP + Media'
        elif is_zip:
            fmt_label = 'ZIP (DB)'
        elif file_name.lower().endswith('.json'):
            fmt_label = 'JSON'
        else:
            fmt_label = '-'

        data.append({
            'id': str(b.id),
            'created_at': b.created_at.strftime('%Y-%m-%d %H:%M'),
            'file_size': b.file_size or '0 MB',
            'created_by': b.created_by_user or '-',
            'note': b.note or '-',
            'format': fmt_label,
            'status': b.status,
            'download_url': f"/backups/download/{b.id}/",
        })

    return JsonResponse({'data': data})


@login_required
@require_POST
def restore_backup(request):
    if not _user_can_restore_full(request.user):
        return JsonResponse({'result': False, 'error_msg': 'Tam geri yükleme yetkiniz yok.'}, status=403)

    backup_id = request.POST.get('backup_id')
    company_id = request.POST.get('company_id')
    store_id = request.POST.get('store_id') or None  # opsiyonel — per-store kapsam

    if not backup_id or not company_id:
        return JsonResponse({'result': False, 'error_msg': 'Parametreler eksik.'})

    service = BackupService(company_id, store_id=store_id)
    success, msg = service.restore_backup(backup_id, user=request.user)

    if success:
        return JsonResponse({'result': True, 'msg': msg})
    return JsonResponse({'result': False, 'error_msg': msg})


@login_required
def download_backup(request, backup_id):
    if not _user_can_create_backup(request.user):
        raise Http404("Yetki yok.")

    backup = get_object_or_404(CompanyBackup, id=backup_id)
    if not backup.backup_file:
        raise Http404("Dosya bulunamadı")

    file_path = backup.backup_file.path
    if os.path.exists(file_path):
        with open(file_path, 'rb') as fh:
            mime_type, _ = mimetypes.guess_type(file_path)
            response = HttpResponse(
                fh.read(),
                content_type=mime_type or 'application/octet-stream',
            )
            response['Content-Disposition'] = (
                f'attachment; filename="{os.path.basename(file_path)}"'
            )
            return response
    raise Http404("Fiziksel dosya sunucuda bulunamadı.")


def _run_backup_in_background(company_id, user_id, note, backup_record_id,
                              fmt='json', include_media=False, store_id=None):
    """Arka plan thread'i — yedek üretir, PENDING kaydı güncelle."""
    from apps.backups.models import CompanyBackup
    from apps.backups.services import BackupService
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id) if user_id else None
        service = BackupService(company_id, store_id=store_id)

        if fmt == 'zip':
            real_backup = service.create_backup_zip(
                note=note, user=user, include_media=include_media
            )
        else:
            real_backup = service.create_backup(note=note, user=user)

        if real_backup:
            pending_backup = CompanyBackup.objects.get(id=backup_record_id)
            pending_backup.backup_file = real_backup.backup_file
            pending_backup.file_size = real_backup.file_size
            pending_backup.status = 'COMPLETED'
            pending_backup.save()
            real_backup.delete()
        else:
            raise Exception("Servis yedek oluşturamadı")

    except Exception as e:
        try:
            b = CompanyBackup.objects.get(id=backup_record_id)
            b.status = 'FAILED'
            b.note = f"Hata: {str(e)}"
            b.save()
        except Exception:
            pass


@login_required
@require_POST
def create_backup(request):
    if not _user_can_create_backup(request.user):
        return JsonResponse({'result': False, 'error_msg': 'Yetki yok.'}, status=403)

    company_id = request.POST.get('company_id')
    store_id = request.POST.get('store_id') or None   # opsiyonel — per-store kapsam
    note = request.POST.get('note', '')
    fmt = (request.POST.get('format') or 'json').strip().lower()
    include_media_raw = (request.POST.get('include_media') or 'false').strip().lower()
    include_media = include_media_raw in ('1', 'true', 'yes', 'on')

    if fmt not in ('json', 'zip'):
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz format.'})
    if not company_id:
        return JsonResponse({'result': False, 'error_msg': 'Firma ID eksik.'})

    backup = CompanyBackup.objects.create(
        company_id=company_id,
        note=note,
        created_by_user=str(request.user),
        status='PENDING',
        file_size='Hesaplanıyor...',
    )

    t = threading.Thread(
        target=_run_backup_in_background,
        args=(company_id, request.user.id, note, backup.id, fmt, include_media),
        kwargs={'store_id': store_id},
    )
    t.daemon = True
    t.start()

    return JsonResponse({
        'result': True,
        'msg': f"Yedekleme başlatıldı ({fmt.upper()}{', media dahil' if include_media else ''}).",
        'backup_id': backup.id,
    })


@login_required
@require_GET
def check_backup_status(request):
    backup_id = request.GET.get('backup_id')
    try:
        backup = CompanyBackup.objects.get(id=backup_id)
        return JsonResponse({
            'status': backup.status,
            'download_url': (
                f"/backups/download/{backup.id}/"
                if backup.status == 'COMPLETED' else None
            ),
        })
    except CompanyBackup.DoesNotExist:
        return JsonResponse({'status': 'FAILED', 'error': 'Kayıt bulunamadı'})


# ==============================================================================
#  Yedek Sil
# ==============================================================================
@login_required
@require_POST
def delete_backup(request):
    """
    Yedek kaydını ve fiziksel dosyayı siler.

    Kısıtlar:
      - Silme yetkisi: can_create_backup (yedek yönetimi yetkisi)
      - RestoreAuditLog PROTECT: bu yedekten geri yükleme yapılmışsa
        Django ProtectedError fırlatır → kullanıcıya bilgi ver.
      - Fiziksel dosya da diskten silinir.

    POST params:
        backup_id  — UUID
    """
    if not _user_can_create_backup(request.user):
        return JsonResponse({'result': False, 'error_msg': 'Yedek silme yetkiniz yok.'}, status=403)

    backup_id = request.POST.get('backup_id', '').strip()
    if not backup_id:
        return JsonResponse({'result': False, 'error_msg': 'backup_id eksik.'})

    try:
        backup = CompanyBackup.objects.get(id=backup_id)
    except CompanyBackup.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Yedek bulunamadı.'})

    # Dosya yolunu kaydet (delete sonrası erişilemez)
    file_path = None
    if backup.backup_file:
        try:
            file_path = backup.backup_file.path
        except Exception:
            file_path = None

    try:
        backup.delete()
    except Exception as e:
        # PROTECT FK: Bu yedekten geri yükleme yapılmış — silinemez
        err = str(e)
        if 'ProtectedError' in type(e).__name__ or 'PROTECT' in err.upper():
            return JsonResponse({
                'result': False,
                'error_msg': 'Bu yedek daha önce geri yükleme için kullanılmış. Audit kaydı korunduğundan silinemez.',
            })
        return JsonResponse({'result': False, 'error_msg': f'Silme hatası: {err}'})

    # Fiziksel dosyayı diskten kaldır
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception:
            pass  # DB kaydı silindi; disk hatası kritik değil

    return JsonResponse({'result': True, 'msg': 'Yedek silindi.'})


# ==============================================================================
#  FAZ B — Excel Raporu
# ==============================================================================
@login_required
@require_GET
def export_xlsx(request):
    if not _user_can_create_backup(request.user):
        raise Http404("Yetki yok.")

    from apps.backups.xlsx_exporter import XlsxExportService

    company_id = request.GET.get('company_id')
    if not company_id:
        raise Http404("Firma ID gerekli.")

    try:
        exporter = XlsxExportService(company_id)
        xlsx_bytes = exporter.export()
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': f'Excel üretilemedi: {e}'})

    filename = f"backup_report_{company_id}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    response = HttpResponse(
        xlsx_bytes,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ==============================================================================
#  FAZ C — Smart Export (Anlık İndirme)
# ==============================================================================
@login_required
@require_GET
def smart_export(request):
    """
    Smart export — anlık paket indirir, kayıt etmez.

    Query params:
        store_id      — zorunlu
        kind          — 'gold_purchases' | 'customers' | 'settings'
        ids           — opsiyonel virgülle ayrılmış UUID listesi
        format        — 'zip' (default — manifest+payload+media) | 'json' (legacy)
        include_media — 'true' (default) | 'false' — sadece zip için anlamlı.
                        Ürün görselleri (Products.image) ve müşteri kimlik
                        görselleri (Customers.identification_*_image) ZIP'e
                        dahil edilir.
    """
    if not _user_can_create_backup(request.user):
        return JsonResponse({'result': False, 'error_msg': 'Yetki yok.'}, status=403)

    from apps.backups.smart_export import SmartExportService

    store_id = request.GET.get('store_id')
    kind = (request.GET.get('kind') or '').strip().lower()
    ids_raw = request.GET.get('ids') or ''
    ids = [x.strip() for x in ids_raw.split(',') if x.strip()] if ids_raw else None

    fmt = (request.GET.get('format') or 'zip').strip().lower()
    if fmt not in ('zip', 'json'):
        fmt = 'zip'

    include_media_raw = (request.GET.get('include_media') or 'true').strip().lower()
    include_media = include_media_raw in ('1', 'true', 'yes', 'on')

    # FAZ 60.2: Görsel optimizasyon (Pillow ile yeniden kodla)
    optimize_media_raw = (request.GET.get('optimize_media') or 'true').strip().lower()
    optimize_media = optimize_media_raw in ('1', 'true', 'yes', 'on')

    try:
        optimize_max_dim = int(request.GET.get('optimize_max_dim') or 1024)
        optimize_quality = int(request.GET.get('optimize_quality') or 75)
    except (TypeError, ValueError):
        optimize_max_dim, optimize_quality = 1024, 75

    # Sınırla — kötü niyetli/kazara çok büyük değerler engellensin
    optimize_max_dim = max(256, min(4096, optimize_max_dim))
    optimize_quality = max(30, min(95, optimize_quality))

    if not store_id:
        return JsonResponse({'result': False, 'error_msg': 'store_id gerekli.'})

    try:
        svc = SmartExportService(store_id)
        if kind == 'gold_purchases':
            payload = svc.export_gold_purchases(gold_purchase_ids=ids)
        elif kind == 'customers':
            payload = svc.export_customers(customer_ids=ids)
        elif kind == 'settings':
            payload = svc.export_settings()
            # Settings paketinde görsel yok — include_media False'a düşür
            include_media = False
        else:
            return JsonResponse({
                'result': False,
                'error_msg': f"Geçersiz kind: {kind}. (gold_purchases | customers | settings)",
            })

        timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')

        if fmt == 'json':
            # Legacy düz JSON
            data_bytes = SmartExportService.to_json_bytes(payload)
            content_type = 'application/json'
            filename = f"smart_{kind}_{store_id}_{timestamp}.json"
        else:
            # ZIP paket (manifest + payload + media)
            data_bytes, _stats = svc.export_as_zip(
                payload, kind,
                include_media=include_media,
                optimize_media=optimize_media,
                optimize_max_dim=optimize_max_dim,
                optimize_quality=optimize_quality,
            )
            content_type = 'application/zip'
            if include_media:
                suffix = '_full_opt' if optimize_media else '_full'
            else:
                suffix = '_db'
            filename = f"smart_{kind}_{store_id}_{timestamp}{suffix}.zip"

    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': f'Smart export hatası: {e}'})

    response = HttpResponse(data_bytes, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ==============================================================================
#  FAZ D — Smart Restore (file upload + dry-run)
# ==============================================================================
@login_required
@require_POST
def smart_restore(request):
    """
    Smart Restore — yüklenen JSON paketini merge mantığıyla yükler.

    POST data:
        store_id  — zorunlu
        backup_id — opsiyonel (hangi yedekten geldi, audit için)
        dry_run   — 'true' | 'false' (default false)
        file      — upload edilen JSON paketi

    Returns:
        Smart restore raporu JSON.
    """
    if not _user_can_restore_smart(request.user):
        return JsonResponse({'result': False, 'error_msg': 'Smart restore yetkiniz yok.'}, status=403)

    from apps.backups.smart_restore import SmartRestoreService

    store_id = request.POST.get('store_id')
    backup_id = request.POST.get('backup_id') or None
    dry_run_raw = (request.POST.get('dry_run') or 'false').strip().lower()
    dry_run = dry_run_raw in ('1', 'true', 'yes', 'on')

    upload = request.FILES.get('file')
    if not upload:
        return JsonResponse({'result': False, 'error_msg': 'Paket dosyası yüklenmedi.'})
    if not store_id:
        return JsonResponse({'result': False, 'error_msg': 'store_id gerekli.'})

    try:
        raw = upload.read()
        # restore_from_payload artık ZIP veya JSON ham bytes'ı kendisi parse eder.
        # ZIP magic byte (PK\x03\x04) ile tespit eder; ZIP ise media/'yı çıkarır.
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': f'Dosya okunamadı: {e}'})

    try:
        svc = SmartRestoreService(store_id, backup_id=backup_id)
        report = svc.restore_from_payload(raw, user=request.user, dry_run=dry_run)
    except ValueError as ve:
        return JsonResponse({'result': False, 'error_msg': f'Paket hatası: {ve}'})
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': f'Smart restore hatası: {e}'})

    return JsonResponse({'result': True, 'report': report, 'dry_run': dry_run})


# ==============================================================================
#  FAZ 60.2 — Chunked Upload (Cloudflare 413 by-pass)
# ==============================================================================
#
# Akış:
#   1) /upload/init/      → upload_id döner (DB kaydı + boş geçici dosya)
#   2) /upload/chunk/     → her sıralı chunk (5-10 MB) ayrı POST → progress
#   3) /upload/finalize/  → tüm parçalar geldikten sonra smart restore çalışır
#   4) /upload/abort/     → kullanıcı iptali (geçici dosya silinir)
#
# Cloudflare body limitini geçmemek için her POST <100 MB. Aslında client 5
# MB chunk gönderir (büyük güvenlik marjı). Sunucu hard cap 10 MB (settings).
# ==============================================================================

@login_required
@require_POST
def chunked_upload_init(request):
    """
    Yeni parçalı yükleme oturumu başlat.

    POST params:
        store_id      — zorunlu (UUID)
        filename      — zorunlu (orijinal dosya adı, sanitize edilir)
        total_size    — zorunlu (byte)
        chunk_size    — zorunlu (byte) — client tarafı sabit chunk boyutu
        backup_id     — opsiyonel (audit referans)

    Response:
        {result: True, upload_id, total_chunks, chunk_size, expires_at}
    """
    if not _user_can_restore_smart(request.user):
        return JsonResponse(
            {'result': False, 'error_msg': 'Smart restore yetkiniz yok.'},
            status=403,
        )

    from apps.backups.chunked_upload import (
        ChunkedUploadService, ChunkedUploadError,
    )

    store_id = request.POST.get('store_id')
    filename = request.POST.get('filename') or 'upload.bin'
    total_size = request.POST.get('total_size')
    chunk_size = request.POST.get('chunk_size')
    backup_id = request.POST.get('backup_id') or None

    if not store_id:
        return JsonResponse({'result': False, 'error_msg': 'store_id gerekli.'})

    try:
        session = ChunkedUploadService.init(
            user=request.user,
            store_id=store_id,
            filename=filename,
            total_size=total_size,
            chunk_size=chunk_size,
            backup_id=backup_id,
        )
    except ChunkedUploadError as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=400)
    except Exception as e:
        return JsonResponse(
            {'result': False, 'error_msg': f'Oturum başlatılamadı: {e}'},
            status=500,
        )

    return JsonResponse({
        'result': True,
        'upload_id': str(session.id),
        'filename': session.filename,
        'total_size': session.total_size,
        'total_chunks': session.total_chunks,
        'chunk_size': session.chunk_size,
        'expires_at': session.expires_at.isoformat(),
    })


@login_required
@require_POST
def chunked_upload_chunk(request):
    """
    Tek bir chunk yaz. Sıralı çağrılmalı (chunk_index = received_chunks).

    POST params:
        upload_id    — zorunlu (UUID)
        chunk_index  — zorunlu (0-based)
        chunk        — zorunlu (multipart file)

    Response:
        {result, received_chunks, total_chunks, progress_percent, status}
    """
    if not _user_can_restore_smart(request.user):
        return JsonResponse(
            {'result': False, 'error_msg': 'Smart restore yetkiniz yok.'},
            status=403,
        )

    from apps.backups.chunked_upload import (
        ChunkedUploadService, ChunkedUploadError,
        SessionNotFound, SessionForbidden, SessionTerminated, InvalidChunk,
    )

    upload_id = request.POST.get('upload_id')
    chunk_index = request.POST.get('chunk_index')
    chunk_file = request.FILES.get('chunk')

    if not upload_id or chunk_index is None or not chunk_file:
        return JsonResponse({
            'result': False,
            'error_msg': 'upload_id, chunk_index ve chunk gerekli.',
        }, status=400)

    try:
        # Tek chunk en fazla 10 MB — RAM'e read() güvenli
        raw = chunk_file.read()
    except Exception as e:
        return JsonResponse(
            {'result': False, 'error_msg': f'Chunk okunamadı: {e}'},
            status=400,
        )

    try:
        progress = ChunkedUploadService.append_chunk(
            upload_id=upload_id,
            chunk_index=chunk_index,
            raw_bytes=raw,
            user=request.user,
        )
    except SessionNotFound as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=404)
    except SessionForbidden as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=403)
    except SessionTerminated as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=409)
    except InvalidChunk as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=400)
    except ChunkedUploadError as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=400)
    except Exception as e:
        return JsonResponse(
            {'result': False, 'error_msg': f'Beklenmeyen hata: {e}'},
            status=500,
        )

    return JsonResponse({'result': True, **progress})


@login_required
@require_POST
def chunked_upload_finalize(request):
    """
    Hazır oturumu Smart Restore'a yönlendir.

    POST params:
        upload_id   — zorunlu
        dry_run     — 'true' | 'false' (default false)
        backup_id   — opsiyonel (audit, init'te verilmediyse burada verilebilir)

    Response:
        Smart restore raporu (success, dry_run, created_*, ...).
    """
    if not _user_can_restore_smart(request.user):
        return JsonResponse(
            {'result': False, 'error_msg': 'Smart restore yetkiniz yok.'},
            status=403,
        )

    from apps.backups.chunked_upload import (
        ChunkedUploadService,
        SessionNotFound, SessionForbidden, SessionTerminated,
    )
    from apps.backups.smart_restore import SmartRestoreService

    upload_id = request.POST.get('upload_id')
    dry_run_raw = (request.POST.get('dry_run') or 'false').strip().lower()
    dry_run = dry_run_raw in ('1', 'true', 'yes', 'on')
    backup_id = request.POST.get('backup_id') or None

    if not upload_id:
        return JsonResponse(
            {'result': False, 'error_msg': 'upload_id gerekli.'},
            status=400,
        )

    try:
        session = ChunkedUploadService.get_ready_session(
            upload_id, user=request.user,
        )
    except SessionNotFound as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=404)
    except SessionForbidden as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=403)
    except SessionTerminated as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=409)

    # Smart Restore — dosya path'i ile çağır (RAM'e yükleme yok)
    try:
        svc = SmartRestoreService(
            session.store_id,
            backup_id=backup_id or session.backup_id,
        )
        report = svc.restore_from_payload(
            session.temp_file_path,
            user=request.user,
            dry_run=dry_run,
        )
    except ValueError as ve:
        return JsonResponse(
            {'result': False, 'error_msg': f'Paket hatası: {ve}'},
            status=400,
        )
    except Exception as e:
        ChunkedUploadService.mark_failed(
            upload_id, error_message=str(e), delete_temp=False,
        )
        return JsonResponse(
            {'result': False, 'error_msg': f'Smart restore hatası: {e}'},
            status=500,
        )

    # Dry-run ise oturumu READY bırak (kullanıcı tekrar çağırabilsin).
    # Gerçek restore başarılı ise COMPLETED + temp dosya silinir.
    if not dry_run:
        if report.get('success'):
            ChunkedUploadService.mark_completed(upload_id, delete_temp=True)
        else:
            ChunkedUploadService.mark_failed(
                upload_id,
                error_message=str(report.get('error', '')),
                delete_temp=False,  # Audit için tut
            )

    return JsonResponse({
        'result': True,
        'report': report,
        'dry_run': dry_run,
        'upload_id': upload_id,
    })


@login_required
@require_POST
def chunked_upload_abort(request):
    """
    Yarım kalan oturumu iptal et — geçici dosyayı sil.

    POST params:
        upload_id — zorunlu
    """
    if not _user_can_restore_smart(request.user):
        return JsonResponse(
            {'result': False, 'error_msg': 'Smart restore yetkiniz yok.'},
            status=403,
        )

    from apps.backups.chunked_upload import (
        ChunkedUploadService,
        SessionNotFound, SessionForbidden,
    )

    upload_id = request.POST.get('upload_id')
    if not upload_id:
        return JsonResponse(
            {'result': False, 'error_msg': 'upload_id gerekli.'},
            status=400,
        )

    try:
        session = ChunkedUploadService.abort(upload_id, user=request.user)
    except SessionNotFound as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=404)
    except SessionForbidden as ce:
        return JsonResponse({'result': False, 'error_msg': str(ce)}, status=403)
    except Exception as e:
        return JsonResponse(
            {'result': False, 'error_msg': f'İptal hatası: {e}'},
            status=500,
        )

    return JsonResponse({
        'result': True,
        'msg': 'Oturum iptal edildi.',
        'upload_id': str(session.id),
        'status': session.status,
    })


# ==============================================================================
#  FAZ E — RestoreAuditLog Görüntüleme
# ==============================================================================
@login_required
@require_GET
def get_audit_log(request):
    """
    DataTables için RestoreAuditLog listesi.
    Query params:
        company_id — opsiyonel filtre (yedek üzerinden)
        backup_id  — opsiyonel filtre
        restore_type — 'FULL' | 'SMART' | (boş = hepsi)
    """
    if not _user_can_view_audit(request.user):
        return JsonResponse({'data': [], 'error': 'Yetki yok.'}, status=403)

    company_id = request.GET.get('company_id')
    backup_id = request.GET.get('backup_id')
    restore_type = (request.GET.get('restore_type') or '').strip().upper()

    qs = RestoreAuditLog.objects.select_related(
        'backup', 'backup__company', 'restored_by', 'content_type',
    ).order_by('-restored_at')

    if company_id:
        qs = qs.filter(backup__company_id=company_id)
    if backup_id:
        qs = qs.filter(backup_id=backup_id)
    if restore_type in ('FULL', 'SMART'):
        qs = qs.filter(restore_type=restore_type)

    data = []
    for ral in qs[:500]:  # son 500 kayıt
        data.append({
            'id': str(ral.id),
            'restored_at': ral.restored_at.strftime('%Y-%m-%d %H:%M:%S'),
            'restore_type': ral.restore_type,
            'backup_id': str(ral.backup_id),
            'backup_date': ral.backup.created_at.strftime('%Y-%m-%d %H:%M') if ral.backup else '-',
            'company': ral.backup.company.title if ral.backup and ral.backup.company else '-',
            'restored_by': str(ral.restored_by) if ral.restored_by else '-',
            'content_type': ral.content_type.model if ral.content_type else '-',
            'object_id': str(ral.object_id) if ral.object_id else '-',
            'idempotency_key': ral.idempotency_key or '-',
            'restore_notes': ral.restore_notes or '-',
            'similarity_warnings': ral.similarity_warnings or {},
        })

    return JsonResponse({'data': data})
