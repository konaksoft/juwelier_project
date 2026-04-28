#!/usr/bin/env python3

import subprocess
import datetime
import os
import argparse
import logging


def postgresql_yedekle(kullanici_adi, parola, veritabani_adi, host, port):
    """
    PostgreSQL veritabanını yedekler ve ~/backups dizinine kaydeder.
    """
    ana_dizin = os.path.expanduser("~")
    yedek_dizini = os.path.join(ana_dizin, "backups")
    os.makedirs(yedek_dizini, exist_ok=True)

    zaman_damgasi = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    yedek_dosya_adi = f"{veritabani_adi}_yedek_{zaman_damgasi}.sql"
    yedek_dosya_yolu = os.path.join(yedek_dizini, yedek_dosya_adi)

    pg_dump_komutu = [
        "pg_dump",
        "-U", kullanici_adi,
        "-d", veritabani_adi,
        "-h", host,
        "-p", str(port),
        "-F", "c",
        "-b",
        "-v",
        "-f", yedek_dosya_yolu
    ]

    env = os.environ.copy()
    env["PGPASSWORD"] = parola  # Parola için ortam değişkeni kullanımı

    try:
        logging.info(f"Yedekleme başlatılıyor: {veritabani_adi} -> {yedek_dosya_yolu}")
        subprocess.run(pg_dump_komutu, check=True, env=env)
        logging.info(f"✅ Yedekleme başarılı! Kaydedilen dosya: {yedek_dosya_yolu}")
        return yedek_dosya_yolu
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Yedekleme hatası! Komut: {' '.join(pg_dump_komutu)}")
        return None


def main():
    parser = argparse.ArgumentParser(description="PostgreSQL Yedekleme Aracı")
    parser.add_argument("-U", "--kullanici", required=True, help="PostgreSQL kullanıcı adı (örn: postgres)")
    parser.add_argument("-P", "--parola", required=True, help="PostgreSQL kullanıcı parolası")
    parser.add_argument("-d", "--veritabani", required=True, help="Yedeklenecek veritabanı adı")
    parser.add_argument("-H", "--host", default="localhost", help="PostgreSQL sunucu adresi (varsayılan: localhost)")
    parser.add_argument("-p", "--port", type=int, default=5432, help="PostgreSQL bağlantı noktası (varsayılan: 5432)")
    parser.add_argument("-L", "--log", default="yedekleme.log", help="Log dosyası (varsayılan: yedekleme.log)")

    args = parser.parse_args()

    logging.basicConfig(
        filename=args.log,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    yedek_yolu = postgresql_yedekle(args.kullanici, args.parola, args.veritabani, args.host, args.port)
    if yedek_yolu:
        print(f"✅ Yedek başarıyla oluşturuldu: {yedek_yolu}")
    else:
        print("❌ Yedekleme başarısız!")


if __name__ == "__main__":
    main()