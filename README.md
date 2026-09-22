# Subvfy API

API de `app-subvenciones-subvfy`: favoritos, alertas, autenticación y análisis con IA.

## Stack

FastAPI + SQLAlchemy 2.0 (async) + Alembic + Pydantic v2, sobre PostgreSQL (asyncpg). JWT para autenticación, APScheduler para el motor de Alertas, Anthropic SDK para Análisis con IA y Asistente IA conversacional. Las decisiones cerradas (stack, arquitectura cloud, modelo de permisos, gotchas ya resueltos) están en `CLAUDE.md`.

## Flujo de ramas

**No se sube a `main`.** La rama de integración es **`develop`**.

- **`develop`** es donde se integra todo el trabajo. Las ramas salen de `develop` y el PR va contra `develop`.
- **`main`** está protegida: solo recibe PRs desde `develop`, y solo con el CI en verde. No acepta push directo.
- **CI en cada PR** (GitHub Actions, `.github/workflows/ci.yml`): migraciones, pytest, ruff y mypy contra un PostgreSQL real. Si algo falla, el PR no se puede mergear.

```bash
git fetch origin
git checkout develop
git pull
git checkout -b feature/lo-que-toque

# ... trabajo, commits ...

git push -u origin feature/lo-que-toque
# abrir el PR contra develop, no contra main
```

