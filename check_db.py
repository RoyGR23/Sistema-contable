import os
import json
import re
from supabase import create_client

url = None
key = None

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()
    u_match = re.search(r'os\.getenv\("SUPABASE_URL",\s*"(.*?)"\)', content)
    k_match = re.search(r'os\.getenv\("SUPABASE_KEY",\s*"(.*?)"\)', content)
    if u_match: url = u_match.group(1)
    if k_match: key = k_match.group(1)

if not url or not key:
    print("Cannot find Supabase credentials")
    exit()

sb = create_client(url, key)

print("--- permissions table ---")
try:
    res = sb.table('permisos').select('*').limit(5).execute()
    print("permisos:", json.dumps(res.data, indent=2))
except Exception as e:
    print('err permisos:', e)

print("\n--- roles_permissions table (permisos_rol) ---")
try:
    res = sb.table('permisos_rol').select('*').limit(5).execute()
    print("permisos_rol:", json.dumps(res.data, indent=2))
except Exception as e:
    print('err permisos_rol:', e)

print("\n--- cuentas_por_cobrar table ---")
try:
    res = sb.table('cuentas_por_cobrar').select('*').limit(1).execute()
    print("cuentas_por_cobrar exists.")
except Exception as e:
    print('err cuentas_por_cobrar:', e)
