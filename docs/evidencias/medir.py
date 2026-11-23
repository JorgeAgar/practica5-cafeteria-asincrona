# Uso: python3 -I medir.py <url_backend> <etiqueta>
# Repone inventario, lanza 60 pedidos a la vez (20 hilos) y espera a que todos terminen.
# Mide con los tiempos de Firestore: primer "creado" -> último "actualizado".
import json, subprocess, sys, time, urllib.request, concurrent.futures as cf, collections, datetime
B, etiqueta = sys.argv[1], sys.argv[2]
tok = subprocess.check_output(["/home/jorge/Downloads/google-cloud-sdk/bin/gcloud", "auth", "print-access-token"]).decode().strip()
FS = "https://firestore.googleapis.com/v1/projects/project-56eb9056-3ab4-4adb-b64/databases/jorge-adolfo-cafeteria/documents"
def post(url, body=None, h=None):
    r = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else b"", method="POST",
                               headers={"Content-Type": "application/json", **(h or {})})
    return json.load(urllib.request.urlopen(r, timeout=60))
post(B + "/inventario/reiniciar")
productos = ["tinto"] * 20 + ["cafe-con-leche"] * 12 + ["empanada"] * 10 + ["pastel-de-pollo"] * 6 + ["arepa-con-queso"] * 6 + ["jugo-natural"] * 6
with cf.ThreadPoolExecutor(20) as ex:
    ids = list(ex.map(lambda kp: post(B + "/pedidos", {"cliente": f"{etiqueta} {kp[0]}", "items": [{"producto": kp[1], "cantidad": 1}]})["id"], enumerate(productos)))
def leer():
    out = {}
    for i in range(0, len(ids), 50):
        r = post(FS + ":batchGet", {"documents": [f"projects/project-56eb9056-3ab4-4adb-b64/databases/jorge-adolfo-cafeteria/documents/pedidos/{x}" for x in ids[i:i+50]]}, {"Authorization": "Bearer " + tok})
        for d in r:
            f = d["found"]["fields"]; out[d["found"]["name"]] = (f["estado"]["stringValue"], f["creado"]["timestampValue"], f["actualizado"]["timestampValue"])
    return out
for _ in range(120):
    time.sleep(5)
    o = leer()
    if all(v[0] in ("listo", "rechazado") for v in o.values()): break
ts = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
ini = min(ts(v[1]) for v in o.values()); fins = sorted(ts(v[2]) for v in o.values())
c = collections.Counter(v[0] for v in o.values())
print(f"{etiqueta}: estados {dict(c)}  último listo a los {(fins[-1]-ini).total_seconds():.1f} s  mediana {(fins[len(fins)//2]-ini).total_seconds():.1f} s  inicio {ini:%H:%M:%S}", flush=True)
