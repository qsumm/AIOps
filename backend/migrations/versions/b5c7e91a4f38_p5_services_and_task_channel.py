"""P5：服务级监控与任务通道（v0.4，FR-408/409/411）

新增五张表：
  services     服务对象（主机 × 服务 × 类型 × 状态 × 指标快照）
  agent_tasks  平台→Agent 受控任务（log_fetch 取证 / svc_action 恢复）
  service_heals 自愈计量（7 天窗口计数，频繁自愈视为缺陷信号）
  heal_guards   自愈熔断状态（连续失败即停 + 每小时限额）

Revision ID: b5c7e91a4f38
Revises: e7c41b9a2d38
"""
from alembic import op

revision = "b5c7e91a4f38"
down_revision = "c3f0a74b9e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id            BIGINT      NOT NULL AUTO_INCREMENT,
            host_id       BIGINT      NOT NULL,
            name          VARCHAR(64) NOT NULL,
            type          ENUM('mysql','redis','nginx','mq','app','other') NOT NULL DEFAULT 'app',
            spec_json     JSON        NULL,
            status        ENUM('up','degraded','down','unknown') NOT NULL DEFAULT 'unknown',
            metrics_json  JSON        NULL,
            note          VARCHAR(255) NOT NULL DEFAULT '',
            enabled       TINYINT(1)  NOT NULL DEFAULT 1,
            last_check_at DATETIME    NULL,
            created_at    DATETIME    DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            KEY idx_svc_host (host_id, name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id           BIGINT      NOT NULL AUTO_INCREMENT,
            host_id      BIGINT      NOT NULL,
            -- ⚠ SeekDB 实测约束（勿改回 ENUM）：ENUM 列**不能出现在复合索引里**——
            --   `WHERE host_id=? AND status='picked'` 走 idx_task_pick 时报
            --   (1210, 'Invalid argument')；已验证 VARCHAR 同样场景正常。
            --   本表 status/kind 高频参与复合索引查询，故一律用 VARCHAR。
            kind         VARCHAR(16) NOT NULL,
            payload_json JSON        NULL,
            status       VARCHAR(16) NOT NULL DEFAULT 'pending',
            result_json  JSON        NULL,
            error        TEXT        NULL,
            origin       VARCHAR(64) NOT NULL DEFAULT '',
            created_by   VARCHAR(64) NOT NULL DEFAULT '',
            created_at   DATETIME    DEFAULT CURRENT_TIMESTAMP,
            picked_at    DATETIME    NULL,
            done_at      DATETIME    NULL,
            expires_at   DATETIME    NULL,
            PRIMARY KEY (id),
            KEY idx_task_host (host_id),
            KEY idx_task_pick (host_id, status, id),
            KEY idx_task_created (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS service_heals (
            id         BIGINT      NOT NULL AUTO_INCREMENT,
            service_id BIGINT      NOT NULL,
            host_ident VARCHAR(64) NOT NULL DEFAULT '',
            action     VARCHAR(32) NOT NULL DEFAULT '',
            ok         TINYINT(1)  NOT NULL DEFAULT 0,
            detail     VARCHAR(255) NOT NULL DEFAULT '',
            task_id    BIGINT      NULL,
            created_at DATETIME    DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            KEY idx_heal_svc (service_id, created_at),
            KEY idx_heal_time (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS heal_guards (
            id                 BIGINT      NOT NULL AUTO_INCREMENT,
            service_id         BIGINT      NOT NULL,
            fail_streak        INT         NOT NULL DEFAULT 0,
            tripped            TINYINT(1)  NOT NULL DEFAULT 0,
            tripped_reason     VARCHAR(255) NOT NULL DEFAULT '',
            last_action_at     DATETIME    NULL,
            hour_window_start  DATETIME    NULL,
            hour_count         INT         NOT NULL DEFAULT 0,
            updated_at         DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_guard_svc (service_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def downgrade() -> None:
    for t in ("heal_guards", "service_heals", "agent_tasks", "services"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
