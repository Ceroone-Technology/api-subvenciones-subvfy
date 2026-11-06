"""Profile image processing: type from content, re-encode to WebP, bounded size, no metadata, no bombs."""

import io

import pytest
from PIL import Image

from app.services import imagenes_perfil as ip


def imagen(formato: str, tam: tuple[int, int] = (64, 64), modo: str = "RGB", **extra) -> bytes:
    salida = io.BytesIO()
    Image.new(modo, tam, (200, 30, 30) if modo == "RGB" else (200, 30, 30, 128)).save(salida, formato, **extra)
    return salida.getvalue()


def abrir(datos: bytes) -> Image.Image:
    return Image.open(io.BytesIO(datos))


@pytest.mark.parametrize("formato", ["JPEG", "PNG", "WEBP"])
def test_acepta_los_tres_formatos_y_devuelve_webp(formato: str) -> None:
    assert abrir(ip.procesar_imagen(imagen(formato))).format == "WEBP"


def test_reduce_a_256_sin_deformar() -> None:
    assert abrir(ip.procesar_imagen(imagen("PNG", (1024, 512)))).size == (256, 128)


def test_no_amplia_las_pequenas() -> None:
    assert abrir(ip.procesar_imagen(imagen("PNG", (40, 30)))).size == (40, 30)


def test_conserva_la_transparencia() -> None:
    resultado = abrir(ip.procesar_imagen(imagen("PNG", modo="RGBA"))).convert("RGBA")
    assert resultado.getpixel((1, 1))[3] < 255


def test_quita_exif_e_icc() -> None:
    exif = Image.Exif()
    exif[0x010F] = "Camara secreta"
    origen = imagen("JPEG", exif=exif.tobytes())
    assert b"Camara secreta" in origen
    resultado = ip.procesar_imagen(origen)
    assert b"Camara secreta" not in resultado
    assert not abrir(resultado).getexif()
    assert "icc_profile" not in abrir(resultado).info


def test_aplica_la_orientacion_antes_de_quitar_exif() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90
    assert abrir(ip.procesar_imagen(imagen("JPEG", (100, 50), exif=exif.tobytes()))).size == (50, 100)


def test_el_tipo_sale_del_contenido() -> None:
    for basura in (
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        imagen("GIF"),
        b"%PDF-1.7 ...",
    ):
        with pytest.raises(ip.ImagenNoSoportada):
            ip.procesar_imagen(basura)


def test_una_imagen_truncada_es_invalida() -> None:
    with pytest.raises(ip.ImagenInvalida):
        ip.procesar_imagen(imagen("PNG", (200, 200))[:60])


def test_una_bomba_de_descompresion_se_rechaza() -> None:
    # 12000x12000 = 144M px, far over the cap, but a tiny PNG: refused before decoding.
    bomba = imagen("PNG", (12000, 12000))
    assert len(bomba) < ip.MAX_BYTES
    with pytest.raises(ip.ImagenInvalida):
        ip.procesar_imagen(bomba)


def test_el_limite_de_pixeles_es_el_configurado() -> None:
    assert ip.MAX_PIXELES <= 25_000_000


def test_un_archivo_vacio_es_invalido_pero_no_de_otro_tipo() -> None:
    with pytest.raises(ip.ImagenInvalida) as e:
        ip.procesar_imagen(b"")
    assert not isinstance(e.value, ip.ImagenNoSoportada)
