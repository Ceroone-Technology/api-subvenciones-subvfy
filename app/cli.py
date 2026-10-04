"""Utilidades de línea de comandos.

Existe por un problema de arranque en frío: desde el Hito 2 Funcionalidad 4
crear un usuario exige estar autenticado, y autenticarse exige que ya haya
un usuario. Alguien tiene que poner el primero desde fuera de la API.

No se siembra por migración a propósito: eso dejaría una contraseña
conocida (y publicada en el repositorio) en toda instalación.

    docker compose exec api python -m app.cli crear-admin \\
        --empresa "Grupo Bigtoone" --nif B12345678 \\
        --email omar.carreno@ceroone.com --nombre Omar --apellidos Carreno

Si se omite `--password`, se pide por consola sin mostrarla. Si la empresa
ya existe (por NIF), se reutiliza en vez de crear otra.

`probar-analisis` (Hito 5, H5.2) sirve para comprobar a mano la calidad del
Análisis con IA con convocatorias reales. **CUESTA DINERO: cada ejecución hace
2 o 3 llamadas de pago a la API de Anthropic.** Consulta la convocatoria a la
BDNS, lanza el resumen y los requisitos clave (y la idoneidad, con
`--empresa-id`) e imprime el resultado con el modelo, los tokens y el coste
estimado. Es una herramienta de verificación, no una funcionalidad: **no
guarda nada en la base de datos** y no imprime ni la clave ni el prompt.

    docker compose exec api python -m app.cli probar-analisis --codigo 933305 [--empresa-id 12]
"""

import argparse
import asyncio
import getpass
import json
import sys
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.config import settings
from app.core.security import PASSWORD_MIN_LONGITUD, hashear_password
from app.database import AsyncSessionLocal
from app.models import Empresa, Rol, Usuario
from app.services.analisis_ia import AnalisisGenerado, analizar_convocatoria
from app.services.bdns_cliente import BdnsNoDisponible, ClienteBdns, ConvocatoriaNoEncontrada
from app.services.ia_cliente import ClienteIA
from app.services.ia_entrada import PerfilEmpresa, cargar_perfil

AVISO_COSTE = (
    "AVISO: este comando hace llamadas de pago a la API de Anthropic (2, o 3 con --empresa-id). "
    "Cada ejecución cuesta dinero."
)


async def crear_admin(
    *, empresa: str, nif: str, email: str, nombre: str, apellidos: str, password: str
) -> None:
    nif = nif.strip().upper()
    async with AsyncSessionLocal() as db:
        if (await db.execute(select(Usuario.id).where(Usuario.email == email))).first():
            raise SystemExit(f"Ya existe un usuario con el email {email}.")

        rol_admin = (
            await db.execute(select(Rol).where(Rol.codigo == "admin"))
        ).scalar_one_or_none()
        if rol_admin is None:
            raise SystemExit("No existe el rol 'admin'. ¿Has aplicado las migraciones (alembic upgrade head)?")

        registro = (
            await db.execute(select(Empresa).where(Empresa.nif == nif))
        ).scalar_one_or_none()
        if registro is None:
            registro = Empresa(razon_social=empresa, nif=nif)
            db.add(registro)
            await db.flush()
            print(f"Empresa creada: {registro.razon_social} (id {registro.id}, NIF {registro.nif})")
        else:
            print(f"Empresa existente reutilizada: {registro.razon_social} (id {registro.id})")

        usuario = Usuario(
            empresa_id=registro.id,
            rol_id=rol_admin.id,
            nombre=nombre,
            apellidos=apellidos,
            email=email,
            password_hash=hashear_password(password),
        )
        db.add(usuario)
        await db.flush()
        # Se firma a sí mismo: no hay nadie anterior a quien atribuir el alta.
        usuario.created_by = usuario.id
        usuario.updated_by = usuario.id
        registro.created_by = usuario.id
        registro.updated_by = usuario.id
        await db.commit()

        print(f"Admin creado: {usuario.email} (id {usuario.id}, empresa {registro.id})")
        print("Ya puedes obtener un token con POST /auth/login.")


