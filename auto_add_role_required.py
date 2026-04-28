import os
import re
import sys
import argparse
import django

# ---- Django init ----
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'jewelery_project.settings')
django.setup()

from apps.roles.models import Permission

IMPORT_LINE = "from apps.roles.decorators import role_required\n"

def ensure_import_and_decorators(file_path: str, app_name: str):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    has_import = any("from apps.roles.decorators import role_required" in line for line in lines)
    updated_lines = []
    inserted_import = False

    for i, line in enumerate(lines):
        # ilk import satırlarının hemen üstüne role_required import’u enjekte et
        if not has_import and not inserted_import and re.match(r'^(from|import)\b', line):
            updated_lines.append(IMPORT_LINE)
            inserted_import = True

        # def foo(...):
        m = re.match(r'^\s*def\s+(\w+)\s*\(', line)
        if m:
            func_name = m.group(1)
            # özel/nested adları atla
            if not func_name.startswith('_'):
                code = f"{app_name.upper()}_{func_name.upper()}"
                prev = lines[i - 1] if i > 0 else ''
                if "@role_required" not in prev:
                    updated_lines.append(f"@role_required('{code}')\n")

                # permission yoksa oluştur
                try:
                    Permission.objects.get(code=code)
                except Permission.DoesNotExist:
                    Permission.objects.create(
                        code=code,
                        name=f"{func_name}",
                        group=app_name
                    )

        updated_lines.append(line)

    # hiç import yoksa dosyanın başına koy
    if not has_import and not inserted_import:
        updated_lines.insert(0, IMPORT_LINE)

    with open(file_path, 'w', encoding='utf-8') as f:
        f.writelines(updated_lines)

    print(f"✅ {os.path.basename(file_path)} güncellendi; permission kayıtları oluşturuldu/güncellendi.")

def main():
    parser = argparse.ArgumentParser(description="Tek bir uygulamanın views.py dosyasına role_required ekle")
    parser.add_argument("--app", help="Uygulama adı (apps/<ad>/views.py)", required=False)
    parser.add_argument("--file", help="Doğrudan dosya yolu (opsiyonel)", required=False)
    args = parser.parse_args()

    if not args.app and not args.file:
        parser.error("En az birini vermelisiniz: --app veya --file")

    base_apps = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'apps')

    if args.file:
        file_path = args.file
        app_name = args.app or os.path.basename(os.path.dirname(file_path))
    else:
        app_name = args.app
        file_path = os.path.join(base_apps, app_name, "views.py")

    if not os.path.exists(file_path):
        sys.exit(f"❌ views dosyası bulunamadı: {file_path}")

    ensure_import_and_decorators(file_path, app_name)
    print("🎉 Bitti.")

if __name__ == "__main__":
    main()