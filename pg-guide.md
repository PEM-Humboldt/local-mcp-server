## Guía de la base: Biodiversidad en cifras (SiB Colombia)

Registros biológicos de GBIF para Colombia (~40,5 millones), validados geográfica y taxonómicamente, más tablas de resumen precalculadas por región y grupo biológico.
Fecha de corte: 2026-08-05 (columna `fecha_corte` de `departamento`/`municipio`).

### Qué tabla usar (de la más rápida a la más costosa)
1. **Cifras ya calculadas: úsalas primero.** Responden en milisegundos.
   - `cifras_totales`: totales nacionales de registros y especies.
   - `geografia_resumen`: registros y especies por departamento o municipio. La columna
     `nivel` combina alcance y desglose: `DCDM` = departamentos, `MCDM` = municipios
     (`C*` = vista nacional por departamento). `*CDM` trae el desglose
     continental/marino/salobre y `*SDM` deja esas columnas vacías; los totales son
     iguales. Usa `DCDM`/`MCDM`.
   - `region_grupo`: por región (`slug_region`) × grupo (`slug_grupo`): registros,
     especies, especies/registros amenazados (nacional MADS y global UICN: total, CR, EN, VU),
     CITES, endémicas, exóticas, invasoras, migratorias.
   - `region_tematica`: lo mismo por región, sin desglose por grupo, más
     `especies_region_estimadas` (especies que se estima habitan la región).
   - Las columnas siguen el patrón `especies[_continentales|_marinas|_salobres]_<tema>`;
     llama get_pg_schema antes de elegir una.
   - `especie_region`: registros por especie (`slug_especie`) y región.
   - `publicador` / `region_publicador`: organizaciones que publican datos.
2. **Catálogos**: `region` (slugs de país, departamentos, municipios, resguardos, reservas,
   núcleos DFyB, Región Amazonía), `departamento`, `municipio`, `grupo` (54 grupos:
   `tipo` = `biologico` | `interes`), `especie` (taxonomía por slug),
   `especie_grupo` (especie ↔ grupo), `especie_meta` (nombre común en español),
   `taxonomic_species_validation` (atributos de conservación por especie).
3. **Registros individuales**: `dwc_integrated` (40,5 M filas, Darwin Core). Solo cuando
   la pregunta no se responda con lo anterior (fechas, colectores, instituciones,
   coordenadas, una especie concreta). **Filtra siempre por una columna indexada**:
   `species`, `taxonomic_species_id`, `locality_id`, `datasetkey`, `publishingorgkey`.
   Un GROUP BY sobre la tabla completa supera el límite de tiempo.

### Uniones
- `dwc_integrated.locality_id` → `geo_locality_validation.id`: ubicación validada del
  registro. Trae `stateprovinceslug` y `countyslug` (slugs de departamento y municipio),
  pertenencia ya calculada a `reserve`, `indigenousreserve`, `dfybnucleus`, `amazonregion`
  y `maritimeregion`, un punto `geom` y `flaggeo` (NULL = sin problemas; si no, describe
  la inconsistencia).
- `dwc_integrated.taxonomic_species_id` → `taxonomic_species_validation.id`: atributos de
  la especie (NULL en registros sin identificación a especie, ~9 %).
- `dwc_integrated.datasetkey` → `gbif_datasets`, `publishingorgkey` → `gbif_publishers`.
- `taxonomic_groups`: grupo (`slug`) → `taxon` + `taxonrank` (p. ej. `aves` = class Aves,
  `orquideas` = family Orchidaceae, `frailejones` = 7 géneros).

### Valores codificados (`taxonomic_species_validation`; NULL = no aplica)
- `threatstatusmads` (amenaza nacional): `CR_MADS`, `EN_MADS`, `VU_MADS`.
- `threatstatusuicn` (global): `CR_IUCN`, `EN_IUCN`, `VU_IUCN`, `NT_IUCN`, `LC_IUCN`, `DD_IUCN`, …
- `cites`: `I`, `II`, `III`, `I/II`. `endemic` = `Endémica`, `exotic` = `Exótica`,
  `invasive` = `Invasora`, `migratory` = `Migratorio`, `ismarine` = `Marine`.
- `flagtaxo` = `Ausente en lista taxonómica…`: especie que no está en la lista de
  referencia. Los conteos de especies de las tablas de resumen la excluyen.

### Trampas
- Los slugs van en minúsculas y con guiones, sin tildes: `antioquia`, `bogota-dc`,
  `la-guajira`, `colombia` (país). Nunca adivines un slug: búscalo en `region`,
  `departamento` o `municipio` con `label ILIKE '%…%'`.
- Los municipios homónimos llevan sufijo: `sucre-suc`, `sucre-san`, `sucre-cau`.
  `boyaca` y `bogota-dc` son a la vez departamento y municipio: en `region_grupo` y
  `especie_region` se refieren al **departamento**, y en `geografia_resumen` se distinguen
  por `nivel`. Al hacer rankings de municipios con `region_grupo`/`especie_region`,
  excluye `slug_region = 'boyaca'`: su cifra es la del departamento y saldría primera.
- `registros_continentales`/`_marinos`/`_salobres` cuentan los registros de especies
  marcadas como terrestres, marinas o salobres en la lista taxonómica: no suman el total.
- En `dwc_integrated` la columna `order` es palabra reservada: escríbela `"order"`.
- Tablas `tmp_*`: insumos con los que se calculan las vistas, no para consultar.
  `dwc_occurrence`/`dwc_verbatim` están vacías (staging). `table_registry` es metadato
  interno.
- Las capas `MGN_ADM_MPIO_2025` (municipios, `dpto_cnmbr`/`mpio_cnmbr` en MAYÚSCULAS),
  `RESGUARDOS`, `RESERVAS`, `NUCLEOS_DFYB`, `REGION_AMAZONIA`, `INVEMAR_MARITIME_REGIONS` y
  `NARINO_MARITIME_REGION` son polígonos en EPSG:4326 y van entre comillas dobles.
- Los parquet H3 (`departamentos_h8`, `dpto_cnmbr` en MAYÚSCULAS sin tildes) y estos slugs
  no se unen por nombre: para cruzar registros con datos H3 convierte las coordenadas con
  `h3_latlng_to_cell(decimallatitude, decimallongitude, 8)` en el tool `query`.
