#!/usr/bin/env python3
"""
Servidor MCP local para datos geoespaciales H3-indexados en DuckDB.

Corre 100% en local. 
Sirve cualquier archivo .parquet que esté en DATA_DIR como tool
'query' de DuckDB

Uso:
    python server.py
    # → escuchando en http://127.0.0.1:8001/mcp

Variables de entorno:
    DATA_DIR   carpeta con los .parquet a servir (default: ./data)
    PORT       puerto HTTP (default: 8001 )

Para conectar desde geo-agent se pone en layers-input.json (o config.json local):
    "mcp_url": "http://localhost:8001/mcp"
"""
import hashlib
import json
import os
import re
from pathlib import Path

import duckdb
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data")).resolve()
TILES_DIR = Path(os.environ.get("TILES_DIR", Path(__file__).parent / "tiles")).resolve()
PORT = int(os.environ.get("PORT", "8001"))

mcp = FastMCP("local-duckdb-geo", stateless_http=True)


def get_connection() -> duckdb.DuckDBPyConnection:
    """Conexión nueva y aislada por request — nada de estado entre queries."""
    con = duckdb.connect(":memory:")
    con.execute("INSTALL h3 FROM community; LOAD h3;")
    con.execute("INSTALL spatial; LOAD spatial;")
    return con


