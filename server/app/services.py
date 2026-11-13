"""Lógica de negocio: APKs, QR de aprovisionamiento, render de políticas, auditoría."""
import base64
import hashlib
import io
import json
import os
import uuid
from pathlib import Path

import qrcode
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import App, AuditLog, Policy
from .schemas import POLICY_MODELS
from .security import pin_hash

AGENT_PACKAGE = "com.adipack.mdm.agent"
AGENT_ADMIN_COMPONENT = f"{AGENT_PACKAGE}/{AGENT_PACKAGE}.AdminReceiver"


def audit(db: Session, actor: str, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail[:4000]))


# ---------------------------------------------------------------- APKs
class ApkError(ValueError):
    pass


def read_apk_info(path: Path) -> dict:
    """Lee paquete, versión y nombre del APK y CIERRA el archivo.

    Importante en Windows: pyaxmlparser deja el ZIP abierto; si no se cierra, Windows impide
    mover el archivo ("está siendo utilizado por otro proceso") y la subida fallaba.
    """
    from pyaxmlparser import APK  # import diferido: es pesado

    apk = None
    try:
        apk = APK(str(path))
        package = apk.package or ""
        version_code = int(apk.version_code or 0)
        version_name = apk.version_name or ""
        try:
            label = apk.application or package
        except Exception:  # noqa: BLE001 - recursos ofuscados: usamos el paquete
            label = package
        return {"package": package, "version_code": version_code, "version_name": str(version_name),
                "label": str(label or package)}
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de lectura = APK inválido
        raise ApkError("El archivo no es un APK válido de Android (¿está dañado o es otro tipo de archivo?).") from exc
    finally:
        zf = getattr(apk, "zip", None)
        if zf is not None:
            try:
                zf.close()
            except Exception:  # noqa: BLE001
                pass
        del apk


def store_apk(db: Session, upload_stream, original_name: str, is_agent: bool = False) -> App:
    """Guarda el APK, calcula SHA-256 y extrae paquete/versión del manifiesto."""
    settings = get_settings()
    settings.apk_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.apk_dir / f".upload-{uuid.uuid4().hex}.apk"
    h = hashlib.sha256()
    size = 0
    limit = settings.max_upload_mb * 1024 * 1024
    try:
        with tmp.open("wb") as out:
            while chunk := upload_stream.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ApkError(f"El archivo supera el máximo de {settings.max_upload_mb} MB.")
                h.update(chunk)
                out.write(chunk)
        if size == 0:
            raise ApkError("El archivo está vacío.")
        info = read_apk_info(tmp)
        package = info["package"]
        if not package:
            raise ApkError("El APK no declara nombre de paquete.")
        # El agente se reconoce solo por su paquete: no depende de marcar la casilla.
        if package == AGENT_PACKAGE:
            is_agent = True
        elif is_agent:
            raise ApkError(f"Marcaste «Es el agente MDM», pero este APK es {package}. "
                           f"El agente es {AGENT_PACKAGE}. Desmarca la casilla para subirlo como aplicación.")

        digest = h.hexdigest()
        existing = db.scalar(select(App).where(App.sha256 == digest))
        if existing:
            raise ApkError(f"Este APK ya está cargado: {existing.label} versión {existing.version_name} "
                           f"(código {existing.version_code}).")
        same_version = db.scalar(select(App).where(App.package_name == package,
                                                   App.version_code == info["version_code"]))
        if same_version:
            raise ApkError(f"Ya existe {package} con el mismo código de versión {info['version_code']} pero con "
                           "contenido distinto. Sube la nueva versión con un versionCode mayor.")
        final_name = f"{package}-{info['version_code']}-{digest[:8]}.apk"
        os.replace(tmp, settings.apk_dir / final_name)
        app = App(
            package_name=package[:255], label=info["label"][:255], version_name=info["version_name"][:64],
            version_code=info["version_code"], filename=final_name, sha256=digest, size=size, is_agent=is_agent,
        )
        db.add(app)
        db.flush()
        return app
    finally:
        tmp.unlink(missing_ok=True)


def apk_path(app: App) -> Path:
    return get_settings().apk_dir / app.filename


def latest_agent_apk(db: Session) -> App | None:
    return db.scalar(select(App).where(App.is_agent.is_(True)).order_by(App.version_code.desc(), App.id.desc()))


