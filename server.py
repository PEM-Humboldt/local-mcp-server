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
    PORT       puerto HTTP (default: 8001 — 8000 lo usa el dev server de geo-agent)

Para conectar desde geo-agent se pone en layers-input.json (o config.json local):
    "mcp_url": "http://localhost:8001/mcp"
"""
import os
from pathlib import Path

import duckdb
import uvicorn
from mcp.server.fastmcp import FastMCP

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data")).resolve()
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
def list_datasets() -> str:
    """Lista los datasets (.parquet) disponibles en el servidor local, con su
    ruta absoluta y número de filas. Llama esto primero, antes de escribir
    cualquier SQL, para saber qué puedes consultar."""
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
def get_schema(dataset_id: str) -> str:
    """Muestra la ruta exacta y las columnas/tipos de un dataset.
    Usa el nombre devuelto por list_datasets (sin la extensión .parquet).
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
    """Placeholder — el docstring real (usado como descripción del tool para
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
                "\n\n⚠️ Mostrando solo las primeras 50 filas — es una vista previa, "
                "no el conteo real. Usa COUNT/SUM/AVG para totales."
            )
        return md
    except Exception as e:
        return f"Error SQL: {e}"


query.__doc__ = f"""
Ejecuta SQL DuckDB sobre los datasets locales indexados en H3.

ANTES de escribir SQL:
1. Llama list_datasets para ver qué datasets existen.
2. Llama get_schema(dataset_id) para obtener la ruta EXACTA y las columnas.
3. Usa siempre `read_parquet('<ruta exacta devuelta por get_schema>')` —
   no existen tablas, y no debes adivinar ni modificar la ruta.

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


@mcp.prompt("geospatial-analyst")
def analyst_persona() -> str:
    return (
        "Eres un analista geoespacial. Antes de cualquier consulta SQL, llama "
        "list_datasets y get_schema para conocer los datasets y sus rutas "
        "exactas. Usa las funciones H3 de DuckDB para operaciones espaciales "
        "(nunca ST_Intersects/ST_Within sobre datos ya indexados en hex). "
        "Reporta siempre áreas en km², nunca conteos de hex crudos."
    )


if __name__ == "__main__":
    datasets = _discover_datasets()
    print(f"DATA_DIR: {DATA_DIR}")
    print(f"Datasets encontrados: {len(datasets)} → {list(datasets)}")
    print(f"Sirviendo MCP en http://127.0.0.1:{PORT}/mcp")

    app = mcp.streamable_http_app()
    app.router.redirect_slashes = False
    uvicorn.run(app, host="127.0.0.1", port=PORT)