def _discover_datasets() -> dict[str, Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return {p.stem: p for p in sorted(DATA_DIR.rglob("*.parquet"))}


@mcp.tool()
def list_local_datasets() -> str:
    """Lista los datasets (.parquet) disponibles en el servidor MCP local, con su
    ruta absoluta y número de filas. Llama esto primero, antes de escribir
    cualquier SQL, para saber qué puedes consultar.
    NO confundir con el `Collection ID` del catálogo STAC de la app (p. ej. "Coberturas"):
    esos IDs son solo para capas visuales del mapa y no existen aquí — usa siempre
    el nombre exacto devuelto por esta función para SQL."""
    datasets = _discover_datasets()
    if not datasets:
        return (
            f"No hay archivos .parquet en {DATA_DIR}. "
            "Genera uno con to_h3_parquet.py y vuelve a intentar."
        )
    con = get_connection()
    lines = []
    for name, path in datasets.items():
        try:
            n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{path}')").fetchone()[0]
            lines.append(f"- **{name}** — {n:,} filas — ruta: `{path}`")
        except Exception as e:
            lines.append(f"- **{name}** — error leyendo el archivo: {e}")
    con.close()
    return "\n".join(lines)


@mcp.tool()
def get_local_schema(dataset_id: str) -> str:
    """Muestra la ruta exacta y las columnas/tipos de un dataset local.
    Usa el nombre devuelto por list_local_datasets (sin la extensión .parquet).
    Llama esto antes de escribir SQL sobre un dataset que no conoces."""
    datasets = _discover_datasets()
    if dataset_id not in datasets:
        disponibles = ", ".join(datasets) or "(ninguno)"
        return f"Dataset desconocido: '{dataset_id}'. Disponibles: {disponibles}"
    path = datasets[dataset_id]
    con = get_connection()
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    con.close()
    cols = "\n".join(f"- `{r[0]}` ({r[1]})" for r in rows)
    return (
        f"### {dataset_id}\n"
        f"Ruta exacta a usar en SQL: `read_parquet('{path}')`\n\n"
        f"Columnas:\n{cols}"
    )


def query(sql_query: str) -> str:
    """El docstring real (usado como descripción del tool para
    el LLM) se arma más abajo, para poder inyectar DATA_DIR dinámicamente."""
    try:
        con = get_connection()
        result = con.sql(sql_query)
        if result is None:
            con.close()
            return "Comando ejecutado correctamente."

        df = result.limit(51).df()
        con.close()
        if df.empty:
            return "Sin resultados."
        truncated = len(df) > 50
        md = df.head(50).to_markdown(index=False)
        if truncated:
            md += (
                "\n\nMostrando solo las primeras 50 filas — es una vista previa, "
                "no el conteo real. Usa COUNT/SUM/AVG para totales."
            )
        return md
    except Exception as e:
        return f"Error SQL: {e}"


query.__doc__ = f"""
Ejecuta SQL DuckDB sobre los datasets locales indexados en H3.

ANTES de escribir SQL:
1. Llama list_local_datasets para ver qué datasets existen.
2. Llama get_local_schema(dataset_id) para obtener la ruta EXACTA y las columnas.
3. Usa siempre `read_parquet('<ruta exacta devuelta por get_local_schema>')` —
   no existen tablas, y no debes adivinar ni modificar la ruta.
4. El `Collection ID` del catálogo STAC de la app (p. ej. "Coberturas") es solo
   para capas visuales del mapa — nunca lo pases como dataset_id aquí.

Datos disponibles ahora mismo en {DATA_DIR}:
{chr(10).join(f"- {p.stem}" for p in _discover_datasets().values()) or "(ninguno todavía)"}

Funciones H3 útiles (extensión `h3` de DuckDB, ya cargada en cada conexión):
- h3_cell_to_parent(h, res) — celda padre en una resolución más gruesa
- h3_cell_to_lat(h) / h3_cell_to_lng(h) — coordenadas del centro de la celda
- h3_cell_area(h, 'km^2') — área exacta de una celda

Reglas de agregación:
- Nunca sumes columnas de área/porcentaje del dataset de origen directamente;
  si necesitas área, súmala como SUM(h3_cell_area(h8, 'km^2')) sobre celdas
  DISTINCT.
- Los datos raster ya vienen agregados por celda (una fila por hex) por
  to_h3_parquet.py — no hace falta GROUP BY para eso, pero sí al cruzar
  (JOIN) dos datasets por columna hN.

Extensiones cargadas en cada conexión: h3, spatial.
"""

mcp.tool(name="query", description=query.__doc__)(query)


# -------------------------------------------------------------------------
# Puente STAC (mapa) ↔ parquet local (SQL)
# -------------------------------------------------------------------------
def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _match_local_datasets(dataset_id: str) -> dict:
    """Empareja un Collection ID de STAC contra los .parquet locales por
    coincidencia de nombre normalizado (prefijo o substring) — p. ej.
    "Coberturas" empareja con "coberturas_h6", "coberturas_h8",
    "coberturas_antioquia_h8". Evita mantener un mapeo manual STAC→parquet."""
    target = _normalize(dataset_id)
    return {
        name: path
        for name, path in _discover_datasets().items()
        if _normalize(name).startswith(target) or target in _normalize(name)
    }


def _describe_parquet(con, name: str, path: Path) -> str:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    cols = "\n".join(f"  - `{r[0]}` ({r[1]})" for r in rows)
    block = f"**{name}**\n  Ruta exacta para SQL: `read_parquet('{path}')`\n  Columnas:\n{cols}"
    try:
        sample = con.execute(f"SELECT * FROM read_parquet('{path}') LIMIT 3").df()
        if not sample.empty:
            preview = sample.to_markdown(index=False).replace("\n", "\n  ")
            block += f"\n  Muestra:\n  {preview}"
    except Exception:
        pass
    return block


@mcp.tool()
def get_stac_details(dataset_id: str, collection: dict = None) -> str:
    """Describe, en términos del dataset .parquet local para SQL, un
    Collection ID del catálogo STAC de la app (p. ej. "Coberturas" — el
    mismo ID que usan los tools locales del mapa list_datasets/get_schema).

    Este servidor tiene DOS catálogos separados por diseño (ver AGENTS.md /
    system-prompt.md de la app): el STAC del mapa usa un ID de colección
    para capas visuales, que casi nunca coincide textualmente con el nombre
    del .parquet que sirve este servidor para SQL. Esta herramienta hace de
    puente: recibe el Collection ID de STAC, lo empareja por nombre contra
    los .parquet descubiertos en DATA_DIR, y devuelve la ruta exacta +
    columnas + muestra de cada uno para que puedas escribir SQL de una vez.

    Existe principalmente para que el tool `get_schema` del mapa (que llama
    a esta función por MCP) deje de fallar con "tool not found" — no hace
    falta llamarla directo salvo que quieras confirmar el emparejamiento."""
    lines = [f"### {dataset_id}"]
    if collection:
        if collection.get("title"):
            lines[0] += f" — {collection['title']}"
        if collection.get("description"):
            lines.append(collection["description"])

    matches = _match_local_datasets(dataset_id)
    if not matches:
        available = ", ".join(_discover_datasets()) or "(ninguno)"
        lines.append(
            f"\n Ningún .parquet local corresponde a este Collection ID. "
            f"Datasets SQL disponibles: {available}. "
            "Si el dataset debería existir, revisa el nombre del archivo en DATA_DIR "
            "o llama list_local_datasets."
        )
        return "\n".join(lines)

    con = get_connection()
    try:
        lines.append(f"\nDataset(s) SQL correspondientes ({len(matches)}):\n")
        for name, path in matches.items():
            lines.append(_describe_parquet(con, name, path))
    finally:
        con.close()
    return "\n".join(lines)


# -------------------------------------------------------------------------
# Visualización dinámica: SQL → hexágonos en el mapa
# -------------------------------------------------------------------------
_VALID_AGGS = {"COUNT", "AVG", "SUM", "MIN", "MAX"}
_HEX_LIMIT = 20000


def _tile_paths(h: str) -> dict:
    return {
        "geojson_path": TILES_DIR / f"{h}.geojson",
        "meta_path": TILES_DIR / f"{h}.json",
        "url": f"http://127.0.0.1:{PORT}/tiles/{h}.geojson",
    }


@mcp.tool()
def register_hex_tiles(sql: str, agg: str = "COUNT") -> dict:
    """Convierte un agregado SQL por celda H3 en un GeoJSON de hexágonos
    listo para el tool del mapa `add_hex_tile_layer`.

    A diferencia de register_hex_tiles del servidor público de geo-agent
    (que arma una pirámide de vector tiles en S3 para datasets de escala
    global), esta versión local construye UN SOLO archivo GeoJSON de forma
    síncrona — apropiado para el tamaño de tus datos — y siempre devuelve
    status="done" de inmediato (no hace falta hacer polling, pero
    get_hex_tile_status existe por compatibilidad).

    Parámetros:
    - sql: un SELECT cuya PRIMERA columna sea una celda H3 (cualquier
      resolución).
        - agg="COUNT" (default): sql solo necesita la columna H3; la
          propiedad de salida es "value" (conteo de filas por hexágono).
        - agg="AVG"/"SUM"/"MIN"/"MAX": sql debe devolver la columna H3 y,
          justo después, UNA columna numérica — se agrega por hexágono.
    - agg: "COUNT" (default), "AVG", "SUM", "MIN" o "MAX".

    Devuelve un dict con status="done" (o "failed"+error) y, si tuvo éxito:
    tile_url_template / geojson_url (iguales, formato siempre "geojson"),
    value_columns, value_stats ({<col>: {"by_res": {"<res>": {min, max}}}}),
    bounds ([w,s,e,n]), hash, feature_count.

    Pasa tile_url_template como el `tile_url` de add_hex_tile_layer junto
    con format="geojson" y geojson_url — ver la descripción de
    add_hex_tile_layer para el mapeo completo de campos.

    Límite: máximo 20000 celdas por llamada. Para un mapa más liviano,
    proyecta la columna H3 a una resolución más gruesa en el propio `sql`
    con h3_cell_to_parent(<col>, <resolución_menor>)."""
    agg_u = (agg or "COUNT").upper()
    if agg_u not in _VALID_AGGS:
        return {"status": "failed", "error": f"agg debe ser uno de {sorted(_VALID_AGGS)}"}

    h = hashlib.sha256(f"{sql}|{agg_u}".encode()).hexdigest()[:16]
    paths = _tile_paths(h)
    TILES_DIR.mkdir(parents=True, exist_ok=True)

    if paths["meta_path"].exists():
        meta = json.loads(paths["meta_path"].read_text())
        meta["status"] = "done"
        return meta

    con = get_connection()
    try:
        probe = con.execute(f"SELECT * FROM ({sql}) LIMIT 0")
        cols = [d[0] for d in probe.description]
        if not cols:
            return {"status": "failed", "error": "La consulta no devuelve columnas."}
        h_col = cols[0]

        if agg_u == "COUNT":
            agg_sql = "COUNT(*)"
        else:
            if len(cols) < 2:
                return {
                    "status": "failed",
                    "error": f"agg='{agg_u}' requiere una segunda columna numérica en el SELECT.",
                }
            agg_sql = f'{agg_u}("{cols[1]}")'

        agg_query = f"""
            SELECT "{h_col}" AS h3_cell, {agg_sql} AS value
            FROM ({sql})
            GROUP BY "{h_col}"
        """
        rows = con.execute(f"""
            SELECT h3_cell,
                   value,
                   h3_get_resolution(h3_cell) AS res,
                   ST_AsGeoJSON(ST_GeomFromText(h3_cell_to_boundary_wkt(h3_cell))) AS geom_json
            FROM ({agg_query})
            LIMIT {_HEX_LIMIT + 1}
        """).fetchall()
    except Exception as e:
        return {"status": "failed", "error": f"Error SQL: {e}"}
    finally:
        con.close()

    if not rows:
        return {"status": "failed", "error": "La consulta no produjo ninguna celda H3."}

    truncated = len(rows) > _HEX_LIMIT
    rows = rows[:_HEX_LIMIT]

    features, lons, lats, by_res = [], [], [], {}
    for h3_cell, value, res, geom_json in rows:
        geom = json.loads(geom_json)
        for ring in geom["coordinates"]:
            for lon, lat in ring:
                lons.append(lon)
                lats.append(lat)
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {"h3": str(h3_cell), "value": value},
        })
        bucket = by_res.setdefault(str(res), [])
        bucket.append(value)

    paths["geojson_path"].write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    result = {
        "status": "done",
        "hash": h,
        "tile_url_template": paths["url"],
        "geojson_url": paths["url"],
        "format": "geojson",
        "value_columns": ["value"],
        "value_stats": {"value": {"by_res": {r: {"min": min(v), "max": max(v)} for r, v in by_res.items()}}},
        "bounds": [min(lons), min(lats), max(lons), max(lats)],
        "feature_count": len(features),
    }
    if truncated:
        result["warning"] = (
            f"Resultado truncado a {_HEX_LIMIT} celdas. Para un mapa más liviano, proyecta "
            "el SQL a una resolución más gruesa con h3_cell_to_parent(<col>, <res_menor>)."
        )
    paths["meta_path"].write_text(json.dumps(result))
    return result


