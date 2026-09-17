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
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.core.security import PASSWORD_MIN_LONGITUD, hashear_password
from app.database import AsyncSessionLocal
from app.models import Empresa, Rol, Usuario


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

    args = parser.parse_args(argv)
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
