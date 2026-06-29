# cusol-garmin — Microservicio Garmin Connect

Microservicio Flask que conecta el módulo Personal Trainer de CUSOL con Garmin Connect.

## Deploy en Render (gratis)

1. Subí esta carpeta a un repositorio GitHub (puede ser privado)
2. Entrá a https://render.com y creá una cuenta gratuita
3. New → Web Service → conectá el repo
4. Render detecta Python automáticamente
5. En "Environment Variables" agregá:
   - `API_SECRET` → una clave larga y random (ej: generá con https://randomkeygen.com)
   - `TOKENS_DIR` → `/tmp/garmin_tokens`
6. Deploy → en 2 minutos tenés la URL del servicio

⚠️ Anotá bien el `API_SECRET` — lo vas a necesitar en el PHP de Ferozo.


API_SECRET
IZ3PiDLoetirkdwRSDd5OcRe

TOKENS_DIR
/tmp/garmin_tokens

## Endpoints

### GET /health
Verificar que el servicio está vivo.
```
curl https://tu-servicio.onrender.com/health

https://garmin-j6p2.onrender.com

```

### POST /auth
Conectar la cuenta Garmin de un alumno (primer login).
```json
{
  "api_secret": "tu-clave-secreta",
  "alumno_id": 42,
  "email": "alumno@gmail.com",
  "password": "su-password-garmin"
}

{
  "api_secret": "IZ3PiDLoetirkdwRSDd5OcRe",
  "alumno_id": 42,
  "email": "mercedesurcola@gmail.com",
  "password": "Sopap@2021"
}


```
Si el alumno tiene 2FA, la respuesta devuelve `"requiere_mfa": true`.
En ese caso, llamar de nuevo con el código recibido:
```json
{
  "api_secret": "tu-clave-secreta",
  "alumno_id": 42,
  "email": "alumno@gmail.com",
  "password": "su-password-garmin",
  "mfa_code": "123456"
}
```

### POST /send-workout
Enviar un entrenamiento al calendario Garmin del alumno.
```json
{
  "api_secret": "tu-clave-secreta",
  "alumno_id": 42,
  "fecha": "2026-06-30",
  "entrenamiento": {
    "nombre": "Rutina Fuerza A",
    "descripcion": "Día de empuje",
    "ejercicios": [
      {
        "nombre": "Press Banca",
        "series": 4,
        "repeticiones": "8-10",
        "peso": "70kg",
        "descanso": 90
      },
      {
        "nombre": "Fondos",
        "series": 3,
        "repeticiones": "12",
        "peso": "",
        "descanso": 60
      }
    ]
  }
}
```

### POST /disconnect
Desconectar Garmin de un alumno (borra sus tokens).
```json
{
  "api_secret": "tu-clave-secreta",
  "alumno_id": 42
}
```

## Notas importantes

- Los tokens se guardan en `/tmp/garmin_tokens/alumno_<id>/`
- En Render free tier el servicio duerme tras 15 min de inactividad
  → el primer request después puede tardar ~20 segundos (cold start)
- Los tokens duran ~1 año; cuando vencen el alumno tiene que reconectar
- ⚠️ Este servicio usa ingeniería inversa de la API privada de Garmin.
  Puede romperse si Garmin cambia sus endpoints sin aviso.