@mcp.tool()
def get_hex_tile_status(hash: str, wait_seconds: int = 0) -> dict:
    """Consulta el estado de un build de register_hex_tiles. Incluido por
    compatibilidad de API con el servidor MCP público de geo-agent — este
    servidor local construye de forma síncrona, así que register_hex_tiles
    nunca devuelve status="running" en la práctica y normalmente no
    necesitas llamar esto. Devuelve el mismo dict que register_hex_tiles
    si el build existe en disco, si no {status: "unknown"}."""
    paths = _tile_paths(hash)
    if paths["meta_path"].exists():
        meta = json.loads(paths["meta_path"].read_text())
        meta["status"] = "done"
        return meta
    return {"hash": hash, "status": "unknown"}


# -------------------------------------------------------------------------
# Tools que blindan las reglas de h3-guide.md 
# -------------------------------------------------------------------------
@mcp.tool()
def compute_area(
    dataset_id: str,
    resolution_col: str,
    group_by: str = None,
    filter_sql: str = None,
    unit: str = "km2",
) -> str:
    """Calcula área (km2 o acres) de un dataset local indexado en H3, de la
    ÚNICA forma correcta según h3-guide.md: SUM(h3_cell_area(...)) sobre
    celdas DISTINCT — nunca sumando una columna de área cruda del dataset
    de origen (eso repite el área total del feature una vez por fila/hex y
    infla el resultado 10x–1e6x).

    Args:
      dataset_id: nombre de list_local_datasets (sin .parquet).
      resolution_col: columna H3 a usar, p. ej. "h6" u "h8" (verifica con
        get_local_schema que exista en el dataset).
      group_by: columna opcional para desglosar el área (p. ej.
        "class_label", "dpto_cnmbr"). Omite para un total único.
      filter_sql: fragmento SQL de WHERE opcional (p. ej.
        "dpto_cnmbr = 'ANTIOQUIA'"). Omite para no filtrar.
      unit: "km2" (default) o "acres".

    Siempre deduplica por DISTINCT (resolution_col[, group_by]) antes de
    sumar, para que un feature tilado en muchas filas de hex con los mismos
    atributos no se cuente varias veces."""
    datasets = _discover_datasets()
    if dataset_id not in datasets:
        return f"Dataset desconocido: '{dataset_id}'. Disponibles: {', '.join(datasets) or '(ninguno)'}"
    path = datasets[dataset_id]
    con = get_connection()
    try:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()]
        if resolution_col not in cols:
            return f"Columna '{resolution_col}' no existe en {dataset_id}. Columnas: {cols}"
        if group_by and group_by not in cols:
            return f"Columna group_by '{group_by}' no existe en {dataset_id}. Columnas: {cols}"

        select_cols = f'"{group_by}", "{resolution_col}"' if group_by else f'"{resolution_col}"'
        where = f"WHERE {filter_sql}" if filter_sql else ""
        factor = 247.105 if unit == "acres" else 1.0
        unit_label = "acres" if unit == "acres" else "km2"
        group_sql = f'GROUP BY "{group_by}"\n                ORDER BY area_{unit_label} DESC' if group_by else ""
        select_group = f'"{group_by}",\n                       ' if group_by else ""

        sql = f"""
            SELECT {select_group}COUNT(*) AS n_cells,
                   SUM(h3_cell_area("{resolution_col}", 'km^2')) * {factor} AS area_{unit_label}
            FROM (SELECT DISTINCT {select_cols} FROM read_parquet('{path}') {where})
            {group_sql}
        """
        df = con.execute(sql).df()
    finally:
        con.close()
    return "Sin resultados (revisa filter_sql)." if df.empty else df.to_markdown(index=False)


