"""X-MKT 템플릿·설치·키·MCP 카탈로그 · X-RPT 사용량·감사(추가 전용)·요율·인보이스·SIEM·보고서·이상 탐지.

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("mkt_templates", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(200), nullable=False),
                    sa.Column("version", sa.String(64), nullable=False), sa.Column("kind", sa.String(32), nullable=False), sa.Column("title", sa.String(200), nullable=False),
                    sa.Column("description", sa.Text(), nullable=False), sa.Column("unit_ids", sa.JSON(), nullable=False), sa.Column("layer", sa.String(16), nullable=False),
                    sa.Column("visibility", sa.String(16), nullable=False), sa.Column("tenant", sa.String(128), nullable=False), sa.Column("project", sa.String(128), nullable=False),
                    sa.Column("publisher", sa.String(128), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("replaced_by", sa.String(200)),
                    sa.Column("manifest", sa.JSON(), nullable=False), sa.Column("files", sa.JSON(), nullable=False), sa.Column("digest", sa.String(64), nullable=False),
                    sa.Column("signature", sa.JSON()), sa.Column("provenance", sa.JSON()), sa.Column("sbom", sa.JSON()), sa.Column("scan", sa.JSON(), nullable=False),
                    sa.Column("approval_id", sa.String(64)), sa.Column("created_at", ts, nullable=False), sa.Column("published_at", ts),
                    sa.UniqueConstraint("name", "version", name="uq_mkt_templates_nv"))
    op.create_table("mkt_installs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("template", sa.String(200), nullable=False),
                    sa.Column("version", sa.String(64), nullable=False), sa.Column("tenant", sa.String(128), nullable=False), sa.Column("project", sa.String(128), nullable=False),
                    sa.Column("params", sa.JSON(), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("target", sa.JSON(), nullable=False),
                    sa.Column("notified_version", sa.String(64)), sa.Column("history", sa.JSON(), nullable=False), sa.Column("created_at", ts, nullable=False),
                    sa.Column("updated_at", ts, nullable=False))
    op.create_index("ix_mkt_installs_tenant", "mkt_installs", ["tenant", "project"])
    op.create_table("mkt_keys", sa.Column("kid", sa.String(64), primary_key=True), sa.Column("public_key", sa.Text(), nullable=False),
                    sa.Column("owner", sa.String(128), nullable=False), sa.Column("source", sa.String(16), nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_table("mkt_mcp_catalog", sa.Column("name", sa.String(200), primary_key=True), sa.Column("version", sa.String(64), primary_key=True),
                    sa.Column("server_json", sa.JSON(), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("review", sa.JSON()),
                    sa.Column("synced_at", ts, nullable=False))

    op.create_table("rpt_usage", sa.Column("id", sa.String(36), primary_key=True), sa.Column("ts", ts, nullable=False), sa.Column("tenant", sa.String(128), nullable=False),
                    sa.Column("project", sa.String(128), nullable=False), sa.Column("unit_id", sa.String(64)), sa.Column("agent_id", sa.String(128)),
                    sa.Column("model", sa.String(128)), sa.Column("meter", sa.String(32), nullable=False), sa.Column("qty", sa.Float(), nullable=False),
                    sa.Column("cost_usd", sa.Float(), nullable=False), sa.Column("tags", sa.JSON(), nullable=False), sa.Column("source", sa.String(32), nullable=False),
                    sa.Column("source_id", sa.String(128), nullable=False), sa.UniqueConstraint("source", "source_id", "meter", name="uq_rpt_usage_src"))
    op.create_index("ix_rpt_usage_ts", "rpt_usage", ["tenant", "ts"])
    op.create_table("rpt_audit", sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=False), sa.Column("id", sa.String(36), nullable=False, unique=True),
                    sa.Column("ts", ts, nullable=False), sa.Column("tenant", sa.String(128), nullable=False), sa.Column("actor", sa.String(256), nullable=False),
                    sa.Column("actor_type", sa.String(16), nullable=False), sa.Column("action", sa.String(128), nullable=False), sa.Column("resource", sa.String(256), nullable=False),
                    sa.Column("result", sa.String(32), nullable=False), sa.Column("before", sa.JSON()), sa.Column("after", sa.JSON()), sa.Column("ip", sa.String(64), nullable=False),
                    sa.Column("source", sa.String(32), nullable=False), sa.Column("source_id", sa.String(128), nullable=False, unique=True),
                    sa.Column("prev_hash", sa.String(64), nullable=False), sa.Column("hash", sa.String(64), nullable=False))
    op.create_index("ix_rpt_audit_q", "rpt_audit", ["tenant", "ts"])
    # 추가 전용 (FR-X-RPT-04): 관리자 권한으로도 UPDATE·DELETE 불가 — 엔진별 트리거
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE FUNCTION rpt_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'rpt_audit is append-only'; END $$")
        op.execute("CREATE TRIGGER rpt_audit_no_update BEFORE UPDATE OR DELETE ON rpt_audit FOR EACH ROW EXECUTE FUNCTION rpt_audit_append_only()")
    else:
        op.execute("CREATE TRIGGER rpt_audit_no_update BEFORE UPDATE ON rpt_audit BEGIN SELECT RAISE(ABORT, 'rpt_audit is append-only'); END")
        op.execute("CREATE TRIGGER rpt_audit_no_delete BEFORE DELETE ON rpt_audit BEGIN SELECT RAISE(ABORT, 'rpt_audit is append-only'); END")
    op.create_table("rpt_rates", sa.Column("plan", sa.String(64), primary_key=True), sa.Column("meter", sa.String(32), primary_key=True),
                    sa.Column("unit_price", sa.Float(), nullable=False), sa.Column("markup_pct", sa.Float(), nullable=False), sa.Column("currency", sa.String(8), nullable=False))
    op.create_table("rpt_plans", sa.Column("tenant", sa.String(128), primary_key=True), sa.Column("project", sa.String(128), primary_key=True),
                    sa.Column("plan", sa.String(64), nullable=False), sa.Column("department", sa.String(128), nullable=False))
    op.create_table("rpt_invoices", sa.Column("id", sa.String(36), primary_key=True), sa.Column("month", sa.String(7), nullable=False),
                    sa.Column("tenant", sa.String(128), nullable=False), sa.Column("project", sa.String(128), nullable=False), sa.Column("plan", sa.String(64), nullable=False),
                    sa.Column("department", sa.String(128), nullable=False), sa.Column("lines", sa.JSON(), nullable=False), sa.Column("subtotal", sa.Float(), nullable=False),
                    sa.Column("markup", sa.Float(), nullable=False), sa.Column("total", sa.Float(), nullable=False), sa.Column("currency", sa.String(8), nullable=False),
                    sa.Column("status", sa.String(16), nullable=False), sa.Column("created_at", ts, nullable=False),
                    sa.UniqueConstraint("month", "tenant", "project", name="uq_rpt_invoice"))
    op.create_table("rpt_siem_sinks", sa.Column("id", sa.String(64), primary_key=True), sa.Column("type", sa.String(32), nullable=False), sa.Column("url", sa.Text(), nullable=False),
                    sa.Column("format", sa.String(16), nullable=False), sa.Column("secret", sa.String(64)), sa.Column("filter", sa.JSON(), nullable=False),
                    sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("cursor", sa.Integer(), nullable=False), sa.Column("sent", sa.Integer(), nullable=False),
                    sa.Column("failures", sa.Integer(), nullable=False), sa.Column("last_error", sa.Text()), sa.Column("last_sent_at", ts))
    op.create_table("rpt_reports", sa.Column("id", sa.String(36), primary_key=True), sa.Column("month", sa.String(7), nullable=False),
                    sa.Column("tenant", sa.String(128), nullable=False), sa.Column("project", sa.String(128), nullable=False), sa.Column("data", sa.JSON(), nullable=False),
                    sa.Column("html", sa.Text(), nullable=False), sa.Column("sent_to", sa.JSON(), nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_table("rpt_anomalies", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant", sa.String(128), nullable=False),
                    sa.Column("agent_id", sa.String(128), nullable=False), sa.Column("meter", sa.String(32), nullable=False), sa.Column("window_start", ts, nullable=False),
                    sa.Column("value", sa.Float(), nullable=False), sa.Column("mean", sa.Float(), nullable=False), sa.Column("std", sa.Float(), nullable=False),
                    sa.Column("z", sa.Float(), nullable=False), sa.Column("traces", sa.JSON(), nullable=False), sa.Column("created_at", ts, nullable=False),
                    sa.UniqueConstraint("tenant", "agent_id", "meter", "window_start", name="uq_rpt_anomaly"))


def downgrade() -> None:
    for t in ("rpt_anomalies", "rpt_reports", "rpt_siem_sinks", "rpt_invoices", "rpt_plans", "rpt_rates"):
        op.drop_table(t)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS rpt_audit_no_update ON rpt_audit")
        op.execute("DROP FUNCTION IF EXISTS rpt_audit_append_only()")
    op.drop_table("rpt_audit")
    op.drop_table("rpt_usage")
    for t in ("mkt_mcp_catalog", "mkt_keys", "mkt_installs", "mkt_templates"):
        op.drop_table(t)
