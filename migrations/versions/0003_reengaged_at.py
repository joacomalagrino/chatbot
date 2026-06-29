"""add reengaged_at + reengage_opt_out to conversations

Agrega dos columnas a conversations para el re-engagement proactivo de leads fuera de
la ventana de 24h (services/reengage_service.py):

- reengaged_at: timestamp de cuándo se le mandó la plantilla de re-engagement. NULL =
  todavía no se re-enganchó. Da idempotencia: el selector excluye los que ya tienen
  valor, así una segunda corrida no vuelve a mandar al mismo lead.
- reengage_opt_out: el lead pidió no recibir más mensajes. NULL/False = se le puede
  escribir; True = nunca se lo re-engancha. Previsto para cablear el opt-out.

Ambas nullable: las conversaciones viejas quedan en NULL (reengaged_at NULL = elegible
para re-engagement; opt_out NULL = no optó por salir).

Revision ID: 0003_reengaged_at
Revises: 0002_last_inbound_at
Create Date: 2026-06-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# identificadores de revisión, usados por Alembic.
revision: str = '0003_reengaged_at'
down_revision: Union[str, None] = '0002_last_inbound_at'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    # Guardamos contra la columna ya existente: igual que 0002, una DB creada por
    # create_all con el modelo actual ya las tiene; sin el guard el ADD COLUMN fallaría
    # con "duplicate column". La adopción de prod (create_all con modelo viejo + stamp
    # baseline) sí entra acá y las crea.
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        if not _has_column('conversations', 'reengaged_at'):
            batch_op.add_column(sa.Column('reengaged_at', sa.DateTime(), nullable=True))
        if not _has_column('conversations', 'reengage_opt_out'):
            batch_op.add_column(sa.Column('reengage_opt_out', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        if _has_column('conversations', 'reengage_opt_out'):
            batch_op.drop_column('reengage_opt_out')
        if _has_column('conversations', 'reengaged_at'):
            batch_op.drop_column('reengaged_at')