@mcp.tool()
def summarize_by_category(
    dataset_id: str,
    category_col: str,
    resolution_col: str,
    mask_dataset_id: str = None,
    mask_filter_sql: str = None,
    dataset_filter_sql: str = None,
) -> str:
    """Resume un dataset por columna categórica (conteo, área, %), opcional-
    mente acotado a una región vía SEMI JOIN contra un dataset máscara
    (departamento, cuenca, área protegida...) en la misma resolución H3.
    Es el patrón documentado en el README de este repo ("cobertura natural
    en Antioquia"): primero máscara vía SEMI JOIN, luego GROUP BY.

    Args:
      dataset_id: dataset a resumir (de list_local_datasets).
      category_col: columna categórica para agrupar (p. ej. "class_label").
      resolution_col: columna H3 compartida por dataset_id y
        mask_dataset_id (p. ej. "h8") — ambos deben tener esta columna exacta.
      mask_dataset_id: dataset de región/límite opcional para acotar (p. ej.
        "departamentos_h8"). Omite para resumir todo el dataset.
      mask_filter_sql: fragmento WHERE aplicado al dataset máscara (p. ej.
        "dpto_cnmbr = 'ANTIOQUIA'"). Requerido si se da mask_dataset_id.
      dataset_filter_sql: fragmento WHERE extra opcional sobre el dataset
        principal (p. ej. "class_label IS NOT NULL")."""
    datasets = _discover_datasets()
    if dataset_id not in datasets:
        return f"Dataset desconocido: '{dataset_id}'. Disponibles: {', '.join(datasets) or '(ninguno)'}"
    path = datasets[dataset_id]
    con = get_connection()
    try:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()]
        if category_col not in cols:
            return f"Columna '{category_col}' no existe en {dataset_id}. Columnas: {cols}"
        if resolution_col not in cols:
            return f"Columna '{resolution_col}' no existe en {dataset_id}. Columnas: {cols}"

        scope_cte, semi_join = "", ""
        if mask_dataset_id:
            if mask_dataset_id not in datasets:
                return f"Mask dataset desconocido: '{mask_dataset_id}'. Disponibles: {', '.join(datasets)}"
            mask_path = datasets[mask_dataset_id]
            mask_cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{mask_path}')").fetchall()]
            if resolution_col not in mask_cols:
                return (
                    f"El mask '{mask_dataset_id}' no tiene la columna '{resolution_col}'. "
                    f"Columnas del mask: {mask_cols}. Usa una resolución presente en ambos."
                )
            mask_where = f"WHERE {mask_filter_sql}" if mask_filter_sql else ""
            scope_cte = f"""
                mask_hex AS (
                    SELECT DISTINCT "{resolution_col}"
                    FROM read_parquet('{mask_path}')
                    {mask_where}
                ),"""
            semi_join = f'SEMI JOIN mask_hex USING ("{resolution_col}")'

        extra_where = f"AND {dataset_filter_sql}" if dataset_filter_sql else ""
        sql = f"""
            WITH {scope_cte}
            scoped AS (
                SELECT "{resolution_col}", "{category_col}"
                FROM read_parquet('{path}')
                {semi_join}
                WHERE "{category_col}" IS NOT NULL {extra_where}
            )
            SELECT "{category_col}",
                   COUNT(*) AS celdas,
                   SUM(h3_cell_area("{resolution_col}", 'km^2')) AS area_km2,
                   ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
            FROM scoped
            GROUP BY "{category_col}"
            ORDER BY area_km2 DESC
        """
        df = con.execute(sql).df()
    finally:
        con.close()
    return "Sin resultados (revisa los filtros)." if df.empty else df.to_markdown(index=False)


