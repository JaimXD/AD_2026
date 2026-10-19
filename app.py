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