async def probar_analisis(
    *,
    codigo: str,
    empresa_id: int | None = None,
    cliente_bdns: ClienteBdns | None = None,
    cliente_ia: ClienteIA | None = None,
) -> int:
    """Analiza una convocatoria real e imprime el resultado. Devuelve el
    código de salida: 0 si todos los análisis salen bien, 1 si alguno falla y
    2 si no se llega a llamar a la IA.

    Los clientes se pueden inyectar para los tests; sin ellos se usan los
    reales. Lo único que lee de la base de datos es el perfil de la empresa.
    """
    print(AVISO_COSTE)
    if cliente_ia is None and not settings.anthropic_api_key:
        print("No se puede analizar: falta ANTHROPIC_API_KEY en el .env.")
        return 2

    perfil: PerfilEmpresa | None = None
    if empresa_id is not None:
        async with AsyncSessionLocal() as db:
            perfil = await cargar_perfil(db, empresa_id)
        if perfil is None:
            print(f"No existe ninguna empresa con id {empresa_id}.")
            return 2

    try:
        async with cliente_bdns or ClienteBdns() as bdns:
            ficha = await bdns.obtener_detalle(codigo)
    except (ValueError, ConvocatoriaNoEncontrada, BdnsNoDisponible) as exc:
        print(f"No se pudo obtener la convocatoria: {exc}")
        return 2

    print(f"Convocatoria {ficha.codigo_bdns}: {ficha.titulo or '(sin título)'}")
    async with cliente_ia or ClienteIA() as ia:
        resultados = await analizar_convocatoria(ia, ficha, perfil, hoy=date.today())

    for tipo, resultado in resultados.items():
        print()
        if isinstance(resultado, AnalisisGenerado):
            coste = f"{resultado.coste_estimado} USD" if resultado.coste_estimado is not None else "sin calcular"
            print(f"== {tipo} ({resultado.version_prompt}) ==")
            print(
                f"Modelo: {resultado.modelo} | Tokens: {resultado.tokens_entrada} de entrada, "
                f"{resultado.tokens_salida} de salida | Coste estimado: {coste}"
            )
            print(json.dumps(resultado.resultado.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            # Los mensajes de nuestros errores son fijos: no llevan ni el
            # prompt ni el cuerpo de la respuesta de Anthropic.
            print(f"== {tipo} ==")
            print(f"ERROR ({type(resultado).__name__}): {resultado}")

    generados = [r for r in resultados.values() if isinstance(r, AnalisisGenerado)]
    costes = [r.coste_estimado for r in generados]
    coste_total = sum((c for c in costes if c is not None), Decimal(0)) if costes and None not in costes else None
    texto_coste = f"{coste_total} USD" if coste_total is not None else "sin calcular"
    print()
    print(
        f"Total: {sum(r.tokens_entrada for r in generados)} tokens de entrada, "
        f"{sum(r.tokens_salida for r in generados)} de salida, coste estimado {texto_coste}."
    )
    if coste_total is None and generados:
        print("(Para calcular el coste, configura ANTHROPIC_PRECIO_ENTRADA_MILLON y ANTHROPIC_PRECIO_SALIDA_MILLON.)")
    return 0 if len(generados) == len(resultados) else 1


def _pedir_password() -> str:
    password = getpass.getpass("Contraseña del admin: ")
    if password != getpass.getpass("Repite la contraseña: "):
        raise SystemExit("Las contraseñas no coinciden.")
    if len(password) < PASSWORD_MIN_LONGITUD:
        raise SystemExit(f"La contraseña debe tener al menos {PASSWORD_MIN_LONGITUD} caracteres.")
    return password


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    subcomandos = parser.add_subparsers(dest="comando", required=True)

    crear = subcomandos.add_parser("crear-admin", help="Crea el primer usuario administrador.")
    crear.add_argument("--empresa", required=True, help="Razón social de la empresa.")
    crear.add_argument("--nif", required=True, help="NIF de la empresa (se reutiliza si ya existe).")
    crear.add_argument("--email", required=True)
    crear.add_argument("--nombre", required=True)
    crear.add_argument("--apellidos", required=True)
    crear.add_argument(
        "--password",
        help="Si se omite, se pide por consola (recomendado: no queda en el historial del shell).",
    )

    probar = subcomandos.add_parser(
        "probar-analisis",
        help="CUESTA DINERO: analiza con la IA una convocatoria real de la BDNS (2 o 3 llamadas de pago).",
        description=f"{AVISO_COSTE} Comprueba a mano la calidad del Análisis con IA. No guarda nada.",
    )
    probar.add_argument("--codigo", required=True, help="Código BDNS de la convocatoria (solo dígitos).")
    probar.add_argument(
        "--empresa-id",
        type=int,
        help="Id de una empresa: añade la idoneidad con su perfil (una llamada de pago más).",
    )

    args = parser.parse_args(argv)
    if args.comando == "probar-analisis":
        raise SystemExit(asyncio.run(probar_analisis(codigo=args.codigo, empresa_id=args.empresa_id)))

    password = args.password or _pedir_password()
    if len(password) < PASSWORD_MIN_LONGITUD:
        raise SystemExit(f"La contraseña debe tener al menos {PASSWORD_MIN_LONGITUD} caracteres.")

    asyncio.run(
        crear_admin(
            empresa=args.empresa,
            nif=args.nif,
            email=args.email,
            nombre=args.nombre,
            apellidos=args.apellidos,
            password=password,
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
