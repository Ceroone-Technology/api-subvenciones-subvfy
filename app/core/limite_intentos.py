"""Failed-attempt limiter, in memory and per process.

Used by `POST /auth/cambiar-password`: that endpoint checks a password for someone who already holds a valid access
token, so without a limit a stolen token could be used to guess the current password at bcrypt speed. After
`MAX_FALLOS` failures inside `VENTANA` the key is refused (429) until the oldest failure leaves the window.

Honest limits: the state lives in the process, so with several workers or Lambda instances the effective limit is
`MAX_FALLOS` per process. It narrows the guessing rate, it does not replace a shared limiter (API Gateway throttling
or a table), which belongs to the deployment milestone.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable

MAX_FALLOS = 5
VENTANA = 15 * 60  # seconds


class LimiteIntentos:
    def __init__(self, maximo: int = MAX_FALLOS, ventana: float = VENTANA, reloj: Callable[[], float] = time.monotonic):
        self._maximo, self._ventana, self._reloj = maximo, ventana, reloj
        self._fallos: dict[int, deque[float]] = defaultdict(deque)

    def _vigentes(self, clave: int) -> deque[float]:
        fallos = self._fallos[clave]
        limite = self._reloj() - self._ventana
        while fallos and fallos[0] <= limite:
            fallos.popleft()
        if not fallos:
            self._fallos.pop(clave, None)
        return fallos

    def bloqueado(self, clave: int) -> bool:
        return len(self._vigentes(clave)) >= self._maximo

    def registrar_fallo(self, clave: int) -> None:
        self._vigentes(clave)
        self._fallos[clave].append(self._reloj())

    def olvidar(self, clave: int) -> None:
        self._fallos.pop(clave, None)


cambio_de_password = LimiteIntentos()
