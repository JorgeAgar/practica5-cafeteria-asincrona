import base64
import binascii
import logging
import os
import random
import time
from collections import Counter
from datetime import datetime, timezone
from functools import cache

from flask import Flask, request
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cocina")

BASE_DATOS = os.environ.get("BASE_DATOS", "(default)")
TIEMPO_PREPARACION = float(os.environ.get("TIEMPO_PREPARACION", "6"))
PROBABILIDAD_FALLO = float(os.environ.get("PROBABILIDAD_FALLO", "0"))
PLATO_MISTERIOSO = "plato-misterioso"
ESTADOS_FINALES = ("listo", "rechazado")
# Con muchos pedidos del mismo producto a la vez, las transacciones chocan y
# Firestore las repite. Si aun así no alcanza, la entrega falla con 500 y
# Pub/Sub la reintenta, lo que es seguro porque la cocina es idempotente.
MAX_INTENTOS_TX = 15

app = Flask(__name__)


@cache
def db():
    return firestore.Client(database=BASE_DATOS)


def ahora():
    return datetime.now(timezone.utc)


@firestore.transactional
def marcar_preparando(transaccion, pedido_ref):
    """Devuelve el pedido como dict (o None si no existe) y lo marca como preparando si no ha terminado."""
    doc = pedido_ref.get(transaction=transaccion)
    if not doc.exists:
        return None
    pedido = doc.to_dict()
    if pedido["estado"] not in ESTADOS_FINALES:
        transaccion.update(pedido_ref, {"estado": "preparando", "actualizado": ahora()})
    return pedido


@firestore.transactional
def terminar(transaccion, pedido_ref, cantidades):
    """Revisa el estado y las existencias y descuenta en una sola transacción.

    Devuelve ("repetido" | "rechazado" | "listo", productos que faltan).
    """
    inventario = db().collection("inventario")
    # En una transacción todas las lecturas van antes que las escrituras.
    pedido = pedido_ref.get(transaction=transaccion)
    if pedido.get("estado") in ESTADOS_FINALES:
        return "repetido", []
    existencias = {
        producto: (inventario.document(producto).get(transaction=transaccion).get("existencias") or 0)
        for producto in cantidades
    }
    faltan = [p for p, cantidad in cantidades.items() if existencias[p] < cantidad]
    if faltan:
        transaccion.update(pedido_ref, {
            "estado": "rechazado",
            "motivo": "Se acabó: " + ", ".join(faltan),
            "actualizado": ahora(),
        })
        return "rechazado", faltan
    for producto, cantidad in cantidades.items():
        transaccion.update(inventario.document(producto), {"existencias": existencias[producto] - cantidad})
    transaccion.update(pedido_ref, {"estado": "listo", "actualizado": ahora()})
    return "listo", []


@app.post("/")
def recibir():
    # Pub/Sub entrega un sobre JSON: {"message": {"data": <base64>, ...},
    # "subscription": ..., "deliveryAttempt": n}. El último campo solo viene
    # cuando la suscripción tiene política de mensajes muertos.
    sobre = request.get_json(silent=True)
    try:
        pedido_id = base64.b64decode(sobre["message"]["data"], validate=True).decode()
    except (TypeError, KeyError, binascii.Error, UnicodeDecodeError):
        log.error("Llegó un sobre que no es de Pub/Sub o no trae data: %r", sobre)
        return "sobre inválido", 400

    intento = sobre.get("deliveryAttempt")
    log.info("Pedido %s, intento de entrega %s", pedido_id, intento or "desconocido")

    pedido_ref = db().collection("pedidos").document(pedido_id)

    # Primera transacción: pasa el pedido a "preparando" solo si todavía no está
    # terminado. Si es una entrega repetida de un pedido ya listo o rechazado, se
    # confirma sin tocar nada: hacerlo dos veces deja lo mismo que hacerlo una.
    pedido = marcar_preparando(db().transaction(), pedido_ref)
    if pedido is None:
        log.warning("El pedido %s no existe; se descarta el mensaje", pedido_id)
        return "", 204
    if pedido["estado"] in ESTADOS_FINALES:
        log.info("Pedido %s ya estaba %s: entrega repetida, no se descuenta otra vez",
                 pedido_id, pedido["estado"])
        return "", 204

    cantidades = Counter()
    for item in pedido["items"]:
        cantidades[item["producto"]] += item["cantidad"]

    if PLATO_MISTERIOSO in cantidades:
        raise RuntimeError(f"La cocina no tiene receta para el plato misterioso (pedido {pedido_id})")

    time.sleep(TIEMPO_PREPARACION)

    # Segunda transacción: vuelve a leer el estado, revisa las existencias y
    # descuenta, todo junto. Si otra entrega del mismo pedido (u otro pedido que
    # gasta el mismo producto) escribe en medio, Firestore aborta y repite la
    # transacción con los datos nuevos, así que nadie descuenta con una lectura vieja.
    resultado, faltan = terminar(db().transaction(max_attempts=MAX_INTENTOS_TX), pedido_ref, cantidades)
    if resultado == "repetido":
        log.info("Pedido %s lo terminó otra entrega mientras tanto; no se descuenta otra vez", pedido_id)
    elif resultado == "rechazado":
        log.info("Pedido %s rechazado, no alcanza: %s", pedido_id, faltan)
    else:
        log.info("Pedido %s listo", pedido_id)

    # Simula que la cocina se cae justo después de terminar y antes de confirmar.
    # Pub/Sub no recibe el 2xx y vuelve a entregar el mismo pedido.
    if random.random() < PROBABILIDAD_FALLO:
        log.warning("Falla simulada en el pedido %s: el trabajo quedó hecho pero no se confirma", pedido_id)
        return "falla simulada", 500

    return "", 204
