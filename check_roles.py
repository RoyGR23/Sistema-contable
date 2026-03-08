import os
import json
import re
from supabase import create_client

url = None
key = None

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()
    u_match = re.search(r'os\.environ\.get\("SUPABASE_URL"(?:,\s*"(.*?)")?\)', content)
    k_match = re.search(r'os\.environ\.get\("SUPABASE_KEY"(?:,\s*"(.*?)")?\)', content)
    # the code actually looks like: os.environ.get("SUPABASE_URL")
    # let's try to get them from .env if it exists, otherwise from os
    pass

from dotenv import load_dotenv
load_dotenv()
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")

if not url or not key:
    print("Cannot find Supabase credentials")
    exit()

sb = create_client(url, key)

print("--- roles table ---")
try:
    res = sb.table('roles').select('*').limit(2).execute()
    print("roles columns:", res.data[0].keys() if res.data else "empty")
    if res.data:
        print("sample:", json.dumps(res.data[0], indent=2))
except Exception as e:
    print('err roles:', e)
