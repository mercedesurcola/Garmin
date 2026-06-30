"""
CUSOL — Garmin Microservice
Recibe entrenamientos desde el módulo PT y los sube a Garmin Connect.

Endpoints:
  POST /auth          → primer login (email + password + mfa opcional)
  POST /send-workout  → subir y programar un entrenamiento
  GET  /health        → verificar que el servicio está vivo
"""

import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from garminconnect import Garmin, GarminConnectAuthenticationError

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── CORS ─────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Secret"
    return response

@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def handle_options(path):
    return jsonify({}), 200


# Clave secreta para que solo Ferozo pueda llamar a este servicio
API_SECRET = os.environ.get("API_SECRET", "cambiar-esta-clave-en-produccion")

# Directorio donde se guardan los tokens por alumno
TOKENS_DIR = os.environ.get("TOKENS_DIR", "/tmp/garmin_tokens")
os.makedirs(TOKENS_DIR, exist_ok=True)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def check_secret(req):
    """Verifica que la request viene de Ferozo."""
    secret = req.headers.get("X-API-Secret") or req.json.get("api_secret", "")
    return secret == API_SECRET


def token_path(alumno_id: str) -> str:
    """Ruta del archivo de tokens de un alumno (acepta ID numérico o email)."""
    import hashlib
    safe_id = "".join(c for c in str(alumno_id) if c.isalnum() or c == "_")[:40]
    hash_suffix = hashlib.md5(str(alumno_id).encode()).hexdigest()[:8]
    return os.path.join(TOKENS_DIR, f"alumno_{safe_id}_{hash_suffix}")


def get_client(alumno_id: str, email: str = None, password: str = None) -> Garmin:
    """
    Devuelve un cliente Garmin autenticado.
    Si hay tokens guardados los usa; si no, hace login con credenciales.
    """
    path = token_path(alumno_id)

    if os.path.exists(path):
        try:
            client = Garmin()
            client.client.load(path)
            logger.info("Tokens cargados para alumno %s", alumno_id)
            return client
        except Exception as e:
            logger.warning("Tokens inválidos para alumno %s: %s", alumno_id, e)

    # Sin tokens válidos: necesita credenciales
    if not email or not password:
        raise ValueError("Se requieren credenciales para el primer login")

    client = Garmin(email=email, password=password)
    client.login()
    client.client.dump(path)
    logger.info("Login exitoso y tokens guardados para alumno %s", alumno_id)
    return client


def _build_step_fuerza(ej: dict, step_order_start: int) -> tuple[dict, int]:
    """Arma un RepeatGroup de fuerza (series x reps x descanso). Devuelve (step, siguiente_step_order)."""
    step_order = step_order_start
    nombre   = ej.get("nombre", "Ejercicio")
    series   = int(ej.get("series", 3))
    reps     = ej.get("repeticiones", "10")
    peso     = str(ej.get("peso", ""))
    descanso = int(ej.get("descanso", 60))

    try:
        reps_num = int(str(reps).split("-")[0].strip())
    except (ValueError, IndexError):
        reps_num = 10

    desc_parts = [f"{series}x{reps}"]
    if peso:
        desc_parts.append(f"@ {peso}")
    desc = " ".join(desc_parts)

    exercise_step = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
        "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True},
        "endConditionValue": reps_num,
        "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
        "description": desc
    }
    step_order += 1
    repeat_steps = [exercise_step]

    if descanso > 0:
        recovery_step = {
            "type": "ExecutableStepDTO",
            "stepOrder": step_order,
            "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
            "endConditionValue": descanso,
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
        }
        repeat_steps.append(recovery_step)
        step_order += 1

    repeat_group = {
        "type": "RepeatGroupDTO",
        "stepOrder": step_order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
        "numberOfIterations": series,
        "workoutSteps": repeat_steps,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
        "endConditionValue": float(series),
        "smartRepeat": False
    }
    step_order += 1
    return repeat_group, step_order


# Zona de esfuerzo (1=suave..4=máximo, del PT) → zona de FC de Garmin (1-5).
# Dejamos un margen: suave→Z2, medio→Z3, rápido→Z4, máximo→Z5.
ZONA_PT_A_GARMIN = {1: 2, 2: 3, 3: 4, 4: 5}

