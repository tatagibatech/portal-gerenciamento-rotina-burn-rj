"""
Carga inicial de ASNs no portal a partir do Excel exportado do WMS.

O Excel tem 2 linhas de cabecalho (nomes tecnicos + rotulos PT-BR).
Dados a partir da linha 3.

Colunas usadas:
  C  (col 3)  = RECEIPTKEY
  M  (col 13) = STATUS
  N  (col 14) = TYPE      (8 = Ordem de Producao)
  CP (col 95) = EDITDATE  (data da ultima atualizacao)

Uso:
  python carga_excel_asn.py "arquivo.xlsx"               # 30 dias (padrao)
  python carga_excel_asn.py "arquivo.xlsx" --dias 60     # ultimos 60 dias
  python carga_excel_asn.py "arquivo.xlsx" --todas       # inclui fechadas
  python carga_excel_asn.py "arquivo.xlsx" --sem-filtro-data  # historico completo
"""
import sys
import time
import argparse
import requests
import openpyxl
from datetime import datetime, date, timedelta

PORTAL = "https://painel-burn-rj.onrender.com"

COL_RECEIPTKEY = 3   # C
COL_STATUS     = 13  # M
COL_TYPE       = 14  # N
COL_EDITDATE   = 94  # CP

STATUS_FECHADO = {"11", "15"}


def _parse_date(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    # Normaliza todos os tipos de espaco (incluindo narrow no-break space   do Excel)
    s = "".join(" " if ord(c) in (160, 8239, 8201) else c for c in str(val).strip())
    for fmt in ("%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def ler_keys_do_excel(caminho, apenas_tipo_8=True, data_minima=None):
    print(f"Lendo {caminho}...")
    if data_minima:
        print(f"  Filtro de data: EDITDATE >= {data_minima}")
    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    ws = wb.active

    keys = []
    ignoradas_tipo = ignoradas_data = parse_fail = 0

    for row in ws.iter_rows(min_row=3, values_only=True):
        rk       = str(row[COL_RECEIPTKEY - 1] or "").strip()
        status   = str(row[COL_STATUS - 1]     or "").strip()
        tipo     = str(row[COL_TYPE - 1]       or "").strip()
        editdate = row[COL_EDITDATE - 1]

        if not rk or rk == "RECEIPTKEY":
            continue
        if apenas_tipo_8 and tipo != "8":
            ignoradas_tipo += 1
            continue
        if data_minima:
            dt = _parse_date(editdate)
            if dt is None:
                parse_fail += 1
                # Se nao conseguiu parsear, inclui por seguranca
            elif dt < data_minima:
                ignoradas_data += 1
                continue

        keys.append((rk, status))

    wb.close()
    n_dias = (date.today() - data_minima).days if data_minima else 0
    print(f"  {len(keys)} ASNs selecionadas.")
    if ignoradas_tipo:
        print(f"  {ignoradas_tipo} ignoradas (tipo != OP).")
    if ignoradas_data:
        print(f"  {ignoradas_data} ignoradas (fora dos ultimos {n_dias} dias).")
    if parse_fail:
        print(f"  {parse_fail} com data nao parseavel (incluidas por seguranca).")
    return keys


def enviar_para_portal(keys, apenas_abertas=True, limite=None):
    if limite:
        keys = keys[:limite]
        print(f"Limitando a {limite} ASNs.")

    ok = err = skip = 0
    print(f"\nEnviando {len(keys)} receiptkeys para o portal...")

    for i, (rk, status_excel) in enumerate(keys, 1):
        if apenas_abertas and status_excel in STATUS_FECHADO:
            skip += 1
            continue

        try:
            r = requests.post(
                f"{PORTAL}/api/webhook-asn",
                json={"receiptkey": rk},
                timeout=30,
            )
            if r.status_code == 200:
                d = r.json()
                st  = d.get("status", "?")
                dep = d.get("deposito", "?")
                ok += 1
                if i % 20 == 0 or i == len(keys):
                    print(f"  [{i}/{len(keys)}] {rk} dep={dep} status={st}")
            elif r.status_code == 404:
                skip += 1
            else:
                err += 1
                print(f"  [{i}/{len(keys)}] {rk} ERRO {r.status_code}: {r.text[:80]}")
        except Exception as e:
            err += 1
            print(f"  [{i}/{len(keys)}] {rk} EXCECAO: {e}")

        time.sleep(0.05)

    print(f"\nResultado: {ok} indexadas, {skip} ignoradas (fechadas/nao encontradas), {err} erros.")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Carga de ASNs do Excel para o portal")
    parser.add_argument("arquivo", nargs="?",
                        default="2026 08 10 - Carga inicial ASN OP.xlsx",
                        help="Caminho do arquivo Excel exportado do WMS")
    parser.add_argument("--dias", type=int, default=30,
                        help="Backlog: apenas ASNs atualizadas nos ultimos N dias (default: 30)")
    parser.add_argument("--sem-filtro-data", action="store_true",
                        help="Carrega todo o historico sem filtro de data")
    parser.add_argument("--todas", action="store_true",
                        help="Envia todas incluindo fechadas")
    parser.add_argument("--limite", type=int, default=0,
                        help="Limite de ASNs a enviar (0 = sem limite)")
    args = parser.parse_args()

    data_minima = None
    if not args.sem_filtro_data:
        data_minima = date.today() - timedelta(days=args.dias)

    keys = ler_keys_do_excel(args.arquivo, apenas_tipo_8=True, data_minima=data_minima)
    if not keys:
        print("Nenhuma receiptkey encontrada com os filtros aplicados.")
        sys.exit(1)

    ok = enviar_para_portal(
        keys,
        apenas_abertas=False,
        limite=args.limite or None,
    )

    try:
        s = requests.get(f"{PORTAL}/api/status", timeout=15).json()
        print(f"\nPortal apos carga:")
        print(f"  Total receipts    : {s['total_receipts']}")
        print(f"  Ultima atualizacao: {s['ultima_atualizacao']}")
    except Exception as e:
        print(f"Erro ao verificar status: {e}")


if __name__ == "__main__":
    main()