@mcp.tool()
def join_datasets_by_h3(
    dataset_a: str,
    dataset_b: str,
    select_a: str = "a.*",
    select_b: str = None,
    resolution_col: str = None,
    filter_sql: str = None,
    limit: int = 50,
) -> str:
    """Une dos datasets locales indexados en H3 por celda hexagonal, eligiendo
    automáticamente la resolución H3 más gruesa presente en AMBOS esquemas
    (según h3-guide.md: unir siempre convirtiendo la resolución más fina a
    la más gruesa — nunca al revés con h3_cell_to_children). Si no hay
    ninguna columna en común, convierte el lado más fino con
    h3_cell_to_parent() a la resolución más fina del lado más grueso.

    Devuelve una vista previa (primeras `limit` filas, markdown) Y el SQL
    exacto usado, para que puedas revisarlo/ajustarlo.

    Args:
      dataset_a, dataset_b: nombres de list_local_datasets.
      select_a: columnas de dataset_a (default "a.*").
      select_b: columnas de dataset_b (default: todas menos cualquier
        columna h<N>, para evitar columnas hex duplicadas en la salida).
      resolution_col: fuerza una columna compartida específica (p. ej. "h6")
        en vez de auto-detectar la más gruesa en común.
      filter_sql: fragmento WHERE opcional (referencia columnas como
        a.<col> / b.<col> para desambiguar).
      limit: tope de filas de la vista previa (default 50; es una vista
        previa, no un conteo)."""
    datasets = _discover_datasets()
    for d in (dataset_a, dataset_b):
        if d not in datasets:
            return f"Dataset desconocido: '{d}'. Disponibles: {', '.join(datasets) or '(ninguno)'}"
    path_a, path_b = datasets[dataset_a], datasets[dataset_b]
    con = get_connection()
    try:
        cols_a = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path_a}')").fetchall()]
        cols_b = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path_b}')").fetchall()]
        h_pat = re.compile(r"^h(\d+)$")
        res_a = {int(m.group(1)): c for c in cols_a if (m := h_pat.match(c))}
        res_b = {int(m.group(1)): c for c in cols_b if (m := h_pat.match(c))}

        if resolution_col:
            if resolution_col not in cols_a or resolution_col not in cols_b:
                return f"'{resolution_col}' no existe en ambos datasets. A: {cols_a} | B: {cols_b}"
            join_expr = f'a."{resolution_col}" = b."{resolution_col}"'
        else:
            # h0 se excluye a propósito: es la partición global (casi todo
            # dataset la tiene), no una resolución de análisis real — usarla
            # sola como llave de join produce un cruce casi-cartesiano.
            # Entre las resoluciones compartidas reales, la MÁS FINA es
            # preferible (más precisa) — ambas son igual de "gratis" porque
            # ya existen como columnas nativas en los dos datasets.
            shared = sorted((set(res_a) & set(res_b)) - {0}, reverse=True)
            if shared:
                res_used = f"h{shared[0]}"
                join_expr = f'a."{res_used}" = b."{res_used}"'
            elif res_a and res_b:
                finest_a, finest_b = max(res_a), max(res_b)
                if finest_a <= finest_b:
                    join_expr = f'h3_cell_to_parent(b."{res_b[finest_b]}", {finest_a}) = a."{res_a[finest_a]}"'
                else:
                    join_expr = f'h3_cell_to_parent(a."{res_a[finest_a]}", {finest_b}) = b."{res_b[finest_b]}"'
            else:
                return f"No se encontraron columnas H3 (h<N>) en uno de los datasets. A: {cols_a} | B: {cols_b}"

        if "h0" in cols_a and "h0" in cols_b:
            join_expr += ' AND a."h0" = b."h0"'

        sel_b = select_b
        if sel_b is None:
            keep_b = [c for c in cols_b if not h_pat.match(c)]
            sel_b = ", ".join(f'b."{c}"' for c in keep_b) if keep_b else "b.* EXCLUDE (" + ", ".join(cols_b) + ")"

        where = f"WHERE {filter_sql}" if filter_sql else ""
        sql = f"""
            SELECT {select_a}, {sel_b}
            FROM read_parquet('{path_a}') a
            JOIN read_parquet('{path_b}') b ON {join_expr}
            {where}
            LIMIT {int(limit)}
        """
        df = con.execute(sql).df()
    finally:
        con.close()
    preview = "Sin resultados." if df.empty else df.to_markdown(index=False)
    return f"SQL usado:\n```sql\n{sql.strip()}\n```\n\nVista previa:\n{preview}"