# stepType según el bloque de running elegido en el PT
RUN_BLOQUE_STEP_TYPE = {
    "calentamiento": {"stepTypeId": 1, "stepTypeKey": "warmup",  "displayOrder": 1},
    "trabajo":        {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "descanso":       {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "vuelta_calma":   {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
}


def _build_step_running(paso: dict, step_order_start: int) -> tuple[dict, int]:
    """
    Arma un step (o RepeatGroup si se repite) de running, por tiempo o distancia,
    con zona de FC opcional. Devuelve (step, siguiente_step_order).
    """
    step_order  = step_order_start
    nombre      = paso.get("run_nombre", "Tramo")
    bloque      = paso.get("run_bloque", "trabajo")
    modo        = paso.get("run_modo", "tiempo")          # 'tiempo' | 'distancia'
    valor       = float(paso.get("run_valor", 60))        # segundos si tiempo, metros si distancia
    zona_pt     = paso.get("run_zona_fc")                 # 1-4 (escala del PT) o None
    repeticiones= int(paso.get("run_repeticiones", 1) or 1)
    descanso    = int(paso.get("run_descanso_seg") or 0)

    step_type = RUN_BLOQUE_STEP_TYPE.get(bloque, RUN_BLOQUE_STEP_TYPE["trabajo"])

    if modo == "distancia":
        end_condition = {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}
        end_value = valor  # metros
    else:
        end_condition = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
        end_value = valor  # segundos

    if zona_pt and int(zona_pt) in ZONA_PT_A_GARMIN:
        target_type = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}
        zone_number = ZONA_PT_A_GARMIN[int(zona_pt)]
    else:
        target_type = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
        zone_number = None

    main_step = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": step_type,
        "endCondition": end_condition,
        "endConditionValue": end_value,
        "targetType": target_type,
        "description": nombre
    }
    if zone_number:
        main_step["zoneNumber"] = zone_number
    step_order += 1

    # Sin repeticiones múltiples: el step va suelto
    if repeticiones <= 1:
        return main_step, step_order

    # Con repeticiones (ej: 4x200m): RepeatGroup con el tramo + descanso adentro
    repeat_steps = [main_step]

    if descanso > 0:
        recovery_step = {
            "type": "ExecutableStepDTO",
            "stepOrder": step_order,
            "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
            "endConditionValue": descanso,
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
        }
        repeat_steps.append(recovery_step)
        step_order += 1

    repeat_group = {
        "type": "RepeatGroupDTO",
        "stepOrder": step_order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
        "numberOfIterations": repeticiones,
        "workoutSteps": repeat_steps,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
        "endConditionValue": float(repeticiones),
        "smartRepeat": False
    }
    step_order += 1
    return repeat_group, step_order


def build_workout_json(entrenamiento: dict) -> dict:
    """
    Convierte el formato de rutina del módulo PT al formato JSON de Garmin Connect.
    Soporta dos formatos de entrada:

    1) Solo fuerza (legacy, sigue funcionando igual):
       { "nombre": "...", "ejercicios": [ {nombre, series, repeticiones, peso, descanso}, ... ] }

    2) Mixto fuerza + running (nuevo):
       { "nombre": "...", "pasos": [
           {"tipo_paso":"fuerza", "nombre", "series", "repeticiones", "peso", "descanso"},
           {"tipo_paso":"running", "run_nombre", "run_bloque", "run_modo", "run_valor",
            "run_zona_fc", "run_repeticiones", "run_descanso_seg"},
           ...
         ] }

    El sportType del workout completo se decide por el primer paso de running
    presente (running); si no hay ninguno, queda como strength_training.
    """
    pasos = entrenamiento.get("pasos")
    if pasos is None:
        # Formato legacy: todo es fuerza
        pasos = [{"tipo_paso": "fuerza", **ej} for ej in entrenamiento.get("ejercicios", [])]

    hay_running = any(p.get("tipo_paso") == "running" for p in pasos)

    steps = []
    step_order = 1
    duracion_estimada = 0

    for paso in pasos:
        if paso.get("tipo_paso") == "running":
            step, step_order = _build_step_running(paso, step_order)
            steps.append(step)
            valor = float(paso.get("run_valor", 60))
            reps  = int(paso.get("run_repeticiones", 1) or 1)
            if paso.get("run_modo") == "distancia":
                # Estimación grosera: 6 min/km
                duracion_estimada += int(valor / 1000 * 360) * reps
            else:
                duracion_estimada += int(valor) * reps
            duracion_estimada += int(paso.get("run_descanso_seg") or 0) * max(reps - 1, 0)
        else:
            step, step_order = _build_step_fuerza(paso, step_order)
            steps.append(step)
            series   = int(paso.get("series", 3))
            descanso = int(paso.get("descanso", 60))
            duracion_estimada += series * (descanso + 5)

    sport_type = (
        {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
        if hay_running else
        {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 1}
    )

    return {
        "workoutName": entrenamiento.get("nombre", "Entrenamiento"),
        "description": entrenamiento.get("descripcion") or None,
        "sportType": sport_type,
        "estimatedDurationInSecs": duracion_estimada or 600,
        "author": {},
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport_type,
                "workoutSteps": steps
            }
        ]
    }

# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "cusol-garmin", "ts": datetime.utcnow().isoformat()})


