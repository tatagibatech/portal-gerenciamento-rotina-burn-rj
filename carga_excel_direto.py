"""Importa ASNs do Excel direto para o portal — sem chamar WMS. Envia em lotes de 500."""
import re, time
import requests, openpyxl

PORTAL = "https://painel-burn-rj.onrender.com"
EXCEL  = "2026 08 10 - Carga inicial ASN OP.xlsx"
BATCH  = 500

DEPOSITOS = {"308","309","310","311","312","313","314","315","316","317","318","321","323","339"}

STATUS_MAP = {
    "0": "pendente", "2": "pendente", "3": "no_recebimento",
    "4": "pendente", "5": "no_recebimento", "9": "recebido",
    "11": "fechado", "15": "fechado", "20": "cancelado", "21": "recebido",
}
STATUS_LABEL = {
    "pendente": "Pendente", "no_recebimento": "No Recebimento",
    "recebido": "Recebido", "fechado": "Fechado",
}
PALETES_VAZIO = {
    "total_previsto": 0, "total_recebido": 0, "qpp": 0, "qpp_source": "indisponivel",
    "paletes_previstos": 0, "paletes_inteiros": 0, "paletes_fracao": 0, "paletes_total": 0,
    "paletes_recebidos": 0, "paletes_rec_inteiros": 0, "paletes_rec_fracao": 0,
    "paletes_rec_total": 0, "diferenca_paletes": 0,
}


def _norm_date(val) -> str:
    if val is None:
        return ""
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if not s or s == "None":
        return ""
    # ISO: "2026-08-10..."
    if len(s) >= 10 and s[4:5] == "-":
        return s[:10]
    # Qualquer formato com "/" (MM/DD/YYYY, M/D/YY, etc.)
    if "/" in s:
        date_part = s.split()[0]  # ignora a parte de hora
        parts = date_part.split("/")
        if len(parts) == 3:
            yr = parts[2]
            if len(yr) <= 2:
                yr = "20" + yr.zfill(2)
            return f"{yr}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    return s[:10]


def _deposito(supplier_code) -> str | None:
    sc = str(supplier_code or "").strip()
    for dep in DEPOSITOS:
        if dep in sc:
            return dep
    return None


def _status_derivado(sr: str) -> str:
    if sr in ("11", "15"):  return "fechado"
    if sr == "20":          return "cancelado"
    if sr in ("9", "21"):   return "recebido"
    if sr in ("3", "5"):    return "no_recebimento"
    return STATUS_MAP.get(sr, "pendente")


import shutil, tempfile

print("Lendo Excel...")
_tmp = tempfile.mktemp(suffix=".xlsx")
shutil.copy2(EXCEL, _tmp)
wb = openpyxl.load_workbook(_tmp, read_only=True, data_only=True)
ws = wb["Data"] if "Data" in wb.sheetnames else wb.active
pat = re.compile(r"^\d+\.\d+$")
receipts = []

for row in ws.iter_rows(min_row=3, values_only=True):
    if not row or len(row) < 92:
        continue
    rk = str(row[2] or "").strip()
    if not pat.match(rk):
        continue
    if str(row[13] if row[13] is not None else "").strip() != "8":
        continue

    sr  = str(row[12] if row[12] is not None else "0").strip()
    dep = _deposito(row[55])
    sn  = str(row[56] or "").strip() or (f"Depósito {dep}" if dep else "")
    std = _status_derivado(sr)
    add = _norm_date(row[91])
    edt = _norm_date(row[93]) or add

    receipts.append({
        "receiptkey":       rk,
        "externkey":        str(row[6]  or "").strip(),
        "supplier_code":    str(row[55] or "").strip(),
        "supplier_name":    sn,
        "editwho":          str(row[94] or "").strip(),
        "receipttype":      "8",
        "status_raw":       sr,
        "status":           std,
        "status_wms":       STATUS_MAP.get(sr, "pendente"),
        "status_label":     STATUS_LABEL.get(std, std),
        "deposito":         dep,
        "data_criacao":     add,
        "data_recebimento": _norm_date(row[11]),
        "data_fechamento":  _norm_date(row[14]),
        "data_atualizacao": edt,
        "n_linhas":         0,
        "paletes":          dict(PALETES_VAZIO),
        "linhas":           [],
    })

wb.close()
print(f"{len(receipts)} ASNs type=8 prontas.")

print("Aguardando portal...")
for _ in range(12):
    try:
        r = requests.get(f"{PORTAL}/api/status", timeout=20)
        if r.status_code == 200:
            print(f"Portal OK ({r.json().get('total_receipts', 0)} receipts atuais)")
            break
    except Exception:
        pass
    time.sleep(10)

print(f"Importando em lotes de {BATCH}...")
total_ok = 0
t0 = time.time()

for i in range(0, len(receipts), BATCH):
    lote = receipts[i:i + BATCH]
    for tentativa in range(3):
        try:
            r = requests.post(
                f"{PORTAL}/api/bulk-import",
                json={"receipts": lote},
                timeout=120,
            )
            if r.status_code == 200:
                total_ok += r.json().get("imported", len(lote))
                break
            time.sleep(5)
        except Exception as e:
            if tentativa == 2:
                print(f"  ERRO lote {i}: {e}")
            time.sleep(5)
    pct = (i + len(lote)) / len(receipts) * 100
    print(f"[{i + len(lote)}/{len(receipts)}] {pct:.0f}%  {time.time() - t0:.0f}s")

print(f"\nFim: {total_ok} importadas em {time.time() - t0:.0f}s")
try:
    s = requests.get(f"{PORTAL}/api/status", timeout=15).json()
    print(f"Portal: {s['total_receipts']} receipts")
except Exception:
    pass
