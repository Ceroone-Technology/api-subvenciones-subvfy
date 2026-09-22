FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Imagen de desarrollo local: instala tambien las dependencias de test para
# que `docker compose exec api pytest` funcione dentro del contenedor. El
# empaquetado de produccion (Lambda + Mangum) es del Hito 7 y sera distinto.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY app ./app
COPY alembic ./alembic
COPY tests ./tests
COPY alembic.ini pytest.ini pyproject.toml ./

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