@app.route("/auth", methods=["POST"])
def auth():
    """
    Primer login del alumno con sus credenciales de Garmin.
    Si tiene 2FA, Garmin envía el código y hay que llamar a este endpoint
    con el campo 'mfa_code' en una segunda request.

    Body: {
      "api_secret": "...",
      "alumno_id": 42,
      "email": "alumno@mail.com",
      "password": "su-password",
      "mfa_code": "123456"   ← opcional, solo si tiene 2FA
    }
    """
    if not check_secret(request):
        return jsonify({"error": "No autorizado"}), 401

    data       = request.json or {}
    alumno_id  = data.get("alumno_id")
    email      = data.get("email")
    password   = data.get("password")
    mfa_code   = data.get("mfa_code")  # Solo si tiene 2FA

    if not alumno_id or not email or not password:
        return jsonify({"error": "Faltan campos: alumno_id, email, password"}), 400

    try:
        path   = token_path(alumno_id)

        if mfa_code:
            # Segunda llamada: completar 2FA
            client = Garmin(email=email, password=password,
                            prompt_mfa=lambda: mfa_code)
        else:
            # Sin 2FA
            client = Garmin(email=email, password=password)

        client.login()
        client.client.dump(path)

        return jsonify({
            "ok": True,
            "mensaje": "Garmin conectado correctamente",
            "alumno_id": alumno_id
        })

    except GarminConnectAuthenticationError as e:
        msg = str(e)
        # Garmin pidió MFA pero no lo recibimos
        if "MFA" in msg.upper() or "2FA" in msg.upper() or "verification" in msg.lower():
            return jsonify({
                "ok": False,
                "requiere_mfa": True,
                "mensaje": "Garmin requiere código de verificación. Revisá tu email o app de autenticación."
            }), 200
        return jsonify({"error": f"Credenciales inválidas: {msg}"}), 401

    except Exception as e:
        logger.exception("Error en /auth para alumno %s", alumno_id)
        return jsonify({"error": str(e)}), 500


@app.route("/send-workout", methods=["POST"])
def send_workout():
    """
    Sube el entrenamiento a Garmin Connect y lo programa en la fecha indicada.

    Body: {
      "api_secret": "...",
      "alumno_id": 42,
      "fecha": "2026-06-28",
      "entrenamiento": {
        "nombre": "Rutina Fuerza A",
        "descripcion": "...",
        "ejercicios": [ ... ]
      }
    }
    """
    if not check_secret(request):
        return jsonify({"error": "No autorizado"}), 401

    data          = request.json or {}
    alumno_id     = data.get("alumno_id")
    fecha         = data.get("fecha")
    entrenamiento = data.get("entrenamiento")

    if not alumno_id or not fecha or not entrenamiento:
        return jsonify({"error": "Faltan campos: alumno_id, fecha, entrenamiento"}), 400

    # Validar formato de fecha
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Formato de fecha inválido. Usar YYYY-MM-DD"}), 400

    path = token_path(alumno_id)
    if not os.path.exists(path):
        return jsonify({
            "error": "Este alumno no tiene Garmin conectado",
            "requiere_auth": True
        }), 401

    try:
        client = get_client(alumno_id)

        # 1. Construir el JSON del workout
        workout_json = build_workout_json(entrenamiento)
        logger.info("Subiendo workout '%s' para alumno %s", entrenamiento.get("nombre"), alumno_id)

        # 2. Subir el workout a Garmin Connect
        resultado = client.upload_workout(workout_json)
        workout_id = resultado.get("detailId") or resultado.get("workoutId")

        if not workout_id:
            logger.error("Respuesta inesperada de Garmin: %s", resultado)
            return jsonify({"error": "Garmin no devolvió un ID de workout", "detalle": resultado}), 500

        # 3. Programarlo en el calendario del alumno
        client.schedule_workout(workout_id, fecha)
        logger.info("Workout %s programado para %s (alumno %s)", workout_id, fecha, alumno_id)

        return jsonify({
            "ok": True,
            "workout_id": workout_id,
            "fecha": fecha,
            "mensaje": f"Entrenamiento enviado a Garmin para el {fecha}"
        })

    except Exception as e:
        logger.exception("Error en /send-workout para alumno %s", alumno_id)
        return jsonify({"error": str(e)}), 500


@app.route("/disconnect", methods=["POST"])
def disconnect():
    """Elimina los tokens de un alumno (desconectar Garmin)."""
    if not check_secret(request):
        return jsonify({"error": "No autorizado"}), 401

    data      = request.json or {}
    alumno_id = data.get("alumno_id")

    if not alumno_id:
        return jsonify({"error": "Falta alumno_id"}), 400

    path = token_path(alumno_id)
    import shutil
    # El path puede ser directorio o archivo .json
    token_file = path if path.endswith('.json') else os.path.join(path, 'garmin_tokens.json')
    if os.path.exists(token_file):
        os.remove(token_file)
    elif os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)

    return jsonify({"ok": True, "mensaje": "Garmin desconectado"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