# ---------------------------------------------------------------- QR Android Enterprise
def provisioning_payload(db: Session, enrollment_token: str, policy: Policy) -> dict:
    """QR estándar de Android (7+): restablecer de fábrica, tocar 6 veces la pantalla de bienvenida."""
    settings = get_settings()
    agent = latest_agent_apk(db)
    if agent is None:
        raise ApkError("Primero sube el APK del agente (pestaña Apps > 'Es el agente MDM')")
    raw = bytes.fromhex(agent.sha256)
    checksum = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    base = settings.public_url.rstrip("/")
    payload = {
        "android.app.extra.PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME": AGENT_ADMIN_COMPONENT,
        "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_DOWNLOAD_LOCATION": f"{base}/agent/android.apk",
        "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_CHECKSUM": checksum,
        "android.app.extra.PROVISIONING_LEAVE_ALL_SYSTEM_APPS_ENABLED": True,
        "android.app.extra.PROVISIONING_SKIP_EDUCATION_SCREENS": True,
        "android.app.extra.PROVISIONING_TIME_ZONE": settings.time_zone,
        "android.app.extra.PROVISIONING_LOCALE": "es_VE",
        "android.app.extra.PROVISIONING_ADMIN_EXTRAS_BUNDLE": {
            "server_url": base,
            "enrollment_token": enrollment_token,
        },
    }
    s = policy.settings or {}
    if s.get("wifi_ssid"):
        payload["android.app.extra.PROVISIONING_WIFI_SSID"] = s["wifi_ssid"]
        payload["android.app.extra.PROVISIONING_WIFI_SECURITY_TYPE"] = s.get("wifi_security", "WPA")
        if s.get("wifi_password"):
            payload["android.app.extra.PROVISIONING_WIFI_PASSWORD"] = s["wifi_password"]
    return payload


def qr_png(data: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- políticas
def validate_policy_settings(platform: str, data: dict) -> dict:
    model = POLICY_MODELS[platform]
    return model.model_validate(data).model_dump()


def render_policy_for_agent(db: Session, policy: Policy) -> dict:
    """Lo que recibe el agente. Nunca enviamos el PIN en claro."""
    s = dict(policy.settings or {})
    s["version"] = policy.version
    base = get_settings().public_url.rstrip("/")
    if policy.platform == "android":
        pin = s.pop("kiosk_exit_pin", "")
        salt = f"adipack-{policy.id}-{policy.version}"
        s["kiosk_exit_pin_salt"] = salt
        s["kiosk_exit_pin_hash"] = pin_hash(pin, salt) if pin else ""
        apps = []
        ids = s.pop("required_app_ids", [])
        if ids:
            for app in db.scalars(select(App).where(App.id.in_(ids))):
                apps.append({
                    "id": app.id, "package": app.package_name, "version_code": app.version_code,
                    "sha256": app.sha256, "url": f"{base}/api/v1/apps/{app.id}/download",
                })
        s["required_apps"] = apps
    return s


def policy_settings_json(policy: Policy) -> str:
    return json.dumps(policy.settings or {}, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- diagnóstico "¿estoy listo para el QR?"
def setup_checks(db: Session, policy: Policy | None = None) -> list[dict]:
    """Lista de verificación en lenguaje simple. ok=True verde, False rojo, None aviso (no bloquea)."""
    from urllib.parse import urlparse

    settings = get_settings()
    host = urlparse(settings.public_url).hostname or ""
    checks = []
    local = host in ("127.0.0.1", "localhost", "0.0.0.0", "")
    checks.append({
        "ok": not local,
        "title": f"Dirección del servidor: {settings.public_url}",
        "help": "Es una dirección local: las tablets no podrán llegar. Conecta esta PC a la red de planta "
                "y reinicia el MDM, o define MDM_PUBLIC_URL en .env." if local else
                "Las tablets deben estar en la misma red Wi-Fi/LAN que esta PC. Recomendado: IP fija para esta PC.",
    })
    agent = latest_agent_apk(db)
    checks.append({
        "ok": agent is not None,
        "title": "APK del agente MDM cargado" if agent else "Falta el APK del agente MDM",
        "help": f"Versión {agent.version_name} ({agent.version_code})." if agent else
                "Ve a Apps, sube el APK compilado del agente y marca 'Es el agente MDM'. Sin esto el QR no funciona.",
    })
    if policy is not None and policy.platform == "android":
        st = policy.settings or {}
        pkg = st.get("kiosk_package", "")
        ids = st.get("required_app_ids", [])
        apps = list(db.scalars(select(App).where(App.id.in_(ids)))) if ids else []
        has_kiosk_app = any(a.package_name == pkg for a in apps)
        if st.get("kiosk_enabled"):
            checks.append({
                "ok": has_kiosk_app,
                "title": f"App del kiosco ({pkg}) incluida en 'Apps obligatorias'" if has_kiosk_app
                         else f"La app del kiosco ({pkg}) no está en 'Apps obligatorias'",
                "help": "Se instalará sola al activar la tablet." if has_kiosk_app else
                        "Sube el APK del MES/CMMS en Apps y márcalo en la política; si no, la tablet quedará esperando la app.",
            })
        checks.append({
            "ok": True if st.get("wifi_ssid") else None,
            "title": f"Wi-Fi en el QR: {st['wifi_ssid']}" if st.get("wifi_ssid") else "La política no tiene Wi-Fi",
            "help": "La tablet se conecta sola al escanear." if st.get("wifi_ssid") else
                    "Recomendado: pon SSID y clave en la política. Si no, tendrás que elegir la red a mano en la tablet.",
        })
        checks.append({
            "ok": True if st.get("kiosk_exit_pin") else None,
            "title": "PIN de salida del kiosco configurado" if st.get("kiosk_exit_pin") else "Sin PIN de salida",
            "help": "Tocando 7 veces el logo ADIPACK se pide el PIN." if st.get("kiosk_exit_pin") else
                    "Sin PIN sólo podrás salir del kiosco desde la consola (comando 'Salir del kiosco').",
        })
    return checks
