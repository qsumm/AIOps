"""D7-fix: incidents 索引首列改为 INT（SeekDB 不支持 ENUM 作为索引首列的等值查询）

问题（实测复现）：`incidents.status` 是 ENUM 且为复合索引 `idx_inc_status(status, severity)` 的首列，
任何等值过滤（`WHERE status = 'open'`，任意取值/大小写）都抛 `(1210, 'Invalid argument')`。

定位证据：
- 全库 25 个 ENUM 列审计：唯一失败的就是 incidents.status；
- 唯一带索引的对照列 `metric_agg.granularity` 位于复合索引第 3 位 → 正常；
- 删掉 idx_inc_status 后同一查询立即恢复（返回 33 行）。
结论：触发条件是 **ENUM 列作为索引首列 + 等值过滤**。

修复：改为 `idx_inc_severity(severity, created_at)`——INT 打头，且更贴合列表查询的
`ORDER BY severity, created_at DESC`。status 基数极低（3 值），无需索引。

见 04 §12（SeekDB 使用禁忌）。
"""
from alembic import op
import sqlalchemy as sa

revision = '13bddd4dad5e'
down_revision = 'a54322dcbfb6'
branch_labels = None
depends_on = None

_SQL_DROP = "ALTER TABLE incidents DROP INDEX idx_inc_status"
_SQL_ADD = "CREATE INDEX idx_inc_severity ON incidents (severity, created_at)"


def _index_exists(name: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(sa.text(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = 'incidents' AND index_name = :n"
    ), {"n": name}).scalar()
    return bool(row)


def upgrade() -> None:
    # 幂等：开发库在定位问题时已手工删过该索引
    if _index_exists("idx_inc_status"):
        op.execute(_SQL_DROP)
    if not _index_exists("idx_inc_severity"):
        op.execute(_SQL_ADD)


def downgrade() -> None:
    if _index_exists("idx_inc_severity"):
        op.execute("ALTER TABLE incidents DROP INDEX idx_inc_severity")
    if not _index_exists("idx_inc_status"):
        op.execute("CREATE INDEX idx_inc_status ON incidents (status, severity)")
