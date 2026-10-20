"""API de productos para Azure SQL con Flask y python-tds.

Rutas:
    GET  /test-db
    GET  /api/products
    GET  /api/products/<id>
    POST /api/products
    PUT  /api/products/no_lock
    PUT  /api/products/lock
    PUT  /api/products/update_version

En Render configura las variables DB_SERVER, DB_NAME, DB_USER y DB_PASSWORD.
La tabla Products debe tener id, names, price, stock y version (INT).
"""

import os
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import certifi
import pytds
from flask import Flask, jsonify, request


app = Flask(__name__)

# Solo protege solicitudes del MISMO proceso Python.
# Para varios servidores o procesos usa el control de version en SQL Server.
product_lock = threading.Lock()


@app.after_request
def identificar_servidor(response):
    response.headers["X-Servidor"] = os.getenv("NOMBRE_SERVIDOR", "Servidor-1")
    return response


def getConnection():
    """Conecta a Azure SQL usando TDS con validacion del certificado TLS."""
    requeridas = ("DB_SERVER", "DB_NAME", "DB_USER", "DB_PASSWORD")
    faltantes = [nombre for nombre in requeridas if not os.getenv(nombre)]
    if faltantes:
        raise RuntimeError(
            "Faltan variables de entorno para Azure SQL: " + ", ".join(faltantes)
        )

    return pytds.connect(
        server=os.environ["DB_SERVER"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        port=1433,
        login_timeout=15,
        timeout=30,
        autocommit=False,
        cafile=certifi.where(),
        validate_host=True,
    )


@contextmanager
def abrir_bd():
    """Abre y cierra cursor/conexion; deshace cambios si ocurre un error."""
    conn = getConnection()
    cursor = None
    try:
        cursor = conn.cursor()
        try:
            yield conn, cursor
        except Exception:
            conn.rollback()
            raise
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            conn.close()


def producto_a_diccionario(row):
    # Se usan indices para que funcione independientemente del tipo de fila.
    return {
        "id": int(row[0]),
        "name": row[1],
        "price": float(row[2]),
        "stock": int(row[3]),
        "version": int(row[4]),
    }


def validar_producto(data, requiere_id=False, requiere_version=False):
    """Devuelve (datos_limpios, error). La BD usa 'names'; el JSON usa 'name'."""
    if not isinstance(data, dict):
        return None, "Debes enviar un objeto JSON valido."

    campos = ["name", "price", "stock"]
    if requiere_id:
        campos.append("id")
    if requiere_version:
        campos.append("version")

    faltantes = [campo for campo in campos if campo not in data]
    if faltantes:
        return None, "Faltan campos: " + ", ".join(faltantes)

    name = data["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        return None, "name debe ser un texto de 1 a 100 caracteres."

    if isinstance(data["price"], bool):
        return None, "price debe ser un numero."
    try:
        price = Decimal(str(data["price"]))
        if (
            not price.is_finite()
            or price < 0
            or price > Decimal("99999999.99")
            or price != price.quantize(Decimal("0.01"))
        ):
            return None, "price debe ser positivo, con maximo 2 decimales."
    except (InvalidOperation, ValueError, TypeError):
        return None, "price debe ser un numero valido."

    stock = data["stock"]
    if isinstance(stock, bool) or not isinstance(stock, int) or not 0 <= stock <= 2147483647:
        return None, "stock debe ser un entero no negativo."

    datos = {"name": name.strip(), "price": price, "stock": stock}

    if requiere_id:
        product_id = data["id"]
        if (
            isinstance(product_id, bool)
            or not isinstance(product_id, int)
            or not 1 <= product_id <= 2147483647
        ):
            return None, "id debe ser un entero positivo."
        datos["id"] = product_id

    if requiere_version:
        version = data["version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or not 1 <= version < 2147483647
        ):
            return None, "version debe ser un entero positivo que pueda incrementarse."
        datos["version"] = version

    return datos, None


def error_de_bd():
    app.logger.exception("Error en la API o al acceder a Azure SQL")
    return jsonify({"mensaje": "Error interno de la aplicacion o la base de datos."}), 500


# GET: listar los productos
@app.route("/api/products", methods=["GET"])
def get_products():
    try:
        with abrir_bd() as (_, cursor):
            cursor.execute(
                "SELECT id, names, price, stock, version FROM Products ORDER BY id"
            )
            products = [producto_a_diccionario(row) for row in cursor.fetchall()]
        return jsonify(products), 200
    except Exception:
        return error_de_bd()


# GET: consultar un producto por id
@app.route("/api/products/<int:product_id>", methods=["GET"])
def get_product(product_id):
    try:
        with abrir_bd() as (_, cursor):
            cursor.execute(
                "SELECT id, names, price, stock, version FROM Products WHERE id = %s",
                (product_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return jsonify({"mensaje": "Producto no encontrado"}), 404
            product = producto_a_diccionario(row)
        return jsonify(product), 200
    except Exception:
        return error_de_bd()


# GET: verificar la conexion a la base de datos
@app.route("/test-db", methods=["GET"])
def test_db():
    try:
        with abrir_bd() as (_, cursor):
            cursor.execute("SELECT DB_NAME()")
            database = cursor.fetchone()[0]
        return jsonify({"mensaje": "Conexion exitosa", "base_datos": database}), 200
    except Exception:
        return error_de_bd()


# POST: insertar un producto sin procedimientos almacenados
@app.route("/api/products", methods=["POST"])
def create_product():
    datos, error = validar_producto(request.get_json(silent=True))
    if error:
        return jsonify({"mensaje": error}), 400

    try:
        with abrir_bd() as (conn, cursor):
            cursor.execute(
                "INSERT INTO Products (names, price, stock) VALUES (%s, %s, %s)",
                (datos["name"], datos["price"], datos["stock"]),
            )
            conn.commit()
        return jsonify({"mensaje": "Producto creado exitosamente"}), 201
    except Exception:
        return error_de_bd()


# PUT: sin bloqueo local (puede haber sobrescrituras entre solicitudes)
@app.route("/api/products/no_lock", methods=["PUT"])
def update_product():
    datos, error = validar_producto(request.get_json(silent=True), requiere_id=True)
    if error:
        return jsonify({"mensaje": error}), 400

    try:
        with abrir_bd() as (conn, cursor):
            # Retraso intencional para demostrar concurrencia en la practica.
            time.sleep(5)
            cursor.execute(
                """
                UPDATE Products
                SET names = %s, price = %s, stock = %s,
                    version = version + 1
                WHERE id = %s
                """,
                (datos["name"], datos["price"], datos["stock"], datos["id"]),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return jsonify({"mensaje": "Producto no encontrado"}), 404
            conn.commit()

        return jsonify({"mensaje": "Producto actualizado sin bloqueo"}), 200
    except Exception:
        return error_de_bd()


# PUT: bloqueo local de hilos en un solo proceso Python
@app.route("/api/products/lock", methods=["PUT"])
def update_lock():
    datos, error = validar_producto(request.get_json(silent=True), requiere_id=True)
    if error:
        return jsonify({"mensaje": error}), 400

    try:
        with product_lock:
            print("Entra al bloqueo", flush=True)
            try:
                with abrir_bd() as (conn, cursor):
                    time.sleep(5)
                    cursor.execute(
                        """
                        UPDATE Products
                        SET names = %s, price = %s, stock = %s,
                            version = version + 1
                        WHERE id = %s
                        """,
                        (datos["name"], datos["price"], datos["stock"], datos["id"]),
                    )
                    if cursor.rowcount == 0:
                        conn.rollback()
                        return jsonify({"mensaje": "Producto no encontrado"}), 404
                    conn.commit()
            finally:
                print("Sale del bloqueo", flush=True)

        return jsonify({"mensaje": "Producto actualizado con bloqueo"}), 200
    except Exception:
        return error_de_bd()


# PUT: control de concurrencia optimista mediante version INT
@app.route("/api/products/update_version", methods=["PUT"])
def update_version():
    datos, error = validar_producto(
        request.get_json(silent=True), requiere_id=True, requiere_version=True
    )
    if error:
        return jsonify({"mensaje": error}), 400

    try:
        with abrir_bd() as (conn, cursor):
            cursor.execute(
                """
                UPDATE Products
                SET names = %s, price = %s, stock = %s,
                    version = version + 1
                WHERE id = %s AND version = %s
                """,
                (
                    datos["name"], datos["price"], datos["stock"],
                    datos["id"], datos["version"],
                ),
            )
            if cursor.rowcount == 0:
                cursor.execute("SELECT 1 FROM Products WHERE id = %s", (datos["id"],))
                existe = cursor.fetchone() is not None
                conn.rollback()
                if not existe:
                    return jsonify({"mensaje": "Producto no encontrado"}), 404
                return jsonify({
                    "mensaje": "Conflicto: la version cambio. Consulta el producto de nuevo."
                }), 409
            conn.commit()

        return jsonify({
            "mensaje": "Producto actualizado con control de version",
            "version": datos["version"] + 1,
        }), 200
    except Exception:
        return error_de_bd()


if __name__ == "__main__":
    # Para pruebas locales. Render ejecuta Gunicorn: app:app.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False, threaded=True)
