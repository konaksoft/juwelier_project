import os
import sys
import glob
import shlex
import subprocess
from pathlib import Path
from datetime import datetime
from shutil import which


# -----------------------------
# Yardımcılar
# -----------------------------

def _postgres_app_bin() -> str | None:
    paths = sorted(glob.glob("/Applications/Postgres.app/Contents/Versions/*/bin"), reverse=True)
    for d in paths:
        if (Path(d) / "pg_dump").exists():
            return d
    return None


def _brew_libpq_bin() -> str | None:
    # Homebrew yoksa None döner; varsa libpq'nun bin yolunu verir
    brew = which("brew") or "/opt/homebrew/bin/brew" if Path("/opt/homebrew/bin/brew").exists() else None
    try:
        if brew:
            out = subprocess.run([brew, "--prefix", "libpq"], capture_output=True, text=True, check=True)
            pref = out.stdout.strip()
            cand = Path(pref) / "bin"
            if cand.exists():
                return str(cand)
    except Exception:
        pass
    return None


def _build_env() -> dict:
    env = os.environ.copy()
    extra = []

    # Kullanıcı PG_BIN verirse onu öne koy
    if os.getenv("PG_BIN"):
        extra.append(os.getenv("PG_BIN"))

    if sys.platform == "darwin":
        for p in (_brew_libpq_bin(), _postgres_app_bin(), "/opt/homebrew/opt/libpq/bin", "/usr/local/opt/libpq/bin"):
            if p:
                extra.append(p)
        # EDB installer yolları
        extra += ["/Library/PostgreSQL/17/bin", "/Library/PostgreSQL/16/bin"]
    else:
        # Linux dağıtımlarında sık yollar
        extra += [
            "/usr/bin",
            "/usr/local/bin",
            "/usr/lib/postgresql/17/bin",
            "/usr/lib/postgresql/16/bin",
            "/usr/pgsql-17/bin",
            "/usr/pgsql-16/bin",
        ]

    # PATH'i genişlet
    path_parts = [p for p in extra if p and Path(p).exists()]
    path_parts.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def _resolve_binary(name: str, env: dict) -> str | None:
    # Manuel override
    override = os.getenv("PG_DUMP_PATH") if name == "pg_dump" else os.getenv("PG_DUMPALL_PATH")
    if override and Path(override).exists():
        return override
    # PATH içinde ara
    p = which(name, path=env.get("PATH"))
    if p:
        return p
    # Windows özel durumları
    if os.name == "nt":
        exe = name + ".exe"
        p = which(exe, path=env.get("PATH"))
        if p:
            return p
        pf = os.getenv("ProgramFiles", r"C:\\Program Files")
        pf86 = os.getenv("ProgramFiles(x86)", r"C:\\Program Files (x86)")
        for v in ["17", "16", "15", "14", "13", "12"]:
            for base in (pf, pf86):
                cand = Path(base) / f"PostgreSQL\\{v}\\bin\\{exe}"
                if cand.exists():
                    return str(cand)
    return None


def _rotate_backups(dirpath: Path, pattern: str, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(dirpath.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        try:
            p.unlink()
        except Exception:
            pass


def _ssh_stream(cmd: str, outfile: Path, target: str, port: str = "22") -> None:
    ssh_opts: list[str] = []
    # İlk bağlantıda anahtarı otomatik ekle
    strict = os.getenv("SSH_STRICT", "accept-new")
    if strict:
        ssh_opts += ["-o", f"StrictHostKeyChecking={strict}"]
    # Kimlik dosyası
    ident = os.getenv("SSH_IDENTITY_FILE")
    if ident and Path(ident).expanduser().exists():
        ssh_opts += ["-i", str(Path(ident).expanduser())]
    # Parolasız/etkileşimsiz kip (cron/celery için)
    if os.getenv("SSH_BATCH", "0").lower() in ("1", "true", "yes", "on"):
        ssh_opts += ["-o", "BatchMode=yes"]

    with open(outfile, "wb") as f:
        p = subprocess.Popen(["ssh", *ssh_opts, "-p", port, target, cmd], stdout=f, stderr=subprocess.PIPE, text=False)
        _, err = p.communicate()
        if p.returncode != 0:
            raise RuntimeError(f"Uzak sunucuda komut başarısız: {err.decode('utf-8', 'ignore')}")


# -----------------------------
# Ana işlev
# -----------------------------

def backup_once() -> dict:
    base_dir = Path(__file__).resolve().parent
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "jewelery_project.settings")

    import django
    django.setup()
    from django.conf import settings

    backups_dir = base_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    db = settings.DATABASES.get("default", {})
    dbname = db.get("NAME", "")
    user = db.get("USER", "")
    password = db.get("PASSWORD", "")
    host = db.get("HOST", "")
    port = str(db.get("PORT", "5432"))

    ts = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
    dump_path = backups_dir / f"{dbname}_{ts}.backup"
    globals_path = backups_dir / f"globals_{ts}.sql"

    env = _build_env()
    if password:
        env["PGPASSWORD"] = password

    # Önce lokal araçları dene
    pg_dump = _resolve_binary("pg_dump", env)
    pg_dumpall = _resolve_binary("pg_dumpall", env)

    if pg_dump and pg_dumpall:
        dump_cmd = [pg_dump, "-F", "c", "-b", "-p", port, "-U", user, "-f", str(dump_path)]
        if host:
            dump_cmd.extend(["-h", host])
        dump_cmd.append(dbname)

        globals_cmd = [pg_dumpall, "-g", "-p", port, "-U", user, "-f", str(globals_path)]
        if host:
            globals_cmd.extend(["-h", host])

        subprocess.run(dump_cmd, check=True, env=env)
        subprocess.run(globals_cmd, check=True, env=env)
    else:
        # Lokal yoksa SSH ile uzak sunucuda pg_dump çalıştır ve çıktıyı indir
        ssh_host = os.getenv("SSH_HOST", host)
        ssh_user = os.getenv("SSH_USER") or "root"
        ssh_port = os.getenv("SSH_PORT", "22")
        if not ssh_host:
            raise FileNotFoundError(
                "pg_dump bulunamadı ve SSH hedefi yok. Mac'te Postgres.app kurabilir (PG_BIN=.../bin), ya da SSH_HOST/SSH_USER vererek uzak sunucudan akıtabilirsiniz."
            )
        target = f"{ssh_user}@{ssh_host}"
        remote_env = f"PGPASSWORD={shlex.quote(password)} " if password else ""
        remote_dump = f"{remote_env}pg_dump -F c -b -p {port} -U {shlex.quote(user)} " + (f"-h {shlex.quote(host)} " if host else "") + f"{shlex.quote(dbname)}"
        remote_globals = f"{remote_env}pg_dumpall -g -p {port} -U {shlex.quote(user)} " + (f"-h {shlex.quote(host)} " if host else "")
        _ssh_stream(remote_dump, dump_path, target, ssh_port)
        _ssh_stream(remote_globals, globals_path, target, ssh_port)

    # Rotasyon
    keep = int(os.getenv("BACKUP_RETENTION", "10"))
    _rotate_backups(backups_dir, f"{dbname}_*.backup", keep)
    _rotate_backups(backups_dir, "globals_*.sql", keep)

    print(str(dump_path))
    print(str(globals_path))
    return {"db_backup": str(dump_path), "globals_backup": str(globals_path)}


if __name__ == "__main__":
    backup_once()
