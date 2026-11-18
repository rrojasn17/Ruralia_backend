"""Auditoría de solo lectura de los dos instrumentos Lirio, sin dependencias.

Usa valores guardados en el XLSX; no ejecuta ni recalcula fórmulas. Los totales
numéricos omiten texto, como SUM de Excel. Cada conteo textual se informa como
incidencia y nunca se presenta como un cero confirmado.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_book(path):
    result = {}
    with ZipFile(path) as z:
        strings = []
        for _, node in ET.iterparse(z.open("xl/sharedStrings.xml"), events=["end"]):
            if node.tag == f"{{{NS['m']}}}si":
                strings.append("".join(t.text or "" for t in node.iter(f"{{{NS['m']}}}t")))
                node.clear()
        rels = {n.attrib["Id"]: n.attrib["Target"] for n in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        for sheet in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS):
            target = rels[sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
            target = posixpath.normpath("xl/" + target) if not target.startswith("/") else target[1:]
            rows = []
            for _, node in ET.iterparse(z.open(target), events=["end"]):
                if node.tag != f"{{{NS['m']}}}row":
                    continue
                row = {"row": int(node.attrib["r"])}
                for cell in node.findall("m:c", NS):
                    value_node = cell.find("m:v", NS)
                    value = value_node.text if value_node is not None else None
                    kind = cell.attrib.get("t")
                    if kind == "s" and value is not None:
                        value = strings[int(value)]
                    elif kind == "inlineStr":
                        value = "".join(t.text or "" for t in cell.findall(".//m:t", NS))
                    elif value is not None and kind not in {"str", "e"}:
                        try:
                            value = float(value) if "." in value or "E" in value.upper() else int(value)
                        except ValueError:
                            pass
                    if value is not None:
                        row[re.sub(r"\d", "", cell.attrib["r"])] = value
                if len(row) > 1:
                    rows.append(row)
                node.clear()
            result[sheet.attrib["name"]] = rows
    return result


def number(value):
    return value if isinstance(value, (int, float)) else 0


def key(value):
    # BUSCARV exacto no distingue mayúsculas. No se eliminan espacios de la llave.
    return str(value or "").upper()


def as_date(value):
    if isinstance(value, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=value)).date()
    if isinstance(value, str):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                pass
    return None


def iso_week(value):
    year, week, _ = value.isocalendar()
    return f"{year % 100:02}-{week:02}"


def group(rows, column="A"):
    result = collections.defaultdict(list)
    for row in rows:
        result[key(row.get(column))].append(row)
    return result


def write_csv(path, records, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def audit(name, book):
    p, h, e, o = (book[n][1:] for n in ("Siembra", "Corta", "Entradas", "Salida Bulbo"))
    pg, hg = group(p), group(h)
    issues = []

    def issue(sheet, row, kind, cell, observed, expected):
        issues.append(dict(archivo=name, hoja=sheet, fila=row, tipo=kind, celda=cell,
                           observado=str(observed), esperado=str(expected)))

    for k, values in pg.items():
        if len(values) > 1:
            for row in values:
                issue("Siembra", row["row"], "llave_no_unica", f"A{row['row']}", k,
                      "ID de segmento único; revisar lado, variedad y origen")
    for sheet, rows, columns in (("Salida Bulbo", o, ("I", "L", "M")),
                                  ("Entradas", e, ("J", "K", "L"))):
        for row in rows:
            a, b, c = columns
            expected = number(row.get(a)) * number(row.get(b))
            if abs(expected - number(row.get(c))) > .001:
                issue(sheet, row["row"], "producto_no_coincide", f"{c}{row['row']}", row.get(c), expected)
    for row in h:
        r = row["row"]
        for col in ("N", "P", "Q", "R", "S", "T"):
            if col in row and not isinstance(row[col], (int, float)):
                issue("Corta", r, "texto_en_conteo", f"{col}{r}", repr(row[col]), "Cantidad numérica por revisar")
        expected_sale = sum(number(row.get(c)) for c in ("P", "Q", "R"))
        expected_total = number(row.get("S")) + number(row.get("N"))
        if expected_sale != number(row.get("S")):
            issue("Corta", r, "venta_no_coincide", f"S{r}", row.get("S"), expected_sale)
        if expected_total != number(row.get("T")):
            issue("Corta", r, "total_no_coincide", f"T{r}", row.get("T"), expected_total)
        seeds = pg.get(key(row.get("A")), [])
        if not seeds:
            issue("Corta", r, "sin_siembra", f"A{r}", row.get("A"), "Llave presente en Siembra")
        else:
            first = seeds[0]
            for hc, pc in (("E", "F"), ("F", "G"), ("G", "H"), ("H", "I"), ("I", "J"), ("K", "N")):
                if str(row.get(hc, "")).strip().upper() != str(first.get(pc, "")).strip().upper():
                    issue("Corta", r, "busqueda_no_coincide", f"{hc}{r}", row.get(hc), first.get(pc))
    for sheet, rows, columns in (("Siembra", p, ("C", "B", "L", "E")),
                                ("Corta", h, ("C", "B", "J", "D"))):
        for row in rows:
            expected = "".join(str(row.get(c, "")) for c in columns)
            if key(row.get("A")) != key(expected):
                issue(sheet, row["row"], "llave_no_coincide_con_componentes", f"A{row['row']}", row.get("A"), expected)
            dt = as_date(row.get("M"))
            wc = "L"
            if dt and iso_week(dt) != str(row.get(wc, "")).strip():
                issue(sheet, row["row"], "fecha_semana_iso_distinta", f"M{row['row']}", row.get("M"),
                      f"{iso_week(dt)}; guardado: {row.get(wc)}")
    signatures = collections.defaultdict(list)
    for row in h:
        signatures[tuple((k, str(v)) for k, v in sorted(row.items()) if k != "row")].append(row["row"])
    duplicate_groups = [v for v in signatures.values() if len(v) > 1]
    for duplicate in duplicate_groups:
        for r in duplicate:
            issue("Corta", r, "posible_evento_duplicado", f"A{r}", duplicate, "Verificar original; no eliminar automáticamente")
    reconciliation = []
    for k, seeds in pg.items():
        cuts = hg.get(k, [])
        planted = sum(number(r.get("J")) for r in seeds)
        output = sum(number(r.get("T")) for r in cuts)
        components = sum(sum(number(r.get(c)) for c in ("N", "P", "Q", "R")) for r in cuts)
        reconciliation.append(dict(
            llave=k, filas_siembra=";".join(str(r["row"]) for r in seeds),
            filas_corta=";".join(str(r["row"]) for r in cuts), segmentos=len(seeds), eventos=len(cuts),
            bulbos_sembrados=planted, salida_total_guardada=output,
            suma_componentes_numericos=components, diferencia_con_factor_1=planted-output,
            variedades=";".join(sorted({str(r.get("I", "")).strip().upper() for r in seeds})),
            estado=("sin_cortas" if not cuts else "excede_con_factor_1" if output > planted
                    else "igual_con_factor_1" if output == planted else "menor_con_factor_1"),
            llave_ambigua=len(seeds) > 1,
            conteos_textuales=sum(any(c in r and not isinstance(r[c], (int, float)) for c in ("N", "P", "Q", "R", "S", "T")) for r in cuts),
        ))
    summary = dict(
        filas={n: len(book[n])-1 for n in ("Entradas", "Salida Bulbo", "Siembra", "Corta")},
        totales_numericos=dict(entradas=sum(number(r.get("L")) for r in e),
                              salidas=sum(number(r.get("M")) for r in o),
                              siembra=sum(number(r.get("J")) for r in p),
                              venta=sum(number(r.get("S")) for r in h),
                              descarte=sum(number(r.get("N")) for r in h),
                              total_corta=sum(number(r.get("T")) for r in h),
                              cantidad_repetida_en_corta=sum(number(r.get("I")) for r in h)),
        llaves_siembra=len(pg), llaves_corta=len(hg),
        llaves_siembra_no_unicas=sum(len(v) > 1 for v in pg.values()),
        llaves_no_unicas_con_lados_distintos=sum(len(v) > 1 and len({r.get("D") for r in v}) > 1 for v in pg.values()),
        cortas_con_llave_ambigua=sum(len(hg.get(k, [])) for k, v in pg.items() if len(v) > 1),
        estados_con_factor_1=dict(collections.Counter(r["estado"] for r in reconciliation)),
        exceso_bruto_con_factor_1=sum(max(0, -r["diferencia_con_factor_1"]) for r in reconciliation),
        incidencias_por_tipo=dict(collections.Counter(r["tipo"] for r in issues)),
        grupos_corta_identicos=duplicate_groups,
    )
    return summary, issues, reconciliation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("excel_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    books, summaries, issues, reconciliations = {}, {}, [], {}
    for name in ("Lirio.xlsx", "Lirio (2).xlsx"):
        path = args.excel_dir / name
        books[name] = read_book(path)
        summary, findings, groups = audit(name, books[name])
        summary["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        summaries[name] = summary
        issues.extend(findings)
        reconciliations[name] = groups
    differences = []
    a, b = books.values()
    for sheet in a:
        ar, br = ({r["row"]: r for r in book[sheet]} for book in (a, b))
        for r in sorted(ar.keys() | br.keys()):
            for c in sorted((ar.get(r, {}).keys() | br.get(r, {}).keys()) - {"row"}):
                av, bv = ar.get(r, {}).get(c), br.get(r, {}).get(c)
                if av != bv:
                    differences.append(dict(hoja=sheet, celda=f"{c}{r}", lirio=av, lirio_2=bv))
    summaries["metodologia"] = "Valores guardados, sin recalcular. Llave sin distinguir mayúsculas. Factor 1 solo diagnóstico; texto omitido en sumas y señalado como incidencia. Diferencias de fechas contra ISO, sin asumir que sea el calendario acordado."
    (args.output_dir / "resumen.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2) + "\n")
    write_csv(args.output_dir / "incidencias_excel.csv", issues,
              ["archivo", "hoja", "fila", "tipo", "celda", "observado", "esperado"])
    write_csv(args.output_dir / "diferencias_entre_archivos.csv", differences,
              ["hoja", "celda", "lirio", "lirio_2"])
    # La copia (2) tiene más salidas y correcciones; no se presume que sea la fuente oficial.
    groups = reconciliations["Lirio (2).xlsx"]
    write_csv(args.output_dir / "conciliacion_por_llave.csv", groups, list(groups[0]))
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
