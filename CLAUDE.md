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

## Backlog: Hito → Funcionalidad → Tarea

El desarrollo completo está desglosado en `D:\Trabajo\BigToOne\Subvenciones\api-hitos-funcionalidades-tareas-subvfy.docx` (103h de las 166h totales del proyecto ya aprobadas — el resto es frontend, en `hitos-tareas-subvfy.docx`, misma carpeta). Estado:

- **Hito 2, Funcionalidad 1 — Arquitectura y esqueleto**: hecho.
- **Hito 2, Funcionalidad 2 — Modelo de datos y migraciones**: hecho. Modelos en `app/models/`, migraciones en `alembic/versions/`, seed de roles aplicado.
- **Hito 2, Funcionalidad 3 — API de empresa/rol/usuario**: hecho. Schemas en `app/schemas/`, routers en `app/api/routes/` (`roles.py`, `empresas.py`, `usuarios.py`), hashing de contraseñas en `app/core/security.py`, 28 tests verdes contra Postgres real.
- **Hito 2, Funcionalidad 4 — Autenticación real (JWT)**: hecho. JWT en `app/core/security.py`, permisos en `app/core/permisos.py`, router en `app/api/routes/auth.py`, arranque en frío en `app/cli.py`, seed del usuario de sistema en `b7f3c21a9d40`. 70 tests verdes.
- **Hito 3, Funcionalidad 1 — Endpoints de favoritos**: hecho. Router en `app/api/routes/favoritos.py`, schemas de favorito y convocatoria. 85 tests verdes.
- **Hito 4, Funcionalidad 1 — CRUD de alertas (10 h)**: hecho (mutaciones). Router en `app/api/routes/alertas.py`, lógica en `app/services/alertas.py`, normalización de filtros en `app/services/filtros.py`, índices en `e4a19c7d2b58`. 119 tests verdes (34 nuevos), ruff y mypy limpios. Los GET de listado y detalle quedan para la siguiente tarea, y los 404 de alertas aún no están declarados en el OpenAPI (`responses={404: ...}`).
- Hito 4 (Alertas, 22 h), Hito 5 (Análisis con IA), Hito 6 (Ficha ampliada, sin cambios de backend), Hito 7 (Cierre): pendientes, ver el .docx para el desglose de tareas y horas de cada uno.

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

**Gotcha**: si en un PATCH solo cambian filas hijas, la fila de `alerta` no queda sucia y el `onupdate` de `updated_at` no salta. Por eso el servicio lanza un `UPDATE` explícito de `updated_at`/`updated_by`, y `_leer` usa `populate_existing` para no devolver el valor viejo del identity map.

**ruff y mypy** están en `requirements-dev.txt`, configurados en `pyproject.toml`: `docker compose exec api ruff check .` y `docker compose exec api mypy app`.

**Gotcha de tests**: el teardown de `tests/conftest.py` tiene que poner a NULL `created_by`/`updated_by` de las convocatorias de prueba **antes** de borrar los usuarios, porque esas columnas apuntan a `usuario.id`. Si añades una tabla nueva que los procesos firmen, va en ese mismo bloque y en ese mismo orden.

**Gotcha de tests (alertas)**: las alertas de prueba se borran **explícitamente antes** que los usuarios, no por la cascada. `alerta_organo`/`alerta_region` quedan a dos niveles (usuario → alerta → hija), y Postgres comprueba su FK de auditoría `created_by → usuario` antes de que la cascada llegue a borrarlas. Sin ese paso, el primer teardown falla y deja restos que tumban la limpieza de todos los tests siguientes (fueron 111 errores). Aplica a cualquier tabla nueva firmada que quede a más de un nivel de cascada del usuario. En producción no pasa, porque los usuarios solo se dan de baja lógica.

## Notificaciones de alertas (desde Hito 4, Funcionalidad 3)

Decisiones cerradas con el usuario — no las reabras sin preguntar:

- **Proveedor: Amazon SES**, por coherencia con el despliegue en AWS ya decidido. `EMAIL_BACKEND` elige entre `consola` (por defecto, solo log) y `ses`. El defecto es `consola` para que ningún entorno mande correo real por descuido.
- **Los enlaces del correo llevan a la ficha dentro de Subvfy**, no al portal de la BDNS: el aviso devuelve al usuario al producto. Las rutas son configurables (`FRONTEND_RUTA_*`) porque **no se han contrastado con el routing real del Angular** — el frontend no estaba en la máquina. Verifícalas antes de enviar nada en producción.
- **HTML con marca + alternativa en texto plano**, siempre las dos partes.
- **Destinatario: el dueño de la alerta** (`alerta.usuario_id`). Las alertas son personales, como los favoritos.

Tres invariantes que sostienen los tests y conviene no romper:

- **La ejecución se registra pase lo que pase.** Un fallo del proveedor **no se propaga**: se guarda `estado_envio = "error"` con el detalle. Si se relanzara, un SES caído tumbaría la pasada entera de alertas y se perdería el rastro de que se evaluaron.
- **Las convocatorias notificadas se persisten aunque el envío falle**, en `alerta_ejecucion_convocatoria`. Es lo que impide re-avisar de lo mismo; si se quiere reintentar, la decisión es del motor (F2), no de este servicio.
- **Una cuenta no activa cuenta como `error`, no como silencio.** Si alguien deja de recibir avisos por estar bloqueado, tiene que verse en el historial de la alerta.

**`enviar()` es síncrono a propósito** (boto3 bloquea y no tiene versión async); el servicio lo saca del event loop con `asyncio.to_thread`. No lo envuelvas en una corrutina falsa.

**Firma de auditoría**: las filas que escribe el motor llevan `created_by`/`updated_by` del usuario de sistema, vía `app/services/auditoria.py`. Es justo para lo que se sembró en `b7f3c21a9d40`. Cualquier proceso automático futuro (sync BDNS, análisis IA en batch) debe usar `id_usuario_sistema()` en vez de inventarse un autor.

**Pendiente de infraestructura, no de código**: verificar el dominio del remitente en SES (SPF/DKIM) y sacar la cuenta del sandbox, donde solo se puede escribir a direcciones verificadas.