Al abrir el PR en GitHub, comprueba que el desplegable **base:** diga `develop`: por defecto propone `main`. Antes de subir, pasa en local lo mismo que el CI (ver [Tests](#tests)).

## Arranque con Docker

Lo único que necesitas instalado es **Docker Desktop**. Ni Python ni PostgreSQL: ambos van dentro de los contenedores.

### Puesta en marcha desde cero

```bash
git clone https://github.com/Ceroone-Technology/api-subvenciones-subvfy.git
cd api-subvenciones-subvfy
git checkout develop        # la rama de trabajo; main solo recibe lo ya integrado

# 1. Configuración. El .env real nunca se sube: está en .gitignore.
cp .env.example .env

# 2. Levantar API + PostgreSQL. La primera vez tarda: construye la imagen.
docker compose up -d --build

# 3. Crear el esquema y los datos semilla.
docker compose exec api alembic upgrade head

# 4. Crear el primer administrador (ver "Primer administrador" más abajo).
docker compose exec api python -m app.cli crear-admin \
  --empresa "Grupo Bigtoone" --nif B12345678 \
  --email tu.email@ceroone.com --nombre Tu --apellidos Nombre
```

Los pasos 3 y 4 solo hacen falta la primera vez: los datos viven en un volumen de Docker que sobrevive a apagar los contenedores.

| Servicio | Dónde queda |
|---|---|
| API | http://localhost:8000 |
| Documentación interactiva (Swagger) | http://localhost:8000/docs |
| Spec OpenAPI (lo que consume Angular) | http://localhost:8000/openapi.json |
| PostgreSQL | `localhost:5432` — usuario `subvfy`, contraseña `subvfy`, base `subvfy` |

Para probar la API, lo más cómodo es **http://localhost:8000/docs**: haz `POST /auth/login` con las credenciales del admin que acabas de crear, copia el `access_token` y pégalo en el botón *Authorize* de arriba a la derecha. A partir de ahí, todas las llamadas que lances desde el Swagger llevan la cabecera puesta.

### El día a día

Todos estos comandos se ejecutan **desde tu máquina**, en la carpeta del proyecto: `docker compose exec` es justo lo que entra al contenedor por ti, no hay que "entrar" a ningún sitio antes. Si ves `no configuration file provided: not found`, es que te falta el `cd`.

```bash
docker compose up -d            # levantar (sin --build si no cambiaste dependencias)
docker compose stop             # parar sin borrar nada
docker compose logs -f api      # ver los logs de la API en vivo
docker compose ps               # estado de los contenedores

docker compose exec api pytest                     # la suite de tests
docker compose exec db psql -U subvfy -d subvfy    # consola de PostgreSQL (\dt lista, \q sale)
```

**Importante al desarrollar**: la carpeta `app/` está montada en el contenedor, pero uvicorn corre sin `--reload`, así que los cambios en el código **no se recargan solos**:

```bash
docker compose restart api      # tras tocar código
```

Si prefieres recarga automática, añade esta línea al servicio `api` de `docker-compose.yml`:

```yaml
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Reconstruir la imagen (`--build`) solo hace falta cuando cambian `requirements.txt` o el `Dockerfile`.

### Empezar de cero

```bash
docker compose down             # borra los contenedores, conserva los datos
docker compose down -v          # borra TAMBIÉN la base de datos (volumen incluido)
```

Tras un `down -v` hay que repetir los pasos 3 y 4 de la puesta en marcha.

## Arranque local (sin Docker)

Requiere PostgreSQL 15+ corriendo aparte.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                # ajustar DATABASE_URL si hace falta
alembic upgrade head
uvicorn app.main:app --reload
```

## Migraciones (Alembic)

```bash
alembic upgrade head          # aplica todo lo que falte (esquema + seed)
alembic revision --autogenerate -m "descripción del cambio"   # para el siguiente cambio
```

El esquema de `schema-subvfy.sql` ya está implementado en `app/models/` (14 tablas) y aplicado en dos migraciones:

- `827c98b6a656_esquema_inicial_subvfy.py` — las 14 tablas, constraints e índices. Las FK de auditoría con dependencia circular (rol/empresa/usuario, vía `created_by`/`updated_by`) se añaden con `op.create_foreign_key()` al final de `upgrade()` — `use_alter=True` dentro de `create_table()` no genera el `ALTER TABLE` por sí solo en Alembic, hay que añadirlo explícito (y su `drop_constraint` correspondiente al inicio de `downgrade()`).
- `0db12626fe94_seed_catalogo_de_roles.py` — siembra el catálogo de roles (`admin`, `gestor`, `usuario`).
- `b7f3c21a9d40_seed_empresa_y_usuario_de_sistema.py` — siembra la identidad interna de auditoría para procesos automáticos.
- `e4a19c7d2b58_indices_de_filtros_de_alerta.py` — índices por `organo_bdns_id`/`region_bdns_id` para la búsqueda inversa del motor de alertas.

Verificado con `alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head` sin errores, contra un PostgreSQL real.

## Endpoints

| Recurso | Endpoints |
|---|---|
| Sesión | `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me` |
| Roles | `GET /roles`, `GET /roles/{id}` (catálogo cerrado, solo lectura) |
| Empresas | `GET /empresas`, `POST /empresas`, `GET/PATCH/DELETE /empresas/{id}` |
| Usuarios | `GET /usuarios`, `POST /usuarios`, `GET/PATCH/DELETE /usuarios/{id}` |
| Favoritos | `GET /favoritos`, `POST /favoritos`, `GET/PATCH/DELETE /favoritos/{codigo_bdns}` |
| Alertas | `POST /alertas`, `PATCH/DELETE /alertas/{id}` (el listado y el detalle llegan en la siguiente tarea) |

Convenciones comunes a los listados y las escrituras:

- **Paginación**: `?page=1&size=20` (`size` máximo 100). La respuesta es
  `{items, total, page, size}`, donde `total` cuenta las filas que cumplen el
  filtro, no las de la página.
- **Filtros**: `q` (búsqueda por texto), más `estado` en empresas y
  `empresa_id`/`rol_id`/`estado` en usuarios.
- **PATCH parcial**: solo se aplican los campos presentes en el body.
- **`DELETE` es baja lógica** (`estado` → `inactiva`/`inactivo`), no borrado
  físico: empresas y usuarios están referenciados por las columnas de
  auditoría `created_by`/`updated_by` de todo el esquema.
- **Códigos de error**: 404 si el recurso no existe, 409 si se repite un valor
  único (NIF de empresa, email de usuario), 400 si se referencia una empresa o
  un rol inexistente, 422 si el body no valida.
- La contraseña se envía en claro (`password`) y se guarda hasheada con bcrypt;
  `password_hash` no aparece en ninguna respuesta.

## Autenticación y permisos

Todos los endpoints salvo `POST /auth/login` y `POST /auth/refresh` exigen un
access token: `Authorization: Bearer <token>`.

**Sesión stateless**: el login devuelve un `access_token` (60 min) y un
`refresh_token` (30 días), ambos JWT firmados. No hay tabla de sesiones ni
denylist, así que `POST /auth/logout` es del lado del cliente — descarta los
tokens; el access sigue siendo válido hasta que caduca. Lo que sí es inmediato
es el bloqueo: el token solo lleva el id de usuario y el usuario se relee de la
base de datos en cada petición, así que cambiar su rol o su estado surte efecto
en la llamada siguiente.

**No hay registro público.** Subvfy es B2B: las altas las hace un admin con
`POST /empresas` y `POST /usuarios`.

| Rol | Alcance |
|---|---|
| `admin` | Global: cualquier empresa y cualquier usuario. |
| `gestor` | Lectura y escritura solo dentro de su propia empresa. |
| `usuario` | Lee su empresa; edita únicamente su propio perfil (sin tocar `empresa_id`, `rol_id` ni `estado`). |

Quien no es admin nunca ve datos de otra empresa: los listados **fuerzan** el
filtro a la empresa del token, y un recurso de otro tenant responde 404 (no
403, que confirmaría su existencia).

### Favoritos

Tres cosas que los separan del resto de recursos:

- **Son personales, no de la empresa.** La tabla cuelga de `usuario_id`: ni un
  gestor ni un admin ven los favoritos de otra persona.
- **Se direccionan por `codigo_bdns`**, no por el id de la fila. El frontend
  consulta la BDNS directamente y es el código lo que tiene en mano al pintar
  el botón de favorito, así que no necesita arrastrar ningún id nuestro.
- **Marcar un favorito siembra la caché local de convocatorias.** El POST lleva
  la ficha dentro y hace upsert por `codigo_bdns`; si ya estaba cacheada se
  refresca. Solo se sobrescriben los campos enviados, para que marcar desde el
  listado de resultados (ficha incompleta) no borre lo que ya estaba
  sincronizado.

```json
POST /favoritos
{
  "convocatoria": {
    "codigo_bdns": "770001",
    "titulo": "Kit Digital - Segmento III",
    "nivel_administracion": "estado",
    "organo_convocante": "Red.es",
    "financiada_mrr": true
  },
  "nota": "Revisar requisitos de plantilla"
}
```

Marcar dos veces la misma convocatoria devuelve 409, no un 200 idempotente: así
el frontend distingue "acabo de marcarla" de "ya la tenía". Quitar un favorito
sí es borrado físico (no tiene valor histórico), pero **la convocatoria cacheada
se conserva**: la comparten alertas y análisis IA.

### Alertas

- **Son personales**, como los favoritos: solo su propietario las edita o
  borra. Para cualquier otro, admin incluido, una alerta ajena responde 404.
- **Órganos y regiones se filtran por id del catálogo de la BDNS**, no por
  texto: el frontend ya tiene esos ids porque consulta la BDNS. Se guardan
  normalizados (una fila por id en `alerta_organo`/`alerta_region`, sin
  duplicados), y un id no válido (`0`, negativo o texto) responde 422
  indicando el campo y el valor recibido.
- En `PATCH`, `organos`/`regiones` **reemplazan** la lista entera (`[]` la
  vacía; omitirlos la deja como estaba). Los campos obligatorios no admiten
  `null`.
- `DELETE` es **borrado físico** y se lleva el histórico de ejecuciones. Para
  pausar una alerta sin perderlo: `PATCH` con `"activa": false`.

```json
POST /alertas
{
  "nombre": "Digitalización en Andalucía",
  "texto_busqueda": "digitalización",
  "nivel_administracion": "ccaa",
  "frecuencia": "semanal",
  "organos": [1500],
  "regiones": [9]
}
```

### Primer administrador

Crear un usuario exige estar autenticado, y autenticarse exige que ya exista
un usuario. Ese arranque en frío se resuelve desde fuera de la API:

```bash
docker compose exec api python -m app.cli crear-admin   --empresa "Grupo Bigtoone" --nif B12345678   --email tu.email@ceroone.com --nombre Tu --apellidos Nombre
```

Si omites `--password`, se pide por consola y no queda en el historial del
shell. No se siembra por migración a propósito: eso dejaría una contraseña
conocida y publicada en cada instalación.

El `sistema@subvfy.es` que siembra la migración `b7f3c21a9d40` **no** es una
cuenta de acceso: nace `bloqueado` con contraseña aleatoria y existe solo para
firmar `created_by`/`updated_by` de los procesos automáticos (sync de la BDNS,
alertas, análisis IA en batch).

## Tests

```bash
docker compose exec api pytest
docker compose exec api ruff check .
docker compose exec api mypy app
```

Son las mismas comprobaciones que ejecuta el CI en cada PR: si fallan aquí, el PR no se podrá mergear. Ojo: el CI usa Python 3.11 y la imagen de Docker 3.12, así que evita la sintaxis exclusiva de 3.12.

Los tests de endpoints escriben en una base de datos real (no mocks) y limpian
sus filas al terminar; para poder distinguirlas usan NIFs con prefijo `TEST-` y
emails bajo `@test.subvfy.example.com`.

## Estructura

```
app/
  main.py          # arranque de FastAPI, middlewares, registro de routers
  config.py        # settings (variables de entorno)
  database.py      # engine async, sesión, Base declarativa
  models/          # modelos SQLAlchemy (uno por tabla de schema-subvfy.sql)
  schemas/         # schemas Pydantic de request/response
  api/routes/      # un router por recurso (rol, empresa, usuario, auth, ...)
  services/        # lógica de negocio sin HTTP (desde alertas)
  core/            # seguridad (JWT/hash), permisos por rol, scheduler
  cli.py           # utilidades de consola (crear el primer admin)
alembic/           # migraciones
tests/             # pytest + httpx.AsyncClient
```

## Backlog

El desarrollo se organiza como Hito → Funcionalidad → Tarea en `api-hitos-funcionalidades-tareas-subvfy.docx` (carpeta del proyecto). Estado:

- Hito 2, Funcionalidad 1 — Arquitectura y esqueleto: **hecho**.
- Hito 2, Funcionalidad 2 — Modelo de datos y migraciones: **hecho** (modelos SQLAlchemy + migraciones + seed, verificado con tests contra Postgres real).
- Hito 2, Funcionalidad 3 — API de empresa/rol/usuario: **hecho** (schemas Pydantic + CRUD paginado + hashing de contraseñas).
- Hito 2, Funcionalidad 4 — Autenticación real (JWT): **hecho** (login/refresh/logout/me, autorización por rol, aislamiento multi-tenant, 70 tests contra Postgres real).
- Hito 3, Funcionalidad 1 — Endpoints de favoritos: **hecho** (marcar/quitar, listado con join a convocatoria, nota personal, 85 tests contra Postgres real).
- Hito 4, Funcionalidad 1 — CRUD de alertas: **hecho** (crear/editar/eliminar, filtros de órgano y región normalizados por id BDNS).
