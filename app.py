"""API de productos para Azure SQL con Flask y python-tds.

Rutas:
    GET  /test-db
    GET  /api/products
    GET  /api/products/<id>
    POST /api/products
    PUT  /api/products/no_lock
    PUT  /api/products/lock
    PUT  /api/products/update_version
    POST /api/products/enviar-correo (inventario por Resend)

En Render configura DB_SERVER, DB_NAME, DB_USER y DB_PASSWORD,
así como RESEND_API_KEY, EMAIL_FROM, EMAIL_TO y EMAIL_TRIGGER_TOKEN.
La tabla Products debe tener id, names, price, stock y version (INT).
"""

import os
import threading
import time
import hmac
from html import escape
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import certifi
import pytds
import resend
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


# =========================================
# POST: ENVIAR INVENTARIO POR CORREO
# =========================================
# Por seguridad, este endpoint no acepta destinatarios arbitrarios.
# El receptor se define en Render mediante EMAIL_TO.
# Protege el envío con X-Email-Token: <EMAIL_TRIGGER_TOKEN>.

@app.route("/api/products/enviar-correo", methods=["POST"])
def enviar_inventario_por_correo():
    clave_resend = os.getenv("RESEND_API_KEY", "").strip()
    remitente = os.getenv("EMAIL_FROM", "").strip()
    destinatario = os.getenv("EMAIL_TO", "").strip()
    token_esperado = os.getenv("EMAIL_TRIGGER_TOKEN", "")

    if not all((clave_resend, remitente, destinatario, token_esperado)):
        app.logger.error("Configuracion de correo incompleta en Render")
        return jsonify({"mensaje": "Servicio de correo sin configurar."}), 503

    token_enviado = request.headers.get("X-Email-Token", "")
    if not hmac.compare_digest(token_enviado, token_esperado):
        return jsonify({"mensaje": "No autorizado."}), 401

    try:
        # Leer los datos directamente de Azure SQL (no del JSON del usuario).
        with abrir_bd() as (_, cursor):
            cursor.execute("SELECT names, stock FROM Products ORDER BY id")
            filas = cursor.fetchall()

        productos = [(str(fila[0]), int(fila[1])) for fila in filas]
        unidades = sum(cantidad for _, cantidad in productos)
        total_productos = len(productos)

        # Versión texto y HTML, con valores escapados.
        lineas = ["Reporte de inventario de App Distribuida", ""]
        html_filas = []
        for nombre, cantidad in productos:
            lineas.append(f"- {nombre}: {cantidad} unidades")
            html_filas.append(
                "<tr>"
                f"<td style='padding:8px;border:1px solid #ddd'>{escape(nombre)}</td>"
                f"<td style='padding:8px;border:1px solid #ddd'>{cantidad}</td>"
                "</tr>"
            )

        if not productos:
            lineas.append("No hay productos registrados.")

        lineas += ["", f"Productos diferentes: {total_productos}",
                   f"Unidades en inventario: {unidades}"]

        cuerpo_html = (
            "<h2>Reporte de inventario - App Distribuida</h2>"
            "<p>Productos consultados directamente desde Azure SQL:</p>"
            "<table style='border-collapse:collapse'>"
            "<tr><th style='padding:8px;border:1px solid #ddd'>Producto</th>"
            "<th style='padding:8px;border:1px solid #ddd'>Cantidad (stock)</th></tr>"
            + ("".join(html_filas) if html_filas else
               "<tr><td colspan='2'>No hay productos registrados</td></tr>")
            + "</table>"
            + f"<p><strong>Productos diferentes:</strong> {total_productos}<br>"
              f"<strong>Unidades totales:</strong> {unidades}</p>"
        )

    except Exception:
        # No intentar enviar información inventada si falla la BD.
        return error_de_bd()

    try:
        # La clave se guarda en Render, nunca en GitHub.
        resend.api_key = clave_resend
        respuesta = resend.Emails.send({
            "from": remitente,
            "to": [destinatario],
            "subject": "Reporte de productos e inventario",
            "html": cuerpo_html,
            "text": "\n".join(lineas),
        })
        mensaje_id = (
            respuesta.get("id") if isinstance(respuesta, dict)
            else getattr(respuesta, "id", None)
        )
        return jsonify({
            "mensaje": "Correo aceptado por Resend.",
            "id_resend": mensaje_id,
            "destinatario": destinatario,
            "productos": total_productos,
            "unidades": unidades,
        }), 200
    except Exception:
        app.logger.exception("No se pudo enviar el inventario a Resend")
        return jsonify({
            "mensaje": "No se pudo enviar el correo. Revisa los Logs de Render y Resend."
        }), 502



if __name__ == "__main__":
    # Para pruebas locales. Render ejecuta Gunicorn: app:app.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False, threaded=True)
