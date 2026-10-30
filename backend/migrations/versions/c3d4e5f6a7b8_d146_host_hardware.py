"""D146：主机硬件配置（主机详情要能一眼看出「几核几 G」）。

Agent 随心跳上报 hostinfo（CPU 逻辑/物理核数、CPU 型号、内存字节数、开机时间、
内核与架构），平台落到 hosts 表的静态列上，详情页直接展示。

为什么落表而不是只存在内存快照里：硬件配置**很少变**，但运维在告警详情里
反复要看（OOM？CPU 打满？规格够不够？）；落表后历史与导出都能用，且不占快照内存。

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("cpu_cores", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("hosts", sa.Column("cpu_cores_physical", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("hosts", sa.Column("cpu_model", sa.String(length=160), nullable=False, server_default=""))
    op.add_column("hosts", sa.Column("mem_total", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("hosts", sa.Column("boot_time", sa.DATETIME(), nullable=True))
    op.add_column("hosts", sa.Column("hostinfo_at", sa.DATETIME(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "hostinfo_at")
    op.drop_column("hosts", "boot_time")
    op.drop_column("hosts", "mem_total")
    op.drop_column("hosts", "cpu_model")
    op.drop_column("hosts", "cpu_cores_physical")
    op.drop_column("hosts", "cpu_cores")
