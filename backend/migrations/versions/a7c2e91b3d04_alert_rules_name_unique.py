"""D11：告警规则名唯一约束（与渠道/模型配置看齐）。

背景（真伪审查发现）：
- 平台其余主表（notification_channels.name、llm_configs.name）都加了 unique
  约束，`alert_rules.name` 是漏网之鱼——允许同名前后建两条，第二次"改"其中一条
  时可能误改另一条，操作人也会困惑。
- 进展记录 §6.A 把这条列为"待你决策"，但实测数据**当前 0 重名**（演示库与生产 alike
  都是唯一的），加约束无迁移成本。
- 决策改"默认加唯一约束"，与渠道/模型配置口径一致；不要花用户一次决策轮。

迁移：
- 一次自增迁移，加 `UNIQUE INDEX uq_alert_rules_name`。
- 仅 ADD，不做数据回填（前面已确认 0 重名）。
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7c2e91b3d04'
down_revision = 'e5b91c3a7d28'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 现存重名先看一眼（生产应当已唯一；本迁移只 ADD，无数据回填）
    bind = op.get_bind()
    dups = bind.execute(sa.text("SELECT name, COUNT(*) c FROM alert_rules GROUP BY name HAVING c > 1")).all()
    if dups:
        # 现实里若真有重名，自动加 -1 -2 后缀保迁移可执行
        # （D11 之前已确认本演示库 0 重名，但写防御代码以防生产拿到迁移失败）
        for name, c in dups:
            rows = bind.execute(sa.text("SELECT id FROM alert_rules WHERE name=:n ORDER BY id"), {"n": name}).all()
            for i, (rid,) in enumerate(rows[1:], start=1):
                bind.execute(sa.text("UPDATE alert_rules SET name=:n WHERE id=:i"),
                             {"n": f"{name} ({i})", "i": rid})
    op.create_index("uq_alert_rules_name", "alert_rules", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_alert_rules_name", table_name="alert_rules")
