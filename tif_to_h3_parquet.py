#!/usr/bin/env python3
"""
to_h3_parquet.py — Convierte un raster COG (local, remoto, o resuelto desde
un item STAC) en parquet indexado en H3. Corre 100% en local: no sube ni
descarga nada a S3, no requiere Kubernetes.

Estrategia (ver docs/mcp-data-server/h3-guide.md del repo geo-agent para el
porqué de este modelo de datos): el raster se recorre por ventanas de
píxeles (para no cargarlo entero en memoria), cada ventana se exporta a XYZ
con GDAL y se agrega en DuckDB usando la extensión `h3` — mucho más rápido
que convertir píxel a celda en un loop de Python. Al final se colapsa todo
a una fila por celda H3 (moda para datos categóricos, promedio para
continuos), como exige el modelo "un row por hex" del resto del ecosistema.

Ejemplos:

    # Desde un item STAC (resuelve el href del asset automáticamente)
    python to_h3_parquet.py \\
        --stac-item http://172.191.168.255:8082/collections/Coberturas/items/2020 \\
        --output data/coberturas_h8.parquet \\
        --resolution 8 \\
        --labels "1=Natural,2=Secundaria,3=Transformada"

    # Desde una URL de COG directa, dato continuo (promedio en vez de moda)
    python to_h3_parquet.py \\
        --source https://.../elevation.tif \\
        --output data/elevation_h8.parquet \\
        --resolution 8 --agg avg --value-column elevation_m

    # Desde un archivo local — --source también acepta rutas locales,
    # no solo URLs. No requiere red en absoluto.
    python to_h3_parquet.py \\
        --source /ruta/local/mi_raster.tif \\
        --output data/mi_raster_h8.parquet \\
        --resolution 8

Requiere: duckdb, requests (pip) y los binarios gdalinfo/gdal_translate en
el PATH (paquete gdal-bin / conda-forge::gdal).
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile

import duckdb
import requests


def resolve_stac_asset(item_url: str, asset_key: str | None = None) -> str:
    """Descarga un item STAC y devuelve el href del asset (raster) pedido."""
    resp = requests.get(item_url, timeout=30)
    resp.raise_for_status()
    item = resp.json()
    assets = item.get("assets", {})
    if not assets:
        raise ValueError(f"El item STAC no tiene assets: {item_url}")
    if asset_key:
        if asset_key not in assets:
            raise ValueError(f"Asset '{asset_key}' no existe. Disponibles: {list(assets)}")
        return assets[asset_key]["href"]
    key = next(iter(assets))
    if len(assets) > 1:
        print(f"  (usando asset '{key}' de {list(assets)} — pasa --asset para elegir otro)")
    return assets[key]["href"]


def gdal_vsi_path(source: str) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        return f"/vsicurl/{source}"
    return source


def download_to_local(url: str, dest_dir: str) -> str:
    """Descarga el raster completo a disco antes de recortarlo por ventanas.

    Streaming windowed reads over /vsicurl/ against many small windows can
    hang or time out on flaky connections (each window is its own set of
    HTTP range requests). A single sequential GET of the whole COG is far
    more reliable, and for a whole-country conversion we end up reading
    nearly the entire file anyway, so there's no bandwidth downside.
    """
    ext = os.path.splitext(url.split("?")[0])[1] or ".tif"
    local_path = os.path.join(dest_dir, f"source{ext}")
    print(f"Descargando raster de origen a disco local (una sola vez)...")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = 100 * done / total
                    print(f"\r  {done / 1e6:,.0f} / {total / 1e6:,.0f} MB ({pct:.0f}%)",
                          end="", flush=True)
        print()
    print(f"  ✓ Descargado a {local_path}")
    return local_path


def raster_info(vsi_path: str) -> dict:
    out = subprocess.run(
        ["gdalinfo", "-json", vsi_path],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return json.loads(out.stdout)


def parse_labels(spec: str | None) -> dict:
    if not spec:
        return {}
    out = {}
    for pair in spec.split(","):
        k, _, v = pair.partition("=")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--source",
                      help="Ruta al raster: una ruta local (ej. /home/tu_usuario/mi_raster.tif) "
                           "o una URL remota (http/https) a un COG/GeoTIFF. No requiere STAC.")
    src.add_argument("--stac-item",
                      help="Alternativa a --source: URL de un item STAC. El script resuelve "
                           "el href del asset por ti en vez de que se lo des directamente.")
    p.add_argument("--asset", help="Nombre del asset a usar si --stac-item tiene varios")
    p.add_argument("--output", required=True, help="Ruta del parquet de salida")
    p.add_argument("--resolution", type=int, default=8,
                    help="Resolución H3 de la celda final (default: 8, ~0.7 km²/celda). "
                         "Para una primera prueba rápida usa 6 (~36 km²/celda).")
    p.add_argument("--parent-resolutions", default="0",
                    help="Resoluciones padre a precomputar, separadas por coma (default: 0)")
    p.add_argument("--band", type=int, default=1, help="Banda a leer (default: 1)")
    p.add_argument("--agg", choices=["mode", "avg"], default="mode",
                    help="Agregación por celda: 'mode' (clase mayoritaria, para datos "
                         "categóricos) o 'avg' (promedio, para datos continuos). Default: mode")
    p.add_argument("--window", type=int, default=4096,
                    help="Tamaño de ventana en píxeles para procesar por partes (default: 4096)")
    p.add_argument("--nodata", type=float, default=None,
                    help="Valor NoData a excluir (auto-detectado del raster si se omite)")
    p.add_argument("--labels", default=None,
                    help='Etiquetas legibles para valores categóricos, ej: '
                         '"1=Natural,2=Secundaria,3=Transformada" (solo con --agg mode)')
    p.add_argument("--value-column", default="value", help="Nombre de la columna de salida")
    p.add_argument("--keep-work-dir", action="store_true",
                    help="No borrar los archivos temporales (para depurar)")
    p.add_argument("--no-download", action="store_true",
                    help="No descargar el raster remoto a disco primero — leerlo por "
                         "streaming (/vsicurl/) ventana a ventana. Más lento y menos "
                         "confiable en conexiones inestables; úsalo solo si el archivo "
                         "es demasiado grande para tu disco.")
    args = p.parse_args()

    source = args.source or resolve_stac_asset(args.stac_item, args.asset)
    print(f"Fuente: {source}")

    work_dir = tempfile.mkdtemp(prefix="raster_to_h3_")

    is_remote = source.startswith("http://") or source.startswith("https://")
    if is_remote and not args.no_download:
        source = download_to_local(source, work_dir)
    vsi_path = gdal_vsi_path(source)

    info = raster_info(vsi_path)
    width, height = info["size"]
    band_info = info["bands"][args.band - 1]
    nodata = args.nodata if args.nodata is not None else band_info.get("noDataValue")
    # gdalinfo -json renders NaN nodata as the *string* "nan"/"NaN", not a
    # JSON float — normalize before the isnan() check below.
    if isinstance(nodata, str):
        try:
            nodata = float(nodata)
        except ValueError:
            nodata = None
    is_nan_nodata = nodata is None or (isinstance(nodata, float) and math.isnan(nodata))
    print(f"  Tamaño: {width} x {height} px | NoData: {nodata}")

    parent_resolutions = sorted(
        {int(x) for x in args.parent_resolutions.split(",") if x.strip() != ""}
    )
    parent_resolutions = [r for r in parent_resolutions if r < args.resolution]

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
    con = duckdb.connect(os.path.join(work_dir, "work.duckdb"))
    con.execute("INSTALL h3 FROM community; LOAD h3;")
    con.execute(f"SET temp_directory='{work_dir}'")

    if args.agg == "mode":
        con.execute("CREATE TABLE hex_agg (h UBIGINT, cls DOUBLE, n BIGINT)")
    else:
        con.execute("CREATE TABLE hex_agg (h UBIGINT, sum_v DOUBLE, n BIGINT)")

    where_clause = "Z = Z" if is_nan_nodata else f"Z != {nodata}"  # "Z = Z" is false for NaN

    n_wx = math.ceil(width / args.window)
    n_wy = math.ceil(height / args.window)
    total = n_wx * n_wy
    done = 0

    try:
        for wy in range(n_wy):
            yoff = wy * args.window
            ysize = min(args.window, height - yoff)
            for wx in range(n_wx):
                xoff = wx * args.window
                xsize = min(args.window, width - xoff)
                done += 1

                xyz_path = os.path.join(work_dir, "window.xyz")
                subprocess.run(
                    ["gdal_translate", "-q", "-of", "XYZ",
                     "-b", str(args.band),
                     "-srcwin", str(xoff), str(yoff), str(xsize), str(ysize),
                     vsi_path, xyz_path],
                    check=True, timeout=600,
                )

                if args.agg == "mode":
                    con.execute(f"""
                        INSERT INTO hex_agg
                        SELECT h3_latlng_to_cell(Y, X, {args.resolution}) AS h,
                               Z AS cls, COUNT(*) AS n
                        FROM read_csv('{xyz_path}', delim=' ', header=false,
                                       columns={{'X':'DOUBLE','Y':'DOUBLE','Z':'DOUBLE'}})
                        WHERE {where_clause}
                        GROUP BY h, cls
                    """)
                else:
                    con.execute(f"""
                        INSERT INTO hex_agg
                        SELECT h3_latlng_to_cell(Y, X, {args.resolution}) AS h,
                               SUM(Z) AS sum_v, COUNT(*) AS n
                        FROM read_csv('{xyz_path}', delim=' ', header=false,
                                       columns={{'X':'DOUBLE','Y':'DOUBLE','Z':'DOUBLE'}})
                        WHERE {where_clause}
                        GROUP BY h
                    """)
                os.remove(xyz_path)

                if done % 10 == 0 or done == total:
                    n_cells = con.execute("SELECT COUNT(DISTINCT h) FROM hex_agg").fetchone()[0]
                    print(f"  ventana {done}/{total} — {n_cells:,} celdas h{args.resolution} hasta ahora")

        print("Agregando resultado final (una fila por celda)...")
        parent_exprs = ", " + ", ".join(
            f"h3_cell_to_parent(h{args.resolution}, {r}) AS h{r}" for r in parent_resolutions
        ) if parent_resolutions else ""

        if args.agg == "mode":
            labels = parse_labels(args.labels)
            label_col = ""
            if labels:
                when_clauses = " ".join(f"WHEN {k} THEN '{v}'" for k, v in labels.items())
                label_col = f", CASE {args.value_column} {when_clauses} ELSE CAST({args.value_column} AS VARCHAR) END AS class_label"

            con.execute(f"""
                COPY (
                    SELECT h AS h{args.resolution}, cls AS {args.value_column}, n AS pixel_count
                    FROM (
                        SELECT h, cls, n,
                               ROW_NUMBER() OVER (PARTITION BY h ORDER BY n DESC) AS rn
                        FROM (SELECT h, cls, SUM(n) AS n FROM hex_agg GROUP BY h, cls)
                    )
                    WHERE rn = 1
                ) TO '{work_dir}/final_base.parquet' (FORMAT PARQUET)
            """)
            con.execute(f"""
                COPY (
                    SELECT * {parent_exprs} {label_col}
                    FROM read_parquet('{work_dir}/final_base.parquet')
                ) TO '{args.output}' (FORMAT PARQUET, COMPRESSION 'zstd')
            """)
        else:
            con.execute(f"""
                COPY (
                    SELECT h AS h{args.resolution}, sum_v / n AS {args.value_column}, n AS pixel_count
                           {parent_exprs}
                    FROM (SELECT h, SUM(sum_v) AS sum_v, SUM(n) AS n FROM hex_agg GROUP BY h)
                ) TO '{args.output}' (FORMAT PARQUET, COMPRESSION 'zstd')
            """)

        n_out = con.execute(f"SELECT COUNT(*) FROM read_parquet('{args.output}')").fetchone()[0]
        print(f"\n✓ Escrito {args.output} — {n_out:,} celdas h{args.resolution}")

    finally:
        con.close()
        if args.keep_work_dir:
            print(f"  (archivos intermedios conservados en {work_dir})")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
