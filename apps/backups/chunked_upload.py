"""
==============================================================================
 FAZ 60.2 — Chunked Upload Service (Cloudflare 413 By-pass)
==============================================================================

Tarih: 2026-05-09
Amaç:
    Smart Restore paketlerini (ZIP+media) Cloudflare'in 100 MB body limitini
    aşmadan parça parça yüklemek için sunucu tarafı oturum yönetimi.

Akış:
    1) init(user, store_id, filename, total_size, chunk_size)
         → ChunkedUploadSession oluştur, geçici dosya path'i hazırla.
    2) append_chunk(upload_id, chunk_index, raw_bytes)
         → Sıralı chunk'ı dosyaya append et, received_chunks++.
    3) finalize(upload_id)
         → Tüm chunk'lar geldi mi kontrol et, status=READY yap, path döndür.
         → Caller (smart_restore endpoint) dosyayı stream parse eder.
    4) abort(upload_id)
         → Geçici dosyayı sil, status=ABORTED yap.
    5) cleanup_expired()
         → expires_at < now olan oturumları sil (geçici dosya + DB kayıt).

Bellek Güvenliği:
    Her chunk diske yazılır, server-side RAM'de tutulmaz. Restore sırasında
    bile dosya stream olarak okunur (smart_restore.restore_from_file_path
    ile). Bu sayede multi-GB paketler bile düşük RAM'de çalışır.

Eşzamanlılık:
    select_for_update() ile aynı upload_id'ye paralel chunk yazımı
    serialize edilir. Frontend zaten sıralı gönderiyor (chunk_index ile)
    ama bu DB-side guard çoklu sekme/yenile durumunu korur.

Güvenlik:
    - User must own the session (init eden user dışında erişilemez).
    - Permission re-check finalize sırasında.
    - ZipSlip / path traversal: temp_file_path her zaman _CHUNK_ROOT altında.
    - filename sanitize edilir.

Konfigürasyon:
    settings.BACKUP_CHUNKED_TEMP_ROOT — varsayılan: BASE_DIR/_chunked_uploads/
    settings.BACKUP_CHUNK_MAX_SIZE_MB — varsayılan: 10 MB chunk üst limit
    settings.BACKUP_TOTAL_MAX_SIZE_MB — varsayılan: 5120 MB (5 GB) toplam üst limit
==============================================================================
"""

import os
import re
import uuid
from pathlib import Path

from django.conf import settings as dj_settings
from django.db import transaction
from django.utils import timezone

from apps.backups.models import ChunkedUploadSession


# --- Konfigürasyon (settings'te override edilebilir) -------------------------

def _chunk_temp_root():
    """Geçici chunk dosyalarının kök dizini."""
    root = getattr(dj_settings, 'BACKUP_CHUNKED_TEMP_ROOT', None)
    if not root:
        root = os.path.join(dj_settings.BASE_DIR, '_chunked_uploads')
    Path(root).mkdir(parents=True, exist_ok=True)
    return root


def _chunk_max_bytes():
    """Tek bir chunk için sunucu tarafı maksimum boyut."""
    mb = int(getattr(dj_settings, 'BACKUP_CHUNK_MAX_SIZE_MB', 10))
    return mb * 1024 * 1024


def _total_max_bytes():
    """Toplam paket için sunucu tarafı maksimum boyut."""
    mb = int(getattr(dj_settings, 'BACKUP_TOTAL_MAX_SIZE_MB', 5120))
    return mb * 1024 * 1024


# --- Yardımcılar -------------------------------------------------------------

_SAFE_FILENAME_RE = re.compile(r'[^A-Za-z0-9._\-]+')


def _sanitize_filename(name):
    """Path traversal'a karşı dosya adını güvene al."""
    name = (name or '').strip()
    if not name:
        return 'upload.bin'
    # Sadece basename (./../ vb. atılır)
    name = os.path.basename(name)
    # Tehlikeli karakterleri normalize et
    name = _SAFE_FILENAME_RE.sub('_', name)
    return name[:200] or 'upload.bin'


# --- Hata Sınıfları ----------------------------------------------------------

class ChunkedUploadError(Exception):
    """Genel chunked upload hatası — view tarafında 4xx'e çevrilir."""
    pass


class SessionNotFound(ChunkedUploadError):
    pass


class SessionForbidden(ChunkedUploadError):
    """Başka kullanıcının oturumuna erişim girişimi."""
    pass


