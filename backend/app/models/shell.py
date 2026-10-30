"""平台壳模型：users / audit_logs / licenses（04 §2 平台壳区）。"""
from datetime import datetime

from sqlalchemy import CHAR, DATETIME, BigInteger, Enum, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(Enum("admin", "member", "demo", name="user_role"), default="member")
    last_login: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    preferred_lang: Mapped[str] = mapped_column(String(8), default="zh", server_default="zh")   # 中英切换（D97）
    updated_at: Mapped[datetime] = mapped_column(DATETIME, nullable=True, onupdate=func.now())   # D171：配置可编辑，变更需可追溯


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(45), default="")
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), index=True)


class LicenseRow(Base):
    __tablename__ = "licenses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    license_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    machine_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    modules_json: Mapped[dict] = mapped_column(JSON, default=lambda: {"modules": ["ops"]})
    activated_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
