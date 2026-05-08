#!/usr/bin/env python3
import os
import zipfile

ROOT    = os.path.dirname(os.path.abspath(__file__))
OUT     = os.path.join(ROOT, 'curie.zip')
EXCLUDE = {'.pyc', '.zip'}
EXCLUDE_DIRS = {'__pycache__', '.git'}

if os.path.exists(OUT):
    os.remove(OUT)
    print(f'Removed {OUT}')

with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as zf:
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fname in filenames:
            if any(fname.endswith(ext) for ext in EXCLUDE):
                continue
            full = os.path.join(dirpath, fname)
            arcname = os.path.relpath(full, ROOT)
            zf.write(full, arcname)
            print(f'  + {arcname}')

print(f'\nDone → {OUT}')