class SessionTerminated(ChunkedUploadError):
    """Oturum READY/COMPLETED/ABORTED gibi terminal durumda — yeni chunk kabul edilmez."""
    pass


class InvalidChunk(ChunkedUploadError):
    """chunk_index sırasız veya chunk_size beklenenden farklı."""
    pass


# --- Ana Servis --------------------------------------------------------------

class ChunkedUploadService:
    """
    Stateless servis sınıfı — tüm state ChunkedUploadSession kaydında.
    Metotlar @staticmethod, çağrı yapan kendi atomic context'ini sağlar.
    """

    # --------------------------------------------------------------------------
    # 1) init
    # --------------------------------------------------------------------------
    @staticmethod
    def init(user, store_id, filename, total_size, chunk_size, backup_id=None):
        """
        Yeni bir parçalı yükleme oturumu başlat.

        Args:
            user: yüklemeyi başlatan kullanıcı (request.user)
            store_id: hedef mağaza UUID
            filename: client-side dosya adı
            total_size: toplam byte
            chunk_size: client-side chunk boyutu (byte)
            backup_id: opsiyonel audit referansı

        Returns:
            ChunkedUploadSession (kaydedilmiş)

        Raises:
            ChunkedUploadError: parametre hataları (boyut limit, sıfır chunk vs.)
        """
        try:
            total_size = int(total_size)
            chunk_size = int(chunk_size)
        except (TypeError, ValueError):
            raise ChunkedUploadError('total_size ve chunk_size sayı olmalı.')

        if total_size <= 0:
            raise ChunkedUploadError('total_size sıfırdan büyük olmalı.')
        if chunk_size <= 0:
            raise ChunkedUploadError('chunk_size sıfırdan büyük olmalı.')
        if chunk_size > _chunk_max_bytes():
            raise ChunkedUploadError(
                f'chunk_size çok büyük (max {_chunk_max_bytes() // (1024 * 1024)} MB).'
            )
        if total_size > _total_max_bytes():
            raise ChunkedUploadError(
                f'Toplam dosya boyutu çok büyük '
                f'(max {_total_max_bytes() // (1024 * 1024)} MB).'
            )

        # Toplam chunk sayısını hesapla
        total_chunks = (total_size + chunk_size - 1) // chunk_size
        if total_chunks <= 0:
            raise ChunkedUploadError('Geçersiz chunk hesaplaması.')

        safe_name = _sanitize_filename(filename)

        # Geçici dosya yolu (id henüz yok — UUID üretip path'i şimdiden belirle)
        session_id = uuid.uuid4()
        temp_path = os.path.join(_chunk_temp_root(), f'{session_id}.bin')

        # Boş dosyayı oluştur (open ile dokunup kapat)
        Path(temp_path).touch(exist_ok=False)

        session = ChunkedUploadSession.objects.create(
            id=session_id,
            user=user if (user and user.is_authenticated) else None,
            store_id=store_id,
            backup_id=backup_id,
            filename=safe_name,
            total_size=total_size,
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            received_chunks=0,
            received_bytes=0,
            temp_file_path=temp_path,
            status='PENDING',
            expires_at=ChunkedUploadSession.default_expiry(),
        )
        return session

    # --------------------------------------------------------------------------
    # 2) append_chunk
    # --------------------------------------------------------------------------
    @staticmethod
    def append_chunk(upload_id, chunk_index, raw_bytes, user=None):
        """
        Sıralı bir chunk'ı geçici dosyaya append et.

        Args:
            upload_id: ChunkedUploadSession.id
            chunk_index: 0-based parça sırası (received_chunks ile eşleşmeli)
            raw_bytes: parçanın ham içeriği
            user: erişim kontrolü için (None ise kontrol atlanır — superuser yolu)

        Returns:
            dict: {received_chunks, total_chunks, received_bytes, total_size,
                   status, progress_percent}

        Raises:
            SessionNotFound, SessionForbidden, SessionTerminated, InvalidChunk
        """
        try:
            chunk_index = int(chunk_index)
        except (TypeError, ValueError):
            raise InvalidChunk('chunk_index sayı olmalı.')

        if chunk_index < 0:
            raise InvalidChunk('chunk_index negatif olamaz.')

        if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
            raise InvalidChunk('raw_bytes geçersiz.')

        chunk_len = len(raw_bytes)
        if chunk_len == 0:
            raise InvalidChunk('Boş chunk kabul edilmiyor.')
        if chunk_len > _chunk_max_bytes():
            raise InvalidChunk(
                f'Chunk çok büyük (max {_chunk_max_bytes() // (1024 * 1024)} MB).'
            )

        with transaction.atomic():
            try:
                session = (
                    ChunkedUploadSession.objects
                    .select_for_update()
                    .get(id=upload_id)
                )
            except ChunkedUploadSession.DoesNotExist:
                raise SessionNotFound('Yükleme oturumu bulunamadı.')

            # Erişim kontrolü
            if user is not None and not getattr(user, 'is_superuser', False):
                if session.user_id and session.user_id != getattr(user, 'id', None):
                    raise SessionForbidden('Bu oturuma erişim yetkiniz yok.')

            # Süresi dolmuşsa
            if timezone.now() > session.expires_at:
                session.status = 'EXPIRED'
                session.save(update_fields=['status', 'updated_at'])
                ChunkedUploadService._safe_remove(session.temp_file_path)
                raise SessionTerminated('Yükleme oturumunun süresi dolmuş.')

            # Terminal kontrol
            if session.is_terminal():
                raise SessionTerminated(
                    f'Oturum zaten {session.status} durumunda.'
                )

            # chunk_index sıralı olmalı (idempotent: aynı index iki kez gelirse atla)
            expected = session.received_chunks
            if chunk_index < expected:
                # Idempotent: bu chunk zaten yazılmış (network retry vb.)
                return ChunkedUploadService._progress_dict(session)
            if chunk_index > expected:
                raise InvalidChunk(
                    f'Sıra dışı chunk: beklenen {expected}, gelen {chunk_index}.'
                )

            # Son chunk değilse boyut tam olmalı
            is_last = (chunk_index == session.total_chunks - 1)
            if not is_last and chunk_len != session.chunk_size:
                raise InvalidChunk(
                    f'Sondan önceki chunk\'lar tam {session.chunk_size} byte olmalı '
                    f'(gelen {chunk_len}).'
                )

            # Append yaz
            try:
                with open(session.temp_file_path, 'ab') as fh:
                    fh.write(bytes(raw_bytes))
            except OSError as e:
                session.status = 'FAILED'
                session.error_message = f'Disk yazma hatası: {e}'
                session.save(update_fields=['status', 'error_message', 'updated_at'])
                raise ChunkedUploadError(f'Disk yazma hatası: {e}')

            session.received_chunks = expected + 1
            session.received_bytes = session.received_bytes + chunk_len
            if session.status == 'PENDING':
                session.status = 'UPLOADING'

            # Tüm chunk'lar geldi mi?
            if session.received_chunks >= session.total_chunks:
                if session.received_bytes != session.total_size:
                    session.status = 'FAILED'
                    session.error_message = (
                        f'Toplam byte uyuşmazlığı: beklenen {session.total_size}, '
                        f'alınan {session.received_bytes}.'
                    )
                else:
                    session.status = 'READY'

            session.save(update_fields=[
                'received_chunks', 'received_bytes', 'status',
                'error_message', 'updated_at',
            ])

            return ChunkedUploadService._progress_dict(session)

    # --------------------------------------------------------------------------
    # 3) finalize — sadece hazırlık, asıl restore çağıranın işidir
    # --------------------------------------------------------------------------
    @staticmethod
    def get_ready_session(upload_id, user=None):
        """
        Restore için hazır oturumu doğrula ve döndür. Status değiştirmez —
        caller (view) restore başarılı olunca mark_completed çağırır.

        Returns:
            ChunkedUploadSession (status='READY')

        Raises:
            SessionNotFound, SessionForbidden, SessionTerminated
        """
        try:
            session = ChunkedUploadSession.objects.get(id=upload_id)
        except ChunkedUploadSession.DoesNotExist:
            raise SessionNotFound('Yükleme oturumu bulunamadı.')

        if user is not None and not getattr(user, 'is_superuser', False):
            if session.user_id and session.user_id != getattr(user, 'id', None):
                raise SessionForbidden('Bu oturuma erişim yetkiniz yok.')

        if session.status != 'READY':
            raise SessionTerminated(
                f'Oturum durumu uygun değil: {session.status}. '
                f'(Beklenen: READY)'
            )

        # Süresi dolmuşsa reddet
        if timezone.now() > session.expires_at:
            session.status = 'EXPIRED'
            session.save(update_fields=['status', 'updated_at'])
            ChunkedUploadService._safe_remove(session.temp_file_path)
            raise SessionTerminated('Yükleme oturumunun süresi dolmuş.')

        # Dosya gerçekten var mı?
        if not os.path.exists(session.temp_file_path):
            session.status = 'FAILED'
            session.error_message = 'Geçici dosya bulunamadı (silinmiş olabilir).'
            session.save(update_fields=['status', 'error_message', 'updated_at'])
            raise SessionTerminated('Geçici dosya bulunamadı.')

        return session

    # --------------------------------------------------------------------------
    # 4) mark_completed / mark_failed — restore sonrası
    # --------------------------------------------------------------------------
    @staticmethod
    def mark_completed(upload_id, delete_temp=True):
        """Restore başarılı sonrası kayıt + temp dosya silinir."""
        try:
            session = ChunkedUploadSession.objects.get(id=upload_id)
        except ChunkedUploadSession.DoesNotExist:
            return None
        session.status = 'COMPLETED'
        session.save(update_fields=['status', 'updated_at'])
        if delete_temp:
            ChunkedUploadService._safe_remove(session.temp_file_path)
        return session

    @staticmethod
    def mark_failed(upload_id, error_message='', delete_temp=True):
        try:
            session = ChunkedUploadSession.objects.get(id=upload_id)
        except ChunkedUploadSession.DoesNotExist:
            return None
        session.status = 'FAILED'
        session.error_message = (error_message or '')[:5000]
        session.save(update_fields=['status', 'error_message', 'updated_at'])
        if delete_temp:
            ChunkedUploadService._safe_remove(session.temp_file_path)
        return session

    # --------------------------------------------------------------------------
    # 5) abort — kullanıcı iptali
    # --------------------------------------------------------------------------
    @staticmethod
    def abort(upload_id, user=None):
        """Kullanıcı iptali — geçici dosya silinir, status=ABORTED."""
        try:
            session = ChunkedUploadSession.objects.get(id=upload_id)
        except ChunkedUploadSession.DoesNotExist:
            raise SessionNotFound('Yükleme oturumu bulunamadı.')

        if user is not None and not getattr(user, 'is_superuser', False):
            if session.user_id and session.user_id != getattr(user, 'id', None):
                raise SessionForbidden('Bu oturuma erişim yetkiniz yok.')

        if session.status == 'COMPLETED':
            return session  # Tamamlandı zaten — no-op

        session.status = 'ABORTED'
        session.save(update_fields=['status', 'updated_at'])
        ChunkedUploadService._safe_remove(session.temp_file_path)
        return session

    # --------------------------------------------------------------------------
    # 6) cleanup_expired — Celery beat / management command'tan çağrılır
    # --------------------------------------------------------------------------
    @staticmethod
    def cleanup_expired():
        """
        Süresi dolmuş veya terminal durumdaki eski kayıtları temizle.
        Returns: dict {expired: N, removed_files: N, kept: N}
        """
        now = timezone.now()
        expired_qs = ChunkedUploadSession.objects.filter(
            expires_at__lt=now,
        ).exclude(status='COMPLETED')  # Audit için COMPLETED'ları sakla

        expired_count = 0
        removed_files = 0
        for session in expired_qs:
            expired_count += 1
            if ChunkedUploadService._safe_remove(session.temp_file_path):
                removed_files += 1
            session.status = 'EXPIRED'
            session.save(update_fields=['status', 'updated_at'])
        return {
            'expired': expired_count,
            'removed_files': removed_files,
            'kept_completed': ChunkedUploadSession.objects.filter(
                status='COMPLETED',
            ).count(),
        }

    # --------------------------------------------------------------------------
    # Yardımcılar
    # --------------------------------------------------------------------------
    @staticmethod
    def _safe_remove(path):
        """Bir dosyayı güvenle sil — hata varsa False döner."""
        if not path:
            return False
        try:
            if os.path.exists(path):
                os.remove(path)
            return True
        except OSError:
            return False

    @staticmethod
    def _progress_dict(session):
        return {
            'upload_id': str(session.id),
            'received_chunks': session.received_chunks,
            'total_chunks': session.total_chunks,
            'received_bytes': session.received_bytes,
            'total_size': session.total_size,
            'status': session.status,
            'progress_percent': session.progress_percent,
        }
