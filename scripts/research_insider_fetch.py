# -*- coding: utf-8 -*-
"""
Telecharge les « Insider Transactions Data Sets » trimestriels de la SEC (Form 3/4/5).
Etude: docs/reports/recherche-dirigeants-2026-10-26.md

La SEC exige un User-Agent avec un contact (politique Fair Access). Il n'est ecrit
nulle part: il vient de la variable d'environnement SEC_USER_AGENT, passee en ligne
de commande. Le script ne parle qu'a www.sec.gov, une requete a la fois (< 10/s).

Chaque trimestre va dans son propre dossier <data-dir>/<AAAAqN>/ (donnees non fiables:
a lire avec python -I). Garder <data-dir> hors du depot (data/ n'est pas dans .gitignore).

Usage:
  SEC_USER_AGENT="nom contact" .venv/Scripts/python.exe -I scripts/research_insider_fetch.py \
      --data-dir <dossier hors depot> [--start 2012q1] [--end 2026q3]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request

LISTING = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
MIN_GAP_SEC = 0.2  # 5 requetes/s au plus


def zip_links(ua: str) -> dict:
    """Trimestre -> URL du ZIP, lus sur la page de la SEC (deux chemins selon l'annee)."""
    req = urllib.request.Request(LISTING, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=60) as resp:
        html = resp.read().decode("utf-8", "replace")
    out = {}
    for path in re.findall(r'href="(/files/[^"]*?(\d{4}q[1-4])_form345\.zip)"', html):
        out[path[1]] = "https://www.sec.gov" + path[0]
    return out


def quarters(start: str, end: str):
    y, q = int(start[:4]), int(start[-1])
    y1, q1 = int(end[:4]), int(end[-1])
    while (y, q) <= (y1, q1):
        yield f"{y}q{q}"
        q += 1
        if q == 5:
            y, q = y + 1, 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--start", default="2012q1")
    ap.add_argument("--end", default="2026q3")
    args = ap.parse_args()
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        print("SEC_USER_AGENT manquant: la SEC refuse les requetes sans contact declare. Arret.")
        return 2
    try:
        links = zip_links(ua)
    except urllib.error.HTTPError as exc:
        print(f"page SEC: HTTP {exc.code}. Arret (pas de contournement).")
        return 1
    last = time.monotonic()
    for qtr in quarters(args.start, args.end):
        if qtr not in links:
            print(f"{qtr}: absent de la page SEC")
            continue
        dest_dir = os.path.join(args.data_dir, qtr)
        dest = os.path.join(dest_dir, f"{qtr}_form345.zip")
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            print(f"{qtr}: deja la ({os.path.getsize(dest) / 1e6:.1f} Mo)")
            continue
        os.makedirs(dest_dir, exist_ok=True)
        wait = MIN_GAP_SEC - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        req = urllib.request.Request(links[qtr], headers={"User-Agent": ua})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest + ".part", "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        except urllib.error.HTTPError as exc:
            print(f"{qtr}: HTTP {exc.code}. Arret (pas de contournement).")
            return 1
        os.replace(dest + ".part", dest)
        print(f"{qtr}: {os.path.getsize(dest) / 1e6:.1f} Mo", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
