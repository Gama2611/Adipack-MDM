"""API que consumen los agentes (Android y PC). Autenticación: Bearer <device_token>."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import App, Command, Device, EnrollmentToken
from .schemas import CheckinIn, CheckinOut, CommandOut, CommandResultIn, EnrollIn, EnrollOut
from .security import new_token, token_hash
from .services import apk_path, audit, latest_agent_apk, render_policy_for_agent

router = APIRouter()
RESEND_AFTER = timedelta(minutes=5)
MAX_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite devuelve datetimes "naive": los normalizamos a UTC.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def current_device(authorization: str = Header(""), db: Session = Depends(get_db)) -> Device:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Falta token de dispositivo")
    dev = db.scalar(select(Device).where(Device.token_hash == token_hash(authorization[7:].strip())))
    if dev is None or dev.retired:
        raise HTTPException(401, "Dispositivo no reconocido o retirado")
    return dev


@router.post("/api/v1/enroll", response_model=EnrollOut)
def enroll(body: EnrollIn, request: Request, db: Session = Depends(get_db)):
    et = db.scalar(select(EnrollmentToken).where(EnrollmentToken.token == body.enrollment_token))
    if et is None or et.revoked:
        raise HTTPException(403, "Token de enrolamiento inválido")
    if et.expires_at and _aware(et.expires_at) < _now():
        raise HTTPException(403, "Token de enrolamiento vencido")
    if et.max_uses and et.uses >= et.max_uses:
        raise HTTPException(403, "Token de enrolamiento agotado")
    if et.policy.platform != body.platform and not (
        et.policy.platform in {"windows", "linux"} and body.platform in {"windows", "linux"}
    ):
        raise HTTPException(400, f"Este token es para {et.policy.platform}, no para {body.platform}")

    token = new_token()
    # Re-enrolamiento del mismo hardware: reutilizamos el registro (conserva nombre/ubicación).
    dev = db.scalar(
        select(Device).where(Device.hardware_id == body.hardware_id, Device.platform == body.platform)
    )
    if dev is None:
        count = db.scalar(select(func.count(Device.id)).where(Device.name.like(f"{et.name_prefix}-%"))) or 0
        dev = Device(name=body.name or f"{et.name_prefix}-{count + 1:02d}", platform=body.platform,
                     hardware_id=body.hardware_id, token_hash="")
        db.add(dev)
    dev.token_hash = token_hash(token)
    dev.retired = False
    dev.policy_id = et.policy_id
    dev.manufacturer, dev.model, dev.serial = body.manufacturer, body.model, body.serial
    dev.os_version, dev.agent_version = body.os_version, body.agent_version
    dev.applied_policy_version = 0
    dev.enrolled_at = _now()
    dev.last_seen = _now()
    et.uses += 1
    db.flush()
    audit(db, f"device:{dev.id}", "enroll", f"{dev.name} {body.manufacturer} {body.model} desde {request.client.host if request.client else '?'}")
    db.commit()
    return EnrollOut(device_id=dev.id, device_token=token, name=dev.name)


@router.post("/api/v1/checkin", response_model=CheckinOut)
def checkin(body: CheckinIn, request: Request, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    now = _now()
    status = dict(body.status)
    status["remote_ip"] = request.client.host if request.client else ""
    # El inventario pesado (PC) llega cada N minutos: conservamos el último entre check-ins.
    for key in ("inventory", "apps"):
        if key not in status and key in (dev.status or {}):
            status[key] = dev.status[key]
    dev.status = status
    dev.last_seen = now
    dev.applied_policy_version = body.applied_policy_version
    if body.agent_version:
        dev.agent_version = body.agent_version
    if body.os_version:
        dev.os_version = body.os_version

    out_cmds: list[CommandOut] = []
    pending = db.scalars(
        select(Command).where(Command.device_id == dev.id, Command.state.in_(["pending", "sent"])).order_by(Command.id)
    ).all()
    for c in pending:
        if c.state == "sent" and _aware(c.sent_at) and now - _aware(c.sent_at) < RESEND_AFTER:
            continue  # ya entregado, esperando resultado
        if c.attempts >= MAX_ATTEMPTS:
            c.state, c.result, c.finished_at = "failed", "Sin respuesta del dispositivo tras 5 intentos", now
            continue
        c.state, c.sent_at, c.attempts = "sent", now, c.attempts + 1
        out_cmds.append(CommandOut(id=c.id, type=c.type, params=c.params or {}))

    policy = dev.policy
    rendered = None
    if policy is not None and body.applied_policy_version != policy.version:
        rendered = render_policy_for_agent(db, policy)
    db.commit()
    return CheckinOut(
        device_id=dev.id, name=dev.name, policy_version=policy.version if policy else 0,
        policy=rendered, commands=out_cmds, server_time=now.isoformat(),
    )


@router.post("/api/v1/commands/{command_id}/result")
def command_result(command_id: int, body: CommandResultIn, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    cmd = db.get(Command, command_id)
    if cmd is None or cmd.device_id != dev.id:
        raise HTTPException(404, "Comando no encontrado")
    if cmd.state in {"done", "failed", "cancelled"}:
        return {"ok": True, "state": cmd.state}  # idempotente: el agente puede reintentar
    cmd.state = "done" if body.success else "failed"
    cmd.result = body.output
    cmd.finished_at = _now()
    if cmd.type == "wipe" and body.success:
        dev.retired = True
    db.commit()
    return {"ok": True, "state": cmd.state}


@router.get("/api/v1/apps/{app_id}/download")
def download_app(app_id: int, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    app = db.get(App, app_id)
    if app is None or not apk_path(app).exists():
        raise HTTPException(404, "App no encontrada")
    return FileResponse(apk_path(app), media_type="application/vnd.android.package-archive",
                        filename=app.filename, headers={"X-SHA256": app.sha256})


@router.get("/agent/android.apk")
def agent_apk(db: Session = Depends(get_db)):
    """Descarga pública del agente: la usa el asistente de configuración de Android al leer el QR."""
    app = latest_agent_apk(db)
    if app is None or not apk_path(app).exists():
        raise HTTPException(404, "APK del agente no cargado")
    return FileResponse(apk_path(app), media_type="application/vnd.android.package-archive", filename="adipack-mdm-agent.apk")


@router.get("/healthz")
def healthz():
    return {"ok": True}
