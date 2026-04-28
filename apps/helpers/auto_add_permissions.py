import os
import re

VIEWS_PATH = 'apps/accounts/views.py'
URLS_PATH = 'apps/accounts/urls.py'

permissions = {
    'index_view': 'J5J1000',
    'add_view': 'J6J1001',
    'employee_detail_view': 'ABC1010',
    'profile_view': 'J6J1002',
    'delete': 'J6J1004',
    'change_status': 'J6J1004',
    'get_all': 'ABC1016',
    'get_all_accounts': 'ABC1016',
    'change_two_factor': 'ABC1016',
    'staff_management_view': 'ABC1016',
}

def decorate_views():
    with open(VIEWS_PATH, 'r', encoding='utf-8') as file:
        content = file.read()

    updated_lines = []
    lines = content.splitlines()
    for i, line in enumerate(lines):
        updated_lines.append(line)
        match = re.match(r'^def (\w+)\(', line.strip())
        if match:
            func_name = match.group(1)
            if func_name in permissions:
                prev_line = lines[i - 1] if i > 0 else ''
                if f"@role_required('{permissions[func_name]}')" not in prev_line:
                    updated_lines.insert(len(updated_lines)-1, f"@role_required('{permissions[func_name]}')")

    with open(VIEWS_PATH, 'w', encoding='utf-8') as file:
        file.write('\n'.join(updated_lines))

    print("✅ views.py dosyası güncellendi.")

def update_urls():
    with open(URLS_PATH, 'r', encoding='utf-8') as file:
        content = file.read()

    if "def secure" not in content:
        secure_func = """
def secure(view_func, code):
    from apps.roles.decorators import role_required
    from django.contrib.auth.decorators import login_required
    return login_required(role_required(code)(view_func))
"""
        content = secure_func + "\n" + content

    def replace_match(match):
        path_line = match.group(0)
        view_func = match.group(2)
        if view_func in permissions:
            return f"{match.group(1)}secure({view_func}, '{permissions[view_func]}'),{match.group(3)}"
        return path_line

    content = re.sub(
        r"(path\(['\"]\S+['\"],\s*)(\w+)(\s*,\s*name=)",
        replace_match,
        content
    )

    with open(URLS_PATH, 'w', encoding='utf-8') as file:
        file.write(content)

    print("✅ urls.py dosyası secure() ile güncellendi.")

if __name__ == '__main__':
    decorate_views()
    update_urls()