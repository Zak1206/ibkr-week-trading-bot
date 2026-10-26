# -*- coding: utf-8 -*-
"""
Construit la table des achats en bourse de dirigeants (Form 4, code P) depuis les ZIP
trimestriels telecharges par research_insider_fetch.py. Regles: section « Methode »
de docs/reports/recherche-dirigeants-2026-10-26.md (figees avant les donnees).

Une ligne par depot (ACCESSION_NUMBER) avec au moins une transaction P retenue.
Sortie: scripts/research_insider_filings.pkl (ignore par git, comme tous les .pkl).

Donnees non fiables: lancer avec python -I, depuis la racine du depot.
Usage: .venv/Scripts/python.exe -I scripts/research_insider_events.py --data-dir <dossier des ZIP>
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import zipfile

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "research_insider_filings.pkl")

EXCLUDED_TITLE = re.compile(r"PREFERRED|NOTE|WARRANT|UNIT|DEBENTURE", re.I)
CEO_CFO = re.compile(r"\bCEO\b|CHIEF EXECUTIVE|PRINCIPAL EXECUTIVE|\bCFO\b|CHIEF FINANCIAL|PRINCIPAL FINANCIAL", re.I)
# Ticker US simple (BRK.B accepte); les suffixes OTC du type « DYSL:OB » sont exclus.
VALID_SYMBOL = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")


def _read(z: zipfile.ZipFile, name: str, cols: list) -> pd.DataFrame:
    with z.open(name) as f:
        return pd.read_csv(f, sep="\t", dtype=str, usecols=cols, quoting=3, on_bad_lines="skip",
                           encoding="utf-8", encoding_errors="replace", keep_default_na=False)


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.str.strip(), format="%d-%b-%Y", errors="coerce")


def one_quarter(path: str) -> pd.DataFrame:
    z = zipfile.ZipFile(path)
    tr = _read(z, "NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "SECURITY_TITLE", "TRANS_DATE", "TRANS_CODE",
                                          "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"])
    tr = tr[(tr.TRANS_CODE.str.strip() == "P") & (tr.TRANS_ACQUIRED_DISP_CD.str.strip() == "A")]
    tr = tr[~tr.SECURITY_TITLE.str.contains(EXCLUDED_TITLE)]
    tr = tr.assign(shares=pd.to_numeric(tr.TRANS_SHARES, errors="coerce"),
                   px=pd.to_numeric(tr.TRANS_PRICEPERSHARE, errors="coerce"),
                   tdate=_date(tr.TRANS_DATE))
    tr = tr[(tr.shares > 0) & (tr.px > 0) & tr.tdate.notna()]
    tr = tr.assign(value=tr.shares * tr.px)
    agg = tr.groupby("ACCESSION_NUMBER").agg(value=("value", "sum"), shares=("shares", "sum"),
                                             tdate_min=("tdate", "min"), tdate_max=("tdate", "max"))
    agg["price"] = agg.value / agg.shares

    sub = _read(z, "SUBMISSION.tsv", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK",
                                      "ISSUERTRADINGSYMBOL"])
    sub = sub[sub.DOCUMENT_TYPE.str.strip() == "4"].set_index("ACCESSION_NUMBER")
    df = agg.join(sub, how="inner")
    df["filing_date"] = _date(df.FILING_DATE)
    df["symbol"] = df.ISSUERTRADINGSYMBOL.str.strip().str.upper()

    own = _read(z, "REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP",
                                          "RPTOWNER_TITLE"])
    own = own[own.ACCESSION_NUMBER.isin(df.index)]
    rel = own.RPTOWNER_RELATIONSHIP.fillna("")
    own = own.assign(is_od=rel.str.contains("Director|Officer", case=False),
                     is_ceo_cfo=rel.str.contains("Officer", case=False)
                     & own.RPTOWNER_TITLE.fillna("").str.contains(CEO_CFO))
    g = own.groupby("ACCESSION_NUMBER")
    df["is_od"] = g.is_od.any().reindex(df.index).fillna(False).astype(bool)
    df["is_ceo_cfo"] = g.is_ceo_cfo.any().reindex(df.index).fillna(False).astype(bool)
    df["od_ciks"] = (own[own.is_od].groupby("ACCESSION_NUMBER").RPTOWNERCIK
                     .apply(lambda s: tuple(sorted(set(s.str.strip())))).reindex(df.index))
    df["only_10pct"] = (g.RPTOWNER_RELATIONSHIP.apply(lambda s: all(v.strip() == "TenPercentOwner" for v in s))
                        .reindex(df.index).fillna(False).astype(bool))
    keep = ["filing_date", "symbol", "ISSUERCIK", "value", "price", "shares", "tdate_min", "tdate_max",
            "is_od", "is_ceo_cfo", "od_ciks", "only_10pct"]
    return df[keep].rename(columns={"ISSUERCIK": "issuer_cik"}).reset_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    args = ap.parse_args()
    paths = sorted(glob.glob(os.path.join(args.data_dir, "*", "*_form345.zip")))
    parts = []
    for p in paths:
        d = one_quarter(p)
        parts.append(d)
        print(f"{os.path.basename(p)}: {len(d)} depots avec achat P "
              f"({int(d.is_od.sum())} dirigeant/administrateur)", flush=True)
    out = pd.concat(parts, ignore_index=True).drop_duplicates("ACCESSION_NUMBER")
    out["valid_symbol"] = out.symbol.str.match(VALID_SYMBOL)
    out.to_pickle(OUT)
    print(f"\n{len(out)} depots -> {OUT}")
    od = out[out.is_od & out.valid_symbol]
    print(f"dirigeant/administrateur, ticker valide: {len(od)} depots, {od.symbol.nunique()} tickers")
    print(f"  >= 25 k$: {int((od.value >= 25_000).sum())} depots, "
          f"{od[od.value >= 25_000].symbol.nunique()} tickers")
    print(f"  seulement actionnaire > 10 % (mis a part): {int((out.only_10pct).sum())} depots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
