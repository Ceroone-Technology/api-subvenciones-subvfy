# app-subvenciones-subvfy — API (contexto para Claude Code)

Backend en Python del proyecto `app-subvenciones-subvfy` (Subvfy, Grupo Bigtoone). Este archivo resume las decisiones ya tomadas para que una sesión de Claude Code arranque con el mismo contexto que se construyó en Cowork, sin tener que repetirlo.

## Qué es este proyecto

API REST para un buscador de subvenciones (BDNS) con Favoritos, Alertas, Autenticación y Análisis con IA. El frontend (Angular 19, `app-subvenciones-subvfy` en `D:\Trabajo\Web-Angular\`) ya consume la BDNS directamente; esta API añade la persistencia y las funcionalidades que requieren backend propio.

## Stack (decisión cerrada, no reabrir sin razón)

FastAPI + SQLAlchemy 2.0 (async) + Alembic + Pydantic v2, sobre PostgreSQL vía asyncpg. JWT (python-jose + passlib/bcrypt) para autenticación. APScheduler en proceso para el motor de Alertas en local — en producción (AWS Lambda) se reemplaza por EventBridge Scheduler + una Lambda separada, ver más abajo. Anthropic SDK (Python) para Análisis con IA y Asistente IA. pytest + httpx.AsyncClient para tests. Docker/docker-compose para desarrollo local.

**Por qué Python y no Node.js/TypeScript**: hubo una reunión paralela (Luis Huapaya) donde se acordó Node.js/TS para un track de trabajo distinto ("Subfy", con frontend React y practicantes). Para *esta* API se decidió mantener Python porque ya estaba construido y probado, y evita reescribir trabajo entregado. Si alguien pregunta por qué no coincide con esa reunión, esta es la razón.

## Arquitectura cloud (AWS, low-cost — decisión cerrada)

- **Cómputo**: la misma app FastAPI, envuelta con **Mangum** (adaptador ASGI→Lambda), detrás de **API Gateway HTTP API** (el tier más barato). Sin servidor encendido 24/7.
- **Base de datos**: **Railway PostgreSQL** (no RDS, no DynamoDB) — mismo `DATABASE_URL`/asyncpg sin cambios de código. Se descartó DynamoDB porque el modelo es relacional y normalizado (14 tablas, FKs) — migrarlo sería tirar el diseño ya hecho.
- **Motor de Alertas**: en Lambda no tiene sentido `APScheduler` en proceso (no hay estado entre invocaciones) — se reemplaza por una función Lambda propia disparada por **EventBridge Scheduler**.
- **`NullPool` en `app/database.py` es intencional**: no es solo para tests (evita el bug de asyncpg "attached to a different loop" entre tests), es la config correcta para Lambda, donde un pool de conexiones persistente no aporta nada porque el runtime puede cambiar de event loop entre invocaciones. No lo cambies a un pool con conexiones persistentes sin volver a evaluar esto.
- Esto se implementa recién en el Hito 7 (Despliegue) del backlog — no bloquea el desarrollo funcional actual.

## Diseño de base de datos

Ya diseñado y documentado: `../schema-subvfy.sql` (DDL de referencia) y `../erd-subvfy.mmd`/`.png` (diagrama), un nivel arriba en `D:\Trabajo\BigToOne\Subvenciones\`. 14 tablas: `rol`, `empresa`, `empresa_palabra_clave`, `usuario`, `convocatoria` (caché local de la BDNS, no es la fuente de verdad), `favorito`, `alerta`, `alerta_organo`, `alerta_region`, `alerta_ejecucion`, `alerta_ejecucion_convocatoria`, `analisis_ia`, `conversacion_asistente`, `mensaje_asistente`.

Multi-tenant por `empresa` (Subvfy es B2B): cada `usuario` pertenece a una `empresa`, y el Análisis con IA compara convocatorias contra el perfil de la empresa.

Auditoría uniforme: todas las tablas tienen `created_at`/`updated_at`/`created_by`/`updated_by`, vía el mixin `AuditMixin` en `app/models/base.py`. `created_by`/`updated_by` apuntan a `usuario.id`.

**Gotcha ya resuelto, no lo reintroduzcas**: `rol`, `empresa` y `usuario` tienen una dependencia circular de auditoría (se referencian entre sí). Alembic autogenerate mete el `ForeignKeyConstraint(..., use_alter=True)` dentro de `op.create_table()`, pero eso **no emite el `ALTER TABLE`** — la FK queda invisible en la base de datos real aunque aparece en el archivo de migración. La forma correcta: sacarlas de `create_table()` y añadirlas con `op.create_foreign_key()` al final de `upgrade()` (con su `op.drop_constraint()` al inicio de `downgrade()`). Ya está así en `alembic/versions/827c98b6a656_esquema_inicial_subvfy.py` — revisa ese patrón antes de generar una migración nueva que toque estas tres tablas.

**Gotcha de la misma familia, y tampoco lo reintroduzcas**: **Alembic no compara constraints CHECK**. Cambiar la tupla de valores válidos de un modelo (añadir un estado a `ESTADOS_ENVIO`, por ejemplo) no aparece en `alembic revision --autogenerate`: la migración sale vacía de ese cambio y el CHECK real sigue rechazando el valor nuevo en cuanto alguien lo inserta. Hay que escribirlo **a mano**, con `op.drop_constraint(..., type_="check")` + `op.create_check_constraint(...)` en `upgrade()`, y lo simétrico en `downgrade()`. Y en el `downgrade` hay que **recolocar antes las filas que usen el valor que se va**, o el CHECK antiguo no se puede crear: ver `d8c5e0cf33c8`, que pasa las ejecuciones en `pendiente_envio` a `enviado` antes de restaurarlo. La regla general: de una migración autogenerada, revisa siempre lo que Alembic **no** mira — CHECKs y estas FKs.

## Backlog: Hito → Funcionalidad → Tarea

El desarrollo completo está desglosado en `D:\Trabajo\BigToOne\Subvenciones\api-hitos-funcionalidades-tareas-subvfy.docx` (103h de las 166h totales del proyecto ya aprobadas — el resto es frontend, en `hitos-tareas-subvfy.docx`, misma carpeta). Estado:

- **Hito 2, Funcionalidad 1 — Arquitectura y esqueleto**: hecho.
- **Hito 2, Funcionalidad 2 — Modelo de datos y migraciones**: hecho. Modelos en `app/models/`, migraciones en `alembic/versions/`, seed de roles aplicado.
- **Hito 2, Funcionalidad 3 — API de empresa/rol/usuario**: hecho. Schemas en `app/schemas/`, routers en `app/api/routes/` (`roles.py`, `empresas.py`, `usuarios.py`), hashing de contraseñas en `app/core/security.py`, 28 tests verdes contra Postgres real.
- **Hito 2, Funcionalidad 4 — Autenticación real (JWT)**: hecho. JWT en `app/core/security.py`, permisos en `app/core/permisos.py`, router en `app/api/routes/auth.py`, arranque en frío en `app/cli.py`, seed del usuario de sistema en `b7f3c21a9d40`. 70 tests verdes.
- **Hito 3, Funcionalidad 1 — Endpoints de favoritos**: hecho. Router en `app/api/routes/favoritos.py`, schemas de favorito y convocatoria. 85 tests verdes.
- **Hito 4, Funcionalidad 1 — CRUD de alertas (10 h)**: hecho (mutaciones). Router en `app/api/routes/alertas.py`, lógica en `app/services/alertas.py`, normalización de filtros en `app/services/filtros.py`, índices en `e4a19c7d2b58`. 119 tests verdes (34 nuevos), ruff y mypy limpios.
- **Hito 4 — Listado y detalle de alertas**: hecho. `GET /alertas` (paginado con `PaginacionDep`/`Pagina[T]`, filtros `organo_id`/`region_id`/`activa`) y `GET /alertas/{id}` en el mismo router y servicio; los 404 están declarados en el OpenAPI del detalle, el PATCH y el DELETE. 142 tests verdes (23 nuevos en `tests/test_alertas_listado.py`), ruff y mypy limpios.
- **Hito 4 — Historial de ejecuciones por alerta**: hecho, **solo lectura**. `GET /alertas/{id}/ejecuciones` (paginado, filtro `estado_envio`) y `GET /alertas/{id}/ejecuciones/{ejecucion_id}` (con las convocatorias detectadas), en el router de alertas y en `app/services/alerta_ejecuciones.py`. Sin modelo nuevo ni migración: `AlertaEjecucion` y `AlertaEjecucionConvocatoria` ya estaban en `827c98b6a656`. 159 tests verdes (17 nuevos), ruff y mypy limpios.
- **Hito 4 — Motor de ejecución de alertas, tarea 1 de 3 (job programado)**: hecho. Servicio en `app/services/motor_alertas.py` (selección por frecuencia + ciclo con aislamiento de fallos), disparador en `app/core/scheduler.py` (APScheduler 3.x, `AsyncIOScheduler`) enganchado al `lifespan` de `app/main.py`, e identidad de los procesos automáticos en `app/services/sistema.py`. 185 tests verdes (15 nuevos), ruff y mypy limpios. Con la tarea 3, el historial ya se escribe.
- **Hito 4 — Motor de ejecución de alertas, tarea 2 de 3 (consulta a la BDNS)**: hecho. Constructor puro en `app/services/bdns_consulta.py` y cliente HTTP en `app/services/bdns_cliente.py`, enganchados en `evaluar_alerta`. 219 tests verdes (34 nuevos), ruff y mypy limpios.
- **Hito 4 — Ejecuciones fallidas de alerta visibles y con reintentos espaciados**: hecho. `registrar_fallo` en `app/services/deduplicacion_alertas.py`, columnas `alerta.fallos_consecutivos` y `alerta.proximo_reintento_at` (migración `a3f9d27c51b8`, escrita a mano), selección y reinicio en `app/services/motor_alertas.py`, reinicio en el `PATCH` de `app/services/alertas.py`. 255 tests verdes (11 nuevos), ruff y mypy limpios. Detalle en la sección del motor.
- **Hito 4 — Motor de ejecución de alertas, tarea 3 de 3 (deduplicación y registro)**: hecho. Servicio en `app/services/deduplicacion_alertas.py`, migración `d8c5e0cf33c8` (`alerta_ejecucion_convocatoria.alerta_id` + `UNIQUE(alerta_id, convocatoria_id)`, y `estado_envio` admite `pendiente_envio`). **Queda pendiente el envío del aviso**, que es otra funcionalidad: las ejecuciones con novedades se quedan en `pendiente_envio`. 229 tests verdes (10 nuevos), ruff y mypy limpios.
- **Hito 4 — Cobertura de integración del CRUD de alertas**: hecho, solo tests (`tests/test_alertas.py`, 11 nuevos; 170 verdes con el historial ya integrado). Cierran cuatro huecos sobre reglas ya documentadas: el DELETE arrastra `alerta_ejecucion` y su tabla intermedia pero conserva la convocatoria cacheada; los límites de campo (`nombre`, `texto_busqueda`, `nivel_administracion`, `canal_notificacion`, `MAX_FILTROS`) validados entrando por HTTP; el rol `usuario` gestiona sus propias alertas (los endpoints no exigen gestor) y no alcanza las de un compañero; y `usuario_id`/`id` en el body no reasignan el propietario ni al crear ni al editar.
- **Hito 5, Funcionalidad 1 — Análisis por convocatoria, tarea 1 de 4 (integración del Anthropic SDK)**: hecho. Cliente en `app/services/ia_cliente.py` (`ClienteIA`, errores `IANoConfigurada`/`IANoDisponible`/`IARespuestaInvalida`, `calcular_coste`, dependencia `ClienteIADep` en `app/api/deps.py`), configuración en `app/config.py`. 286 tests verdes (31 nuevos en `tests/test_ia_cliente.py`, con MockTransport), ruff y mypy limpios. **Sin probar contra Anthropic real**: no hay clave. Detalle en la sección "Análisis con IA".
- **Hito 5, Funcionalidad 1 — Análisis por convocatoria, tarea 2 de 4 (prompt engineering)**: hecho. Detalle de la convocatoria en la BDNS (`ClienteBdns.obtener_detalle`, adelantado de H5.3), respuesta estructurada en el cliente (`ClienteIA.generar_estructurado`), datos de entrada en `app/services/ia_entrada.py`, formatos en `app/schemas/analisis_ia.py`, prompts y versiones en `app/services/ia_prompts.py`, servicio en `app/services/analisis_ia.py` y comando `probar-analisis` en `app/cli.py`. 434 tests verdes, ruff y mypy limpios. **Sin probar contra Anthropic real**: no hay clave. Sin endpoints ni guardado (H5.3) ni score (H5.4). Detalle en "Análisis con IA → Prompts y análisis".
- Hito 4 (Alertas, 22 h), Hito 5 (Análisis con IA; resto de tareas), Hito 6 (Ficha ampliada, sin cambios de backend), Hito 7 (Cierre): pendientes, ver el .docx para el desglose de tareas y horas de cada uno.

## Flujo de ramas (decisión cerrada, desde el 22/09/2026)

**No se sube a `main`.** La rama de integración es **`develop`**.

- Todo el trabajo se integra en `develop`: las ramas salen de `develop` y el PR va **contra `develop`**.
- `main` está protegida: solo recibe PRs **desde `develop`**, y solo con el CI en verde. No acepta push directo.
- **CI en cada PR** (`.github/workflows/ci.yml`, GitHub Actions): `alembic upgrade head`, `ruff check .`, `mypy app` y `pytest -q` contra un PostgreSQL 16 real. Se lanza en los PRs y en los push a `develop`/`main`. Si falla algo, el PR no se puede mergear.

```bash
git fetch origin
git checkout develop
git pull
git checkout -b feature/lo-que-toque
# ... trabajo, commits ...
git push -u origin feature/lo-que-toque
# abrir el PR contra develop, no contra main
```

Reglas para Claude Code en este repo:
- Nunca hagas push a `main`, ni directo ni con un PR desde una rama de feature. Al crear un PR, `--base develop` siempre de forma explícita: el default del repo en GitHub sigue siendo `main`.
- Antes de empezar una tarea, crea la rama desde `origin/develop` actualizado, no desde `main`.
- Si hay que rebasar una rama ya publicada, pregunta primero y usa `git push --force-with-lease`, nunca `--force` a secas.
- Antes de abrir el PR, corre en local lo mismo que el CI (ver la sección siguiente). Así no descubres en el PR lo que podías ver antes.

**Ojo con la versión de Python**: el CI usa **Python 3.11**, mientras que el `Dockerfile` usa 3.12 y `pyproject.toml` apunta a `py312`. Que algo pase en local no garantiza que pase en el CI. No uses sintaxis exclusiva de 3.12 (p. ej. `type X = ...` o genéricos PEP 695), aunque ruff la sugiera.

## Cómo correr y verificar

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api pytest
docker compose exec api ruff check .
docker compose exec api mypy app
```

La imagen de `docker-compose.yml` es de desarrollo: instala `requirements-dev.txt` y copia/monta `tests/`, por eso `pytest` corre dentro del contenedor (no hay intérprete con dependencias en el host). El empaquetado de producción (Lambda + Mangum) es del Hito 7 y será distinto.

`GET /roles` con un token válido debe responder 200 con los tres roles sembrados: confirma app + config + conexión a BD + migraciones aplicadas. Antes de dar por cerrada cualquier tarea nueva, correr `pytest` contra una base real (no mocks) — así se detectó el bug de las FKs circulares —, además de `ruff check .` y `mypy app`, que deben salir limpios.

**Tooling (desde Hito 4)**: ruff y mypy se configuran en `pyproject.toml`, que solo contiene configuración de herramientas; las dependencias siguen en `requirements*.txt`. `line-length = 120`, porque es el ancho que ya tenía el código. B008 se permite para `fastapi.Query`/`Depends`, que en la firma es el uso idiomático. Cada `Literal[TUPLA]` lleva un `# type: ignore[valid-type]` puntual: no desactives esa regla globalmente.

**Gotcha de entorno — la imagen de desarrollo puede estar vieja**: `pyproject.toml` se copia en el build (y se monta como volumen), y ruff y mypy se instalan desde `requirements-dev.txt`. Si la imagen es anterior a esos cambios, dentro del contenedor no hay `pyproject.toml` ni ruff/mypy, y instalarlos a mano con `pip` **hace que corran sin la configuración del proyecto**: ruff pasa con las reglas por defecto y mypy da errores de stubs que no existen con la config real. Si `ruff` no aparece en el contenedor, o `ls /app` no muestra `pyproject.toml`, reconstruye con `docker compose up -d --build` (el volumen de la base de datos no se toca) en vez de instalar nada a mano.

**Gotcha de entorno (Windows con Avast)**: el escudo web de Avast intercepta el HTTPS y el `pip install` del build falla con `CERTIFICATE_VERIFY_FAILED`, porque el contenedor no confía en el certificado raíz de Avast. Se resuelve excluyendo `pypi.org` y `files.pythonhosted.org` en Avast, no tocando el `Dockerfile` ni usando `--trusted-host`.

No hay endpoints `/health`: existieron como validación del scaffolding en el Hito 2 Funcionalidad 1 y se retiraron una vez que hubo endpoints de negocio. Si en el Hito 7 hace falta un check de liveness para API Gateway/Lambda, se vuelve a añadir entonces con ese propósito explícito.

## Convenciones de código ya establecidas

- Modelos: SQLAlchemy 2.0 estilo `Mapped[...]`/`mapped_column`, un archivo por tabla en `app/models/`, todas heredan `Base, AuditMixin`.
- `type_annotation_map = {int: BigInteger}` en `Base` (`app/database.py`): `Mapped[int]` ya mapea a BIGINT por defecto — no repetir `BigInteger` salvo que la columna real sea `Integer` (como `alerta_organo.organo_bdns_id`, que en el SQL original es `int`, no `bigint`).
- Constraints CHECK con tuplas de valores válidos como constantes a nivel de módulo (ver `ESTADOS_USUARIO`, `TIPOS_ANALISIS`, etc.) — para reusarlas en schemas Pydantic más adelante en vez de repetir los strings.
- Índices parciales y GIN ya replicados fielmente del SQL original (`ix_alerta_activa`, `ix_analisis_ia_resultado`) vía `postgresql_where`/`postgresql_using` en `Index(...)`.

## Convenciones de la API (desde Hito 2, Funcionalidad 3)

- Un router por recurso en `app/api/routes/`, con `prefix`/`tags` propios, registrado en `app/main.py`. Rutas planas sin prefijo de versión, igual que `/health` — si algún día hace falta `/api/v1`, se añade con `root_path` o un prefijo en el `include_router`, no reescribiendo los routers.
- `app/api/deps.py` centraliza las dependencias compartidas (`DbDep`, `PaginacionDep`) como alias `Annotated`, para no repetir `Depends(...)` en cada firma. Ahí van también `UsuarioActualDep` y la autorización por rol cuando existan.
- Listados paginados con `Pagina[T]` (`app/schemas/common.py`): `{items, total, page, size}`, `total` sobre el filtro completo.
- Los valores válidos de los CHECK se reusan en Pydantic con `Literal[TUPLA]` importando la tupla del modelo (`Literal[TAMANOS_VALIDOS]`). Funciona en runtime porque `Literal[tupla]` se expande a sus elementos, y además genera un `enum` real en el OpenAPI que consume el frontend. Un type checker estático se queja; es el precio de no duplicar los strings.
- `DELETE` sobre empresa/usuario es baja lógica (`estado`), no borrado físico: ambos aparecen en `created_by`/`updated_by` de todo el esquema.
- Los valores únicos (NIF, email) se comprueban con un SELECT previo para devolver un 409 con mensaje útil, y las FK con un `db.get()` previo para devolver 400 en vez de dejar escapar un `ForeignKeyViolationError`.

**Gotcha ya resuelto, no lo reintroduzcas**: `requirements.txt` fija `bcrypt==4.2.1`. `passlib[bcrypt]==1.7.4` no pone techo de versión, y con bcrypt >= 5.0 passlib no consigue cargar su backend (revienta en la detección con `ValueError: password cannot be longer than 72 bytes`), así que un build limpio sin ese pin deja el hashing de contraseñas inservible.

**Gotcha de tests**: los emails de prueba no pueden usar el TLD `.test` (ni `.invalid`/`.localhost`) — `email-validator`, que es lo que hay detrás de `EmailStr`, los rechaza como direcciones no válidas. Se usa `@test.subvfy.example.com`.

## Autenticación y permisos (desde Hito 2, Funcionalidad 4)

Decisiones cerradas en esta funcionalidad, con el usuario, no a criterio propio — no las reabras sin preguntar:

- **Sesión stateless**: access token (60 min) + refresh token (30 días), ambos JWT. **No hay tabla de sesiones ni denylist**, y se descartó añadirla: obligaría a consultar la BD en cada petición autenticada. Consecuencia asumida: `POST /auth/logout` no invalida nada en el servidor, es del lado del cliente. Hay un test (`test_logout_no_invalida_el_token`) que documenta justo eso; si algún día se añade denylist, ese test debe cambiar de expectativa.
- **Sin registro público**: las altas las hace un admin. No añadas `POST /auth/register`.
- **Matriz de permisos**: admin global; gestor lee y escribe solo en su empresa; usuario lee su empresa y edita solo su propio perfil (y no puede tocarse `empresa_id`/`rol_id`/`estado`, que es la escalada de privilegios más barata). Está en la tabla de `app/core/permisos.py`.
- **El token solo lleva `sub` (id de usuario) y `tipo`**. Nada de rol ni empresa dentro: la dependencia `usuario_actual` relee usuario y rol de la BD en cada petición, y por eso bloquear una cuenta o cambiarle el rol tiene efecto inmediato. No metas el rol en el token "para ahorrar una consulta" sin asumir que se vuelve obsoleto.
- **Aislamiento multi-tenant**: los listados **fuerzan** el filtro a `actual.filtro_empresa` en vez de validar el `empresa_id` recibido. Es defensa en profundidad: así, olvidarse de una comprobación al añadir un endpoint no se traduce en una fuga entre clientes. Cualquier recurso nuevo con `empresa_id` (favoritos, alertas, análisis) debe seguir el mismo patrón.
- **404 en vez de 403 para recursos de otro tenant**: un 403 confirmaría que ese id existe. Para roles insuficientes sobre un recurso propio sí se devuelve 403.

**Arranque en frío**: crear un usuario exige token y obtener token exige usuario. Se resuelve con `docker compose exec api python -m app.cli crear-admin`. El `sistema@subvfy.es` de la migración `b7f3c21a9d40` no sirve para entrar (nace `bloqueado`, contraseña aleatoria): es solo la identidad de auditoría de los procesos automáticos.

**Gotcha de tests**: los endpoints exigen autenticación, así que los usuarios de prueba se crean **directamente en la BD** (`crear_sesion_en_bd` en `tests/conftest.py`), no por la API — el mismo arranque en frío. Hay un cliente por rol (`client_admin`, `client_gestor`, `client_usuario`); `client` a secas es el no autenticado y se reserva para los 401. El `admin` de las fixtures vive en **su propia empresa**, para que ver datos de la empresa cliente demuestre que es el rol quien se lo permite y no que compartan tenant.

**Gotcha de limpieza**: el teardown de tests tiene que poner a NULL `created_by`/`updated_by` antes de borrar, porque ahora las filas sí se firman y los DELETE chocan contra esas FKs de auditoría.

## La caché de convocatorias (desde Hito 3)

`convocatoria` **no es la fuente de verdad** — lo es la BDNS, que el frontend consulta directamente. La tabla existe para que favoritos, alertas y análisis IA tengan a qué apuntar con una FK y no se pierda el histórico si la API externa cambia.

De ahí el patrón que estrena `app/api/routes/favoritos.py` y que deben reutilizar alertas y análisis: el endpoint recibe la ficha de la BDNS dentro del body (`ConvocatoriaUpsert`) y hace `INSERT ... ON CONFLICT (codigo_bdns) DO UPDATE` antes de crear la fila que la referencia. Dos detalles que no son accidentales:

- El `set_` del upsert solo incluye los campos **enviados y no nulos** (`exclude_unset` + descarte de `None`). Marcar un favorito desde el listado de resultados manda una ficha incompleta, y sin este filtro se machacarían con NULL los datos de una sincronización anterior. Hay un test que lo fija: `test_un_guardado_parcial_no_borra_datos_ya_cacheados`.
- Al quitar un favorito **no se borra la convocatoria**: es compartida.

Los recursos que cuelgan del usuario (favoritos, alertas) se filtran por `actual.id`, no por `filtro_empresa`. Los que cuelguen de la empresa (análisis) siguen el patrón multi-tenant de `app/core/permisos.py`. No los mezcles: son dos alcances distintos.

## Alertas (desde Hito 4, Funcionalidad 1)

Decisiones cerradas con el usuario:

- **Personales**, como favoritos: `alerta.usuario_id` es el propietario y solo él edita o borra. Cualquier otro, admin incluido, recibe 404.
- **Órganos y regiones son ids del catálogo BDNS** (enteros), no texto: el frontend los tiene porque consulta la BDNS. No hay catálogo propio ni normalización de texto. Si algún día hiciera falta, se expondría un `GET /catalogos/...`, que está propuesto pero no aprobado. "Normalización" aquí significa filas hijas sin duplicados (`normalizar_ids_bdns`, aplicada en los schemas).
- En `PATCH`, las listas de filtros **reemplazan** a las anteriores. `DELETE` es **borrado físico** (se lleva el histórico de `alerta_ejecucion`); para pausar está `activa`.
- **Capa de servicios**: las alertas estrenan `app/services/`. El router valida y traduce a HTTP, y el servicio hace el trabajo con excepciones de dominio (`AlertaNoEncontrada`, `RangoFechasInvalido`). Los routers anteriores no se han migrado a este patrón.

- **Listado sin N+1**: `AlertaRead` se monta siempre con `_con_filtros(db, alertas)`, que resuelve los filtros de una página entera con **dos consultas `IN (ids)`**. El modelo `Alerta` no tiene `relationship()` a propósito, para que no haya lazy-loads en async. Una petición de listado hace un número fijo de consultas (auth + count + página + órganos + regiones), y `test_sin_n_mas_1_con_200_alertas` lo fija contando las sentencias SQL con `event.listen(engine.sync_engine, "before_cursor_execute", ...)`. No consultes las tablas hijas alerta por alerta.
- **Orden del listado**: `created_at DESC, id DESC`. El `id` desempata las alertas creadas en la misma transacción, que comparten `created_at`; sin él, la paginación puede repetir o saltarse filas.

- **Historial de ejecuciones**: cuelga de la alerta (`/alertas/{id}/ejecuciones`), no es un recurso aparte, porque una ejecución no existe sin su alerta y así la propiedad se comprueba una sola vez. El listado devuelve solo el resumen y el detalle añade las convocatorias; se ordena por `fecha_ejecucion_at DESC, id DESC`, que es lo que indexa `ix_alerta_ejecucion_alerta`. El detalle exige `alerta_id` **y** `ejecucion_id` en el mismo WHERE: pedir una ejecución desde otra alerta, aunque sea del mismo usuario, da 404.

**Gotcha de teardown (ejecuciones)**: `alerta_ejecucion` y `alerta_ejecucion_convocatoria` **no** necesitan borrado explícito en `tests/conftest.py`. Caen por cascada al borrar las alertas de prueba, que ya se borran antes que los usuarios y que las convocatorias, así que ni la FK de auditoría ni `convocatoria_id` (que no tiene cascada) llegan a chocar. Si añades una tabla que referencie `convocatoria` y no cuelgue de `alerta`, sí tendrás que borrarla a mano antes que las convocatorias.

**Gotcha**: si en un PATCH solo cambian filas hijas, la fila de `alerta` no queda sucia y el `onupdate` de `updated_at` no salta. Por eso el servicio lanza un `UPDATE` explícito de `updated_at`/`updated_by`, y `_leer` usa `populate_existing` para no devolver el valor viejo del identity map.

**ruff y mypy** están en `requirements-dev.txt`, configurados en `pyproject.toml`: `docker compose exec api ruff check .` y `docker compose exec api mypy app`.

**Gotcha de tests**: el teardown de `tests/conftest.py` tiene que poner a NULL `created_by`/`updated_by` de las convocatorias de prueba **antes** de borrar los usuarios, porque esas columnas apuntan a `usuario.id`. Si añades una tabla nueva que los procesos firmen, va en ese mismo bloque y en ese mismo orden.

**Gotcha de tests (alertas)**: las alertas de prueba se borran **explícitamente antes** que los usuarios, no por la cascada. `alerta_organo`/`alerta_region` quedan a dos niveles (usuario → alerta → hija), y Postgres comprueba su FK de auditoría `created_by → usuario` antes de que la cascada llegue a borrarlas. Sin ese paso, el primer teardown falla y deja restos que tumban la limpieza de todos los tests siguientes (fueron 111 errores). Aplica a cualquier tabla nueva firmada que quede a más de un nivel de cascada del usuario. En producción no pasa, porque los usuarios solo se dan de baja lógica.

## Motor de ejecución de alertas (desde Hito 4, tarea 1 de 3)

Decisiones cerradas con el usuario:

- **Quién toca se decide con `alerta.ultima_ejecucion_at`**, contra el intervalo de su frecuencia, en **una sola consulta** (`alertas_pendientes`). No se deriva de `alerta_ejecucion` (sería un MAX por alerta) ni se añadió columna de próxima ejecución por frecuencia (`proximo_reintento_at` existe, pero solo para la espera tras un fallo). Orden de servicio: las nunca ejecutadas primero y luego las más atrasadas.
- **`frecuencia = "inmediata"` es intervalo cero**: toca en cada ciclo, así que su cadencia real es el intervalo del scheduler (`SCHEDULER_INTERVALO_MINUTOS`, 15 por defecto).
- **Esta tarea solo marca `ultima_ejecucion_at`** (firmando `updated_by` con el usuario de sistema). Las filas de `alerta_ejecucion` son de la tarea 3: escribirlas aquí sería adelantarla. Sin esa marca, cada ciclo reprocesaría lo mismo.
- **`evaluar_alerta` es el punto de extensión** y hoy no hace nada. No lanza `NotImplementedError` a propósito: dejaría todas las alertas en error, y cada una se reintentaría (con su espera creciente) y llenaría el historial de filas `error`.
- **Aislamiento de fallos**: una alerta que revienta se registra con `logger.exception` (solo el `alerta_id`, nada del usuario), se hace `rollback` y se sigue. Su `ultima_ejecucion_at` no avanza, y se reintenta cuando acaba su espera (ver el punto siguiente). Hay un tope de `MAX_POR_CICLO` alertas por ciclo.
- **Un fallo deja rastro y no se reintenta a ciegas**: como el rollback se lleva la fila de `alerta_ejecucion`, `registrar_fallo` (`app/services/deduplicacion_alertas.py`) escribe el error **en una transacción nueva, después del rollback**: fila `alerta_ejecucion` con `estado_envio = 'error'`, `convocatorias_encontradas = 0` y `detalle_error`, más `alerta.fallos_consecutivos` y `alerta.proximo_reintento_at` (migración `a3f9d27c51b8`). `alertas_pendientes` salta las alertas con `proximo_reintento_at` futuro. Espera creciente `ALERTAS_REINTENTO_BASE_MINUTOS × 2^(fallos-1)` (15 min, 30, 60… por defecto) con tope `ALERTAS_REINTENTO_MAX_HORAS` (24). Un éxito (`_marcar_ejecutada`) o un `PATCH` de la alerta ponen contador y espera a cero. Reglas que no son accidentales:
  - **Un fallo nunca avanza `ultima_ejecucion_at`**: es el `desde` de la consulta a la BDNS, y avanzarlo perdería lo publicado entre el fallo y el reintento. Por eso la espera tiene sus propias columnas.
  - **`detalle_error` no es `str(exc)`**: en un `DBAPIError` de SQLAlchemy incluye el SQL y los parámetros (datos de la BDNS), y el campo sale por `GET /alertas/{id}/ejecuciones`. `detalle_de_error` usa clase + primera línea del mensaje del driver, truncado a 500, con prefijo `[ejecución]`. El estado `error` lo reutilizará el futuro envío del aviso, y el prefijo distingue el origen.
  - **Si registrar el fallo también falla, se loguea y el ciclo sigue** (`_registrar_fallo` en `motor_alertas.py`).
  - No se desactiva la alerta (`activa`) automáticamente: es una decisión del usuario, y el error ya es visible en el historial.
- **El servicio no sabe de APScheduler ni de FastAPI**: recibe la sesión de fuera, así que vale igual para un test, una CLI o la Lambda del Hito 7. `app/core/scheduler.py` es solo fontanería y no se despliega en producción.
- **Desactivado por defecto** (`SCHEDULER_HABILITADO=false`): en tests y en Lambda nada debe arrancar por su cuenta. Además, `httpx`+`ASGITransport` no dispara el `lifespan`, así que la suite no lo levanta ni por accidente.
- **Multi-worker, asumido y documentado**: con varios workers de uvicorn cada proceso arrancaría su scheduler y las alertas se evaluarían por duplicado. Hoy se corre con un worker y en Lambda + EventBridge el disparo es externo, así que se deja para el Hito 7. Si hiciera falta antes, la solución barata es envolver el ciclo en un `pg_try_advisory_lock`.

**Gotcha de SQLAlchemy async — atributos expirados y `MissingGreenlet`**: en async, leer un atributo expirado es una consulta, y si se hace fuera del contexto que SQLAlchemy prepara, revienta con `MissingGreenlet`. Hay dos formas de expirar atributos sin darse cuenta, y el motor tropieza con las dos si se le quitan estas dos líneas:

- **Un `UPDATE` con ORM expira los objetos que toca.** `synchronize_session` vale `"auto"` por defecto, así que la sentencia marca como expirados los atributos de las instancias ya cargadas; el siguiente `alerta.id` del bucle intenta recargar y falla. Cuando **no** se van a releer esos objetos desde la sesión, el `UPDATE` lleva `execution_options(synchronize_session=False)` (ver `_marcar_ejecutada`). Si algún día hay que releerlos, el camino es `populate_existing` en el `SELECT` posterior, no dejar que la sentencia expire medio bucle.
- **Un `rollback()` expira la sesión entera, y eso no se desactiva por sentencia.** Por eso `alertas_pendientes` hace `db.expunge(alerta)` de cada fila antes de devolverlas: las instancias que tienen que **sobrevivir a un `rollback`** —el del aislamiento de fallos, que se ejecuta entre iteraciones— no pueden seguir atadas a la sesión. Desprenderlas es seguro aquí porque el `SELECT` carga todas las columnas y **`Alerta` no tiene `relationship()` ni columnas diferidas**; si alguien añade una relación o un `deferred`, esto deja de valer y hay que cargarlo explícitamente antes del `expunge`. Como efecto lateral bueno, sobre una instancia desprendida un atributo no cargado lanza `DetachedInstanceError` en el acto, en vez de intentar una consulta desde el sitio equivocado.

**Gotcha de configuración de sesión — `expire_on_commit=False` en `app/database.py` no es decorativo**: hay código que **lee atributos después del `commit`** y depende de ello. Si alguien lo pone en `True`:

- `registrar_ejecucion` (`app/services/deduplicacion_alertas.py`) devuelve el `AlertaEjecucion` que acaba de commitear, y quien lo llama lee `convocatorias_encontradas` y `estado_envio`. Con expiración al commit, esas dos lecturas pasan a ser recargas perezosas: `MissingGreenlet`.
- Ojo, **cubre el `commit` pero no el `rollback`**: un `rollback` expira igual, con `False` o con `True`. De ahí el `expunge` del punto anterior; no busques protección en esta opción.
- El otro lado de la misma moneda: `_leer` (`app/services/alertas.py`) usa `execution_options(populate_existing=True)` justo **porque** no hay expiración al commit, para no devolver el `updated_at` viejo que sigue en el identity map. Quien cambie esta opción tiene que revisar esos tres sitios a la vez.

**Gotcha de APScheduler 3.x**: su `shutdown` va decorado con `run_in_event_loop`, así que **no apaga en el momento**, sino en la siguiente vuelta del loop. Por eso `parar_scheduler` es `async` y cede el control (`await asyncio.sleep(0)`) antes de devolver; si no, el apagado se queda a medias y `scheduler.running` sigue en `True`.

**Gotcha de logs**: uvicorn solo configura sus propios loggers, así que sin `logging.basicConfig` los `INFO` de `app.*` no aparecen en `docker compose logs -f api`. Se configura una vez en el `lifespan`, con `LOG_LEVEL`.

**Identidad de los procesos automáticos**: `app/services/sistema.py` resuelve el id de `sistema@subvfy.es` (seed de `b7f3c21a9d40`) y lo cachea por proceso. Sigue siendo una identidad de auditoría, no una cuenta de acceso.

### La consulta a la BDNS (tarea 2 de 3)

**Parámetros verificados contra la API real** (`GET {BDNS_BASE_URL}/convocatorias/busqueda`), no sacados de memoria. Si algún día cambian, se comprueban otra vez antes de tocar el código:

| Nuestro criterio | Parámetro BDNS |
|---|---|
| `texto_busqueda` | `descripcion` |
| `nivel_administracion` | `tipoAdministracion`: `estado`→`C`, `ccaa`→`A`, `local`→`L`, `otros`→`O` |
| `organos` / `regiones` | `organos` / `regiones`, ids enteros **repetibles**: la API los acumula |
| `fecha_desde` / `fecha_hasta` | `fechaDesde` / `fechaHasta` en **`dd/mm/yyyy`** (la respuesta viene en `yyyy-mm-dd`) |
| `solo_mrr` | **no tiene parámetro**: se filtra en cliente con el campo `mrr` de cada fila |

- **Una sola consulta por alerta**: al acumular varios `organos`/`regiones`, no hay producto cartesiano ni filtrado en cliente por esos criterios. Si algún día la BDNS deja de acumularlos, habrá que volver a plantearlo.
- **El mapeo vive en un único sitio**, `NIVEL_A_TIPO_ADMINISTRACION` en `app/services/bdns_consulta.py`. No repartas equivalencias por el código.
- **`construir_consulta` es pura**: recibe la alerta, sus filtros y la fecha `desde`, y no toca BD ni red. Por eso sus tests no necesitan Postgres.
- **La fecha `desde` llega como parámetro**, no se calcula en el constructor. Hoy la pone `evaluar_alerta`: `ultima_ejecucion_at`, o una ventana de `BDNS_DIAS_PRIMERA_EJECUCION` días la primera vez. Si el usuario puso su propia `fecha_desde`, **gana la más restrictiva**: lo incremental no reabre un rango que él había cerrado.
- **Criterios que no dan consulta** → `CriteriosAlertaInvalidos`: una alerta sin ningún criterio (traería la BDNS entera) o con un rango ya pasado. `solo_mrr` no cuenta como criterio. El ciclo lo registra y sigue con las demás.
- **El cliente no reintenta** y no toca la base de datos. Los errores HTTP, de red y de timeout salen como `BdnsNoDisponible` (con `status` si lo hay). Recorre páginas hasta la última o hasta `BDNS_MAX_PAGINAS`, ordenando por `fechaRecepcion desc` para que, si se corta, se corte por lo más viejo.
- **`httpx` es dependencia de runtime** desde esta tarea (antes solo de tests). Los tests del cliente usan `httpx.MockTransport`: mockear el servicio externo es legítimo, la base de datos sigue siendo real en el resto de la suite.

**Gotcha de entorno (Avast, otra vez)**: Avast también intercepta `infosubvenciones.es`, así que desde el contenedor la consulta real falla con `CERTIFICATE_VERIFY_FAILED` y `BdnsNoDisponible`. Se arregla añadiendo `infosubvenciones.es` a las excepciones del Escudo web, igual que se hizo con `pypi.org`. **No se desactiva la verificación TLS** ni se mete un `verify=False` para salir del paso.

### Deduplicación y registro (tarea 3 de 3)

- **El alcance es la alerta, no el usuario**: las alertas son personales e independientes, así que la misma convocatoria puede ser novedad para dos alertas, incluso de la misma persona. `filtrar_nuevas` filtra por `alerta_id`.
- **"Ya notificada" = existe su fila en `alerta_ejecucion_convocatoria`** para esa alerta. No hay columna `notificada`: el registro es la marca.
- **La garantía la da Postgres, no el código**: la migración `d8c5e0cf33c8` añade `alerta_ejecucion_convocatoria.alerta_id` (desnormalizado a propósito) con `UNIQUE(alerta_id, convocatoria_id)`, y el registro usa `ON CONFLICT DO NOTHING ... RETURNING`, así que **lo devuelto es exactamente lo insertado**. Dos ciclos simultáneos no pueden contar la misma novedad dos veces, y `convocatorias_encontradas` guarda lo realmente registrado, no el tamaño del lote.
- **Los dos UNIQUE de la tabla puente no son un despiste.** Como *constraint*, `uq_alerta_ejecucion_convocatoria (alerta_ejecucion_id, convocatoria_id)` es redundante frente a `uq_alerta_convocatoria_notificada (alerta_id, convocatoria_id)`: todas las filas de una ejecución comparten alerta, así que el segundo subsume al primero. No lo borres, por dos razones. Una, la redundancia **depende de que `alerta_id` coincida con `alerta_ejecucion.alerta_id`, y el esquema no lo obliga**: si algún código escribiera un `alerta_id` equivocado, el viejo sería la única defensa dentro de la ejecución (ver la mejora propuesta más abajo). Dos, como **índice** es el único que hay por `alerta_ejecucion_id`, y por ahí filtra el detalle del historial y busca Postgres los hijos al borrar una ejecución en cascada.
- **Una transacción y un solo commit** en `registrar_ejecucion`: ejecución + caché + filas puente. Si algo falla a mitad, no queda nada marcado como visto; lo contrario perdería el resultado para siempre, porque en el ciclo siguiente ya no sería nuevo. El marcado de `ultima_ejecucion_at` va después, en su propio commit: si fallara ahí, el ciclo siguiente repite la consulta y la dedup devuelve cero novedades.
- **Se deduplica también dentro del lote** (`codigo_bdns`): la BDNS puede repetir un código entre páginas y el INSERT chocaría consigo mismo.
- **`estado_envio = pendiente_envio`** cuando hay novedades: el envío del aviso es otra funcionalidad y decir `enviado` aquí sería falso. Sin novedades, `sin_novedades`. El `Literal` del OpenAPI se actualiza solo, porque importa la tupla del modelo.
- **Upsert masivo con la regla de favoritos**: `COALESCE(excluded.campo, convocatoria.campo)` es la versión por lotes de "un guardado parcial no borra lo ya cacheado", así que la ficha completa que guardó un favorito no se pierde si la BDNS devuelve campos vacíos. El `nivel1` de la BDNS se traduce con `NIVEL_BDNS_A_NUESTRO`, derivado del mapa de `bdns_consulta.py` para no mantener dos tablas de equivalencias.
- **Nada de respaldos en Python (`x or y`) en el upsert masivo.** Sustituir un `None` antes del INSERT deja `excluded.campo` con valor, el `COALESCE` se vuelve inútil y el respaldo machaca lo cacheado. Pasó con dos campos y se corrigió en el PR #6: `titulo or "(sin título)"` borraba el título que había guardado un favorito, y `nivel3 or nivel2` sustituía un órgano específico ya cacheado por la administración. Si añades un campo al upsert, o va sin respaldo, o queda fuera del `COALESCE` a sabiendas. La excepción consciente es `financiada_mrr`, que se sobrescribe siempre: la BDNS manda `mrr` en todas las filas y para un booleano vale lo último que diga la fuente.
- **Una convocatoria sin `codigo_bdns` o sin título se descarta en `filtrar_nuevas`**, con `logger.warning` y antes de comparar con lo ya notificado, para que no quede marcada como vista y vuelva a entrar si la BDNS la publica completa. No es una precaución opcional: **Postgres comprueba el NOT NULL de la fila propuesta antes de resolver el `ON CONFLICT`**, así que un título nulo revienta el INSERT aunque la fila ya exista y el `COALESCE` fuera a conservar el título viejo. Para `titulo`, por tanto, ese `COALESCE` nunca actúa; lo fija `test_el_upsert_rechaza_un_titulo_nulo`.
- **Primera ejecución**: no hay tratamiento especial más allá de la ventana `BDNS_DIAS_PRIMERA_EJECUCION` que ya aplica la consulta. **Reactivación**: al reactivar una alerta pausada, el `desde` sigue siendo su última ejecución, así que se recupera lo publicado durante la pausa; la dedup evita repetidos.

**MEJORA PROPUESTA, no decidida — FK compuesta en la tabla puente**: `alerta_ejecucion_convocatoria.alerta_id` está desnormalizado y **nada garantiza que coincida** con `alerta_ejecucion.alerta_id` de su propia ejecución. Hoy la coherencia la sostiene el código (`registrar_ejecucion` es el único que escribe ahí) y los tests; un `alerta_id` incoherente dejaría el `UNIQUE(alerta_id, convocatoria_id)` apuntando a la alerta equivocada, de modo que se podría notificar dos veces lo mismo y a la vez bloquear una novedad legítima de otra alerta.

- **Qué lo cerraría**: una FK compuesta `(alerta_ejecucion_id, alerta_id) → alerta_ejecucion(id, alerta_id)`. Con ella, escribir un `alerta_id` que no sea el de la ejecución pasa a ser imposible, no solo improbable.
- **Qué exige**: un `UNIQUE(id, alerta_id)` en `alerta_ejecucion` (redundante con su PK, pero Postgres lo necesita como destino de la FK), más su migración y su `downgrade`.
- **Por qué se aplaza**: son dos constraints nuevas y otra migración sobre una tabla que acaba de cambiar, para cubrir un fallo que hoy no puede ocurrir por ninguna vía de escritura existente. **Pendiente de aprobar**: no la implementes por iniciativa propia; si aparece un segundo sitio que escriba en esa tabla, vuelve a ponerla sobre la mesa.

**Gotcha de teardown**: `alerta_ejecucion` y su tabla puente **se borran explícitamente** en `tests/conftest.py`, de dentro hacia fuera y antes que `alerta`. Por la cascada de `alerta` caerían igual —así estuvo y el recuento de restos salía a cero—, pero el borrado explícito deja el orden a la vista: **lo que no puede pasar nunca es que los usuarios se borren antes**, porque las FKs de auditoría de estas tres tablas apuntan a `usuario.id` y Postgres las comprueba antes de que la cascada anidada llegue a limpiarlas (es el bug de los 111 errores). Lo otro que importa: **las convocatorias que cree un test por esta vía deben usar `codigo_bdns_de_prueba()`**, o la limpieza no las reconoce.

**Gotcha ya pagado**: añadir el `alerta_id` NOT NULL rompió dos tests anteriores que insertaban en la tabla puente sin él (`test_alerta_ejecuciones.py` y `test_alertas.py`). Si escribes en esa tabla, el `alerta_id` va siempre.


## Análisis con IA (desde Hito 5: H5.1 cliente, H5.2 prompts)

Decisiones cerradas con el usuario:

- **Versiones fijadas, no se tocan**: `anthropic==0.42.0` y `ANTHROPIC_MODEL=claude-sonnet-5` los decidió el líder del proyecto. Hay versiones más nuevas (SDK 1.x, `claude-sonnet-5-5`), pero subirlas está pendiente de revisarlo con él. No las cambies por iniciativa propia, aunque una herramienta lo sugiera.
- **El contenido de la convocatoria lo consulta el backend a la BDNS** (detalle en `GET /convocatorias?numConv={codigo}`), no lo manda el frontend: el análisis tiene que ser independiente y no manipulable desde el cliente. La consulta (`ClienteBdns.obtener_detalle`) existe desde H5.2; los endpoints que la usan son de H5.3.
- **Límite diario de análisis por empresa: aplazado**. No lo implementes sin que se decida cifra y rol.

Cómo está hecho el cliente (`app/services/ia_cliente.py`), y por qué:

- **Envoltorio fino, sin dominio**: manda `sistema` + un mensaje de usuario. Dos formas: `generar` devuelve `RespuestaIA(texto, modelo, tokens_entrada, tokens_salida)` (texto libre, para el asistente) y `generar_estructurado` (H5.2) devuelve `RespuestaEstructurada(datos, ...)` con los datos ya validados por un modelo Pydantic. Las dos comparten `_llamar` (llamada, errores y log). El prompt está en `ia_prompts.py` y la persistencia es de H5.3. `modelo` es el que **contestó** según Anthropic, que es el que va a `analisis_ia.modelo_ia`.
- **Tres errores, porque quien llama decide cosas distintas**: `IANoConfigurada` (sin clave, 401/403, o **404 = el modelo configurado no existe**: reintentar no sirve, hay que tocar el `.env`), `IANoDisponible` (timeout, red, 429, 5xx, 529: transitorio) e `IARespuestaInvalida` (vacía, cortada por `max_tokens`, o 400/413/422: fallo nuestro). Todos heredan de `ErrorIA` y llevan `status` y `request_id`.
- **Sin clave no falla al construirse, sino al generar**: un endpoint que solo lee análisis guardados tiene que funcionar sin IA configurada.
- **Una respuesta cortada por `max_tokens` es un error**, no un resultado parcial: ni el texto ni los datos de la herramienta estarían completos.
- **Respuesta estructurada = herramienta obligatoria** (*tool use* forzado: `tools` + `tool_choice={"type": "tool", ...}`, admitido por el SDK 0.42.0). El esquema de la herramienta sale del mismo modelo Pydantic que valida la respuesta. **La validación sigue siendo necesaria**: la herramienta obligatoria hace raro, no imposible, que falte un campo. Se exige `stop_reason == "tool_use"` (otro motivo, como una negativa, puede dejar datos a medias que pasarían la validación porque las listas tienen valor por defecto). Una respuesta inválida es `IARespuestaInvalida` y **no se repite**: cada intento se paga.
- **Un proxy que contesta 200 con HTML** (Avast, una pasarela corporativa) hace que el SDK 0.42.0 devuelva el texto en vez de un `Message`. `_llamar` lo comprueba (`isinstance(respuesta, Message)`) y lo convierte en `IANoDisponible`; cualquier otro `anthropic.APIError` también sale como error propio. Sin esto saldría un `AttributeError`, que el análisis en paralelo relanzaría perdiendo las llamadas ya pagadas.
- **Reintentos los del SDK y pocos** (`ANTHROPIC_MAX_REINTENTOS=1`): hay un usuario esperando. Al revés que la BDNS, que no reintenta porque la consulta un proceso en segundo plano. No reimplementes el backoff: el SDK ya lo hace y respeta `retry-after`.
- **El coste solo se calcula con los dos precios configurados** (`ANTHROPIC_PRECIO_ENTRADA_MILLON`/`_SALIDA_MILLON`, USD por millón de tokens, sin valor por defecto): un precio inventado daría un coste falso. Sin ellos, `coste_estimado` queda en NULL. Se redondea a 4 decimales, como `Numeric(10, 4)`.
- **Dependencia**: `ClienteIADep` en `app/api/deps.py`, un cliente por petición. En los tests de endpoints se sustituye con `app.dependency_overrides[obtener_cliente_ia]`.

**Gotcha de seguridad — no uses `str(exc)` de las excepciones del SDK**: el mensaje de `APIStatusError` **incluye el cuerpo de la respuesta de error**, y ese cuerpo puede hacer eco del prompt, que lleva el perfil de la empresa y la ficha de la convocatoria. Comprobado con el SDK 0.42.0. Por eso los errores propios tienen mensajes fijos, y en el log solo van status, `request_id`, modelo, tokens y duración. Lo fija `test_ni_clave_ni_prompt_salen_en_el_error_ni_en_el_log`, con un cuerpo de error simulado que repite el prompt.

**Y tampoco encadenes esas excepciones** (`raise ... from exc`), ni las de validación de Pydantic sobre lo que contestó la IA: el `str()` de la causa sale entero en cualquier traza completa (`logger.exception`, el manejador de 500 de FastAPI). Y `from None` **no basta**: solo oculta la causa en la traza, pero `__context__` sigue apuntando a ella. Lo correcto es guardar el error propio en una variable y lanzarlo **fuera** del bloque `except` (así está en `_llamar` para `APIStatusError` y en `generar_estructurado` para `ValidationError`). Lo fijan `test_el_error_http_del_sdk_no_queda_encadenado` y el test de formato inválido, que comprueban `__cause__` y `__context__`. Los timeouts y errores de conexión sí se encadenan: su texto no lleva cuerpo.

**Gotcha de configuración**: las variables de precio son `Decimal | None` y **una cadena vacía no es `None`**: `ANTHROPIC_PRECIO_ENTRADA_MILLON=` en el `.env` hace que la app no arranque (`ValidationError`). Por eso van comentadas en `.env.example`. Para no configurarlas, se quitan o se comentan, no se dejan vacías.

**Gotcha de tests**: el cliente se prueba con un `AsyncAnthropic` real sobre `httpx.MockTransport` (`http_client=`), no mockeando el SDK: así se cubren de verdad la serialización, los códigos de error y los reintentos. Para el test de reintento, la respuesta 429 lleva `retry-after-ms` corto, o el backoff del SDK alarga el test. Los tests que no reintentan usan `max_retries=0`.

### Prompts y análisis (H5.2)

Decisiones cerradas con el usuario:

- **Una llamada por tipo de análisis**, en paralelo (`analizar_convocatoria`): resumen y requisitos clave dependen solo de la convocatoria y **se comparten entre empresas** (`empresa_id` NULL en `analisis_ia`); la idoneidad es de una empresa. Cada tipo puede fallar por separado: el resultado de un tipo es su `AnalisisGenerado` o su error (`ErrorIA`/`PerfilInsuficiente`), y los demás se conservan porque ya se han pagado. Un error que no sea de esos dos es un fallo nuestro y se propaga.
- **Confidencialidad entre clientes por la firma**: `peticion_resumen` y `peticion_requisitos` solo reciben la ficha, así que el perfil de una empresa no puede acabar en un análisis compartido. No les añadas un parámetro de perfil. Lo fija `test_resumen_y_requisitos_no_pueden_recibir_el_perfil`.
- **Nada que identifique a la empresa**: `PerfilEmpresa` no tiene campo para la razón social ni para el NIF. Del NIF solo se deriva `tipo_persona` por la forma (sin letra de control): persona física (DNI, NIE, K/L/M), persona jurídica, **entidad sin personalidad jurídica** (E, H, U: comunidades de bienes, de propietarios, UTE; art. 11.3 LGS) o **no consta** (extranjero, mal formado, vacío y **J**, porque una sociedad civil puede tener personalidad o no). Lo de la J está pendiente del visto bueno del líder.
- **`Respuesta*` (lo que rellena la IA) frente a `Resultado*` (lo que devuelve la API)**. Los `Respuesta*` son **planos** (sin modelos anidados → sin `$defs`/`$ref` en el esquema; que Anthropic lo acepte tal cual está sin verificar hasta tener clave) y con topes **holgados**: un tope superado rechaza una respuesta ya pagada, así que son red de seguridad; la brevedad se pide en las descripciones. Los textos vacíos de las listas se quitan antes de validar (`_FormatoIA`). Los `Resultado*` añaden lo que pone el código.
- **Lo que pone el código y no la IA** (no está en el formato de la IA, así que no lo puede escribir ni quitar): el **aviso de que no se han leído las bases reguladoras** (`AVISO_BASES`, con `url_bases_reguladoras` si la BDNS la trae), en requisitos **e idoneidad** (no en el resumen); y en la idoneidad, la **regla del perfil incompleto**: sin ninguno de comunidad, sector, tamaño y actividad → `PerfilInsuficiente` **sin llamar a la IA**; sin actividad (descripción ni palabras clave) → un "alto" se rebaja a "medio" con `encaje_limitado_por_perfil=True`; si falta otro dato, decide la IA, a la que se le dice qué falta. No confundas el aviso de las bases con `informacion_insuficiente`, que lo pone la IA y habla de que **la ficha** es pobre.
- **La idoneidad devuelve una categoría** (`alto`/`medio`/`bajo`/`no_encaja`), **no una nota**: el score 0-100 se calcula en código en H5.4. No pidas un número a la IA.
- **La ficha de resumen y requisitos no lleva nada que dependa del día** (se guardan y se comparten). La fecha de hoy solo va en la idoneidad.
- **Topes de los datos de entrada**: campos recortados, listas cortadas con "(y N más)" (y el prompt dice que una lista así está incompleta). Las listas que deciden quién puede pedir la ayuda (tipos de beneficiario, sectores, regiones) llegan hasta 40 elementos, no 15: una ficha real trae 21 sectores. El texto externo no puede cerrar las etiquetas `<convocatoria>`/`<perfil_empresa>`: `<`, `>` y sus parecidos se cambian por `‹`, `›`.
- **Tope de tokens de respuesta por análisis: 4.096** (`MAX_TOKENS_ANALISIS`), sin tocar `ANTHROPIC_MAX_TOKENS` (2.048 para lo demás). Solo se paga lo que se genera, y una respuesta cortada se paga y no sirve. **Pendiente del visto bueno del líder.**
- **Versión de prompt por tipo** (`VERSIONES_PROMPT`, hoy todas `-v1`). Se sube la de un tipo cuando su prompt cambia lo que contesta la IA **y ya hay análisis guardados con la anterior**; no subas las de resumen o requisitos por tocar solo la idoneidad. Dónde se guarda la versión lo decide H5.3 (no hay columna).
- **El tipo `riesgos` existe en `TIPOS_ANALISIS` y en el CHECK, pero no se genera**: no tiene prompt ni formato. No es una funcionalidad olvidada (decisión de H5.2; se replantearía si algún día se leen las bases en PDF).

**Gotcha de la BDNS — `abierto` no significa "plazo abierto hoy"**: comprobado con 40 fichas reales el 2026-10-04 (una con plazo del 01/10 al 01/11 salía `false`; los únicos `true` eran concesiones sin fechas). Se parsea (`DetalleConvocatoriaBdns.abierta`) pero **no se usa ni se manda a la IA**; la idoneidad compara la fecha de hoy con las fechas o el texto del plazo. Y "Concesión directa - instrumental" (25 de esas 40) suele ser una concesión ya decidida a un beneficiario concreto: el prompt lo avisa.

**Gotcha de la BDNS — el detalle**: `GET /convocatorias?numConv=` contesta **204 sin cuerpo** a un código que no existe (no 404) → `ConvocatoriaNoEncontrada`. Con algunos valores (vacío, por ejemplo) su cortafuegos contesta **200 con una página HTML** de "Acceso denegado" → por eso el código se valida antes de llamar (solo dígitos ASCII, longitud leída del modelo) y una respuesta que no es JSON es `BdnsNoDisponible`. El catálogo de tipos de beneficiario está en `GET /beneficiarios?vpd=GE` (cinco valores, incluido `SIN INFORMACION ESPECIFICA`, sin tilde).

**`probar-analisis` CUESTA DINERO**: `docker compose exec api python -m app.cli probar-analisis --codigo <BDNS> [--empresa-id <id>]` hace 2 llamadas de pago a Anthropic (3 con empresa). Es la herramienta para validar la calidad cuando haya clave: no guarda nada y no imprime ni el prompt ni la clave. No lo ejecutes por tu cuenta con una clave real; para probar el cableado sin coste, una clave falsa da 401 en cada análisis.

**Gotcha de tests (H5.2)**: las fichas de `tests/fixtures/bdns/` son **respuestas reales** de la BDNS (datos públicos); `ficha_bdns(codigo)` en `tests/conftest.py` las lee con el mismo parseo del cliente. El simulador de Anthropic compartido (`tests/simulador_anthropic.py`) contesta según `tool_choice.name`, así que las llamadas en paralelo reciben cada una lo suyo, y guarda los cuerpos recibidos para comprobar qué datos viajan en cada llamada.

**Pendiente para H5.3 y el Hito 7 — timeout de API Gateway**: API Gateway HTTP API corta a los **30 s**, y con `ANTHROPIC_TIMEOUT_SEGUNDOS=60` y un reintento una llamada puede acercarse a los dos minutos. En local no se nota. Antes de desplegar hay que decidir si el análisis es síncrono con un timeout menor o en segundo plano (el estado `procesando` de `analisis_ia` ya existe para eso).
