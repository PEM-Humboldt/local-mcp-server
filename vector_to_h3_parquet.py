#!/usr/bin/env python3
"""
vector_to_h3_parquet.py — Convierte un vector de polígonos/multipolígonos
(GeoJSON, Shapefile, GeoPackage...) en parquet indexado en H3: una fila por
cada (feature, hexágono) que esa geometría cubre. Es el mismo modelo de
"tiling" que usa el resto del ecosistema para límites administrativos,
páramos, cuencas, áreas protegidas, etc. (ver h3-guide.md, sección
"Vector datasets").

A diferencia del raster (to_h3_parquet.py: una fila por hexágono, con un
valor YA agregado), aquí cada fila conserva el hexágono + los atributos
del feature original (nombre, código...), porque un mismo polígono puede
cubrir miles de hexágonos — hay que deduplicar por _cng_fid al sumar
atributos (ver h3-guide.md, "Problem 1 — Tiling").

Ejemplos:

    # Departamentos, conservando solo nombre y código
    python vector_to_h3_parquet.py \\
        --source data_previa/departamentos.geojson \\
        --output data/departamentos_h8.parquet \\
        --resolution 8 --parent-resolutions "6,0" \\
        --keep-columns dpto_cnmbr,dpto_ccdgo

    # Páramos
    python vector_to_h3_parquet.py \\
        --source data_previa/paramos.geojson \\
        --output data/paramos_h8.parquet \\
        --resolution 8 --parent-resolutions "6,0" \\
        --keep-columns NM_UA,COD_CMPLJ,Area_Ha

Requiere: duckdb (instala solo las extensiones `spatial` y `h3` la primera
vez que corre). No necesita GDAL como binario aparte — DuckDB lee GeoJSON
directamente.
"""
import argparse
import sys

import duckdb


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--source", required=True, help="Archivo vectorial (.geojson, .shp, .gpkg...)")
    p.add_argument("--output", required=True, help="Ruta del parquet de salida")
    p.add_argument("--resolution", type=int, default=8, help="Resolución H3 de tiling (default: 8)")
    p.add_argument("--parent-resolutions", default="0",
                    help="Resoluciones padre a precomputar, separadas por coma (default: 0)")
    p.add_argument("--keep-columns", default=None,
                    help="Columnas de atributos a conservar, separadas por coma. "
                         "Si se omite, se conservan todas menos la geometría "
                         "(cuidado: algunos exports traen columnas 'json'/basura anidadas).")
    p.add_argument("--layer", default=None,
                    help="Nombre del layer, para GeoPackage/FileGDB con varias capas")
    args = p.parse_args()

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL h3 FROM community; LOAD h3;")

    layer_clause = f", layer='{args.layer}'" if args.layer else ""
    con.execute(f"CREATE VIEW src AS SELECT * FROM ST_Read('{args.source}'{layer_clause})")

    all_cols = [r[0] for r in con.execute("DESCRIBE src").fetchall()]
    geom_col = next((c for c in all_cols if c.lower() in ("geom", "geometry", "shape")), None)
    if geom_col is None:
        sys.exit(f"No se encontró columna de geometría. Columnas disponibles: {all_cols}")

    if args.keep_columns:
        keep = [c.strip() for c in args.keep_columns.split(",")]
        missing = [c for c in keep if c not in all_cols]
        if missing:
            sys.exit(f"Columnas no encontradas: {missing}. Disponibles: {all_cols}")
    else:
        keep = [c for c in all_cols if c != geom_col]

    n_features = con.execute("SELECT COUNT(*) FROM src").fetchone()[0]
    print(f"Fuente: {args.source}")
    print(f"  {n_features:,} features | columnas conservadas: {keep}")

    col_list = ", ".join(f'"{c}"' for c in keep) if keep else ""
    col_prefix = f"{col_list}, " if col_list else ""
    h_col = f"h{args.resolution}"

    parent_resolutions = sorted(
        {int(x) for x in args.parent_resolutions.split(",") if x.strip() != ""}
    )
    parent_resolutions = [r for r in parent_resolutions if r < args.resolution]
    parent_exprs = ", " + ", ".join(
        f"h3_cell_to_parent({h_col}, {r}) AS h{r}" for r in parent_resolutions
    ) if parent_resolutions else ""

    print(f"Tilando a H3 resolución {args.resolution}...")
    con.execute(f"""
        COPY (
            WITH parts AS (
                SELECT row_number() OVER () AS _cng_fid, {col_prefix}
                       UNNEST(ST_Dump(ST_Multi({geom_col}))).geom AS part_geom
                FROM src
            ),
            hexed AS (
                SELECT _cng_fid, {col_prefix}
                       UNNEST(h3_polygon_wkt_to_cells(ST_AsText(part_geom), {args.resolution})) AS {h_col}
                FROM parts
            )
            SELECT _cng_fid, {col_prefix}{h_col} {parent_exprs}
            FROM hexed
        ) TO '{args.output}' (FORMAT PARQUET, COMPRESSION 'zstd')
    """)

    n_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{args.output}')").fetchone()[0]
    n_out_feat = con.execute(
        f"SELECT COUNT(DISTINCT _cng_fid) FROM read_parquet('{args.output}')"
    ).fetchone()[0]
    print(f"\n✓ Escrito {args.output} — {n_rows:,} filas (hexágono × feature), {n_out_feat:,} features")


if __name__ == "__main__":
    main()
