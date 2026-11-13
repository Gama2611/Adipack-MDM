#!/usr/bin/env python3
"""Respaldo de la base PostgreSQL de ADIPACK MDM (y de los APKs).

    python respaldar_bd.py                 # crea respaldos/adipack_mdm_AAAAMMDD_HHMM.dump
    python respaldar_bd.py --conservar 60  # cuántos respaldos guardar (por defecto 30)

Restaurar (en una base vacía, ver manual):
    pg_restore --clean --if-exists --no-owner --role=adipack_mdm_app -d adipack_mdm ARCHIVO.dump
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
os.chdir(HERE)


def find_pg_dump() -> str | None:
    if p := shutil.which("pg_dump"):
        return p
    # Instalación típica de PostgreSQL en Windows: toma la versión más nueva
    cands = sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin\pg_dump.exe"),
                   key=lambda s: int(Path(s).parts[-3]) if Path(s).parts[-3].isdigit() else 0)
    return cands[-1] if cands else None


def database_url() -> str:
    url = os.environ.get("MDM_DATABASE_URL", "")
    env = HERE / ".env"
    if not url and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("MDM_DATABASE_URL="):
                url = line.split("=", 1)[1].strip()
    return url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--destino", type=Path, default=HERE / "respaldos")
    ap.add_argument("--conservar", type=int, default=30)
    a = ap.parse_args()

    url = database_url()
    if not url.startswith("postgresql"):
        print("✗ MDM_DATABASE_URL no es PostgreSQL. Ejecuta primero CREAR_BASE_DATOS.bat.")
        return 2
    u = urlparse(url.replace("postgresql+psycopg", "postgresql"))
    pg_dump = find_pg_dump()
    if not pg_dump:
        print("✗ No encontré pg_dump. Viene con PostgreSQL (carpeta bin); agrégala al PATH.")
        return 2

    a.destino.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    dump = a.destino / f"{u.path.lstrip('/')}_{stamp}.dump"
    env = dict(os.environ, PGPASSWORD=unquote(u.password or ""))  # nunca en la línea de comandos
    cmd = [pg_dump, "-h", u.hostname or "localhost", "-p", str(u.port or 5432), "-U", unquote(u.username or ""),
           "-d", u.path.lstrip("/"), "-F", "c", "-Z", "6", "--no-owner", "-f", str(dump)]
    print(f"→ Respaldando base {u.path.lstrip('/')} …")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        dump.unlink(missing_ok=True)
        print(f"✗ pg_dump falló: {r.stderr.strip()}")
        return 1
    print(f"  ✓ {dump.name} ({dump.stat().st_size / 1024:.0f} KB)")

    apk_dir = HERE / "data" / "apks"
    if apk_dir.exists() and any(apk_dir.iterdir()):
        z = a.destino / f"apks_{stamp}.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
            for f in apk_dir.glob("*.apk"):
                zf.write(f, f.name)
        print(f"  ✓ {z.name} (APKs subidos)")

    for pattern in ("*.dump", "apks_*.zip"):
        old = sorted(a.destino.glob(pattern))[:-a.conservar] if a.conservar > 0 else []
        for f in old:
            f.unlink()
    print(f"✓ Respaldo listo en {a.destino}. Copia esa carpeta fuera de esta PC periódicamente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