@mcp.prompt("geospatial-analyst")
def analyst_persona() -> str:
    return (
        "Eres un analista geoespacial. Antes de cualquier consulta SQL, llama "
        "list_local_datasets y get_local_schema para conocer los datasets y sus rutas "
        "exactas. Usa las funciones H3 de DuckDB para operaciones espaciales "
        "(nunca ST_Intersects/ST_Within sobre datos ya indexados en hex). "
        "Reporta siempre áreas en km², nunca conteos de hex crudos.\n\n"
        "Para tareas comunes usa los tools dedicados en vez de escribir el SQL a mano: "
        "compute_area (área correcta, nunca SUM de una columna de área cruda), "
        "summarize_by_category (conteo/área/% por categoría, con máscara SEMI JOIN opcional), "
        "join_datasets_by_h3 (cruce entre dos datasets, resolución elegida automáticamente), "
        "register_hex_tiles + get_hex_tile_status (visualizar un agregado SQL como capa "
        "de hexágonos en el mapa, vía el tool del mapa add_hex_tile_layer)."
    )


_TILE_NAME_RE = re.compile(r"^[0-9a-f]{16}\.geojson$")


async def _serve_tile(request):
    """Sirve los GeoJSON que register_hex_tiles escribe en TILES_DIR.
    Nombre validado contra un hash hex fijo — el parámetro viene de la URL."""
    name = request.path_params["name"]
    if not _TILE_NAME_RE.fullmatch(name):
        return PlainTextResponse("Not found", status_code=404)
    path = TILES_DIR / name
    if not path.exists():
        return PlainTextResponse("Not found", status_code=404)
    return JSONResponse(json.loads(path.read_text()))


if __name__ == "__main__":
    datasets = _discover_datasets()
    TILES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"DATA_DIR: {DATA_DIR}")
    print(f"TILES_DIR: {TILES_DIR}")
    print(f"Datasets encontrados: {len(datasets)} → {list(datasets)}")
    print(f"Sirviendo MCP en http://127.0.0.1:{PORT}/mcp")

    app = mcp.streamable_http_app()
    app.router.redirect_slashes = False
    app.router.routes.append(Route("/tiles/{name}", _serve_tile))
    # geo-agent-LIB corre en el navegador (p. ej. http://localhost:8000) y le
    # pega a este servidor por fetch() — sin estas cabeceras, el preflight
    # OPTIONS que manda el navegador recibe 405 y la petición real nunca sale.
    app = CORSMiddleware(
        app,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    uvicorn.run(app, host="127.0.0.1", port=PORT)
