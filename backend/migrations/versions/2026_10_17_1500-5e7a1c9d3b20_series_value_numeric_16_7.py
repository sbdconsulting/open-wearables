"""data_point_series value precision increase to NUMERIC(16,7)

Latitude and longitude samples share this column. At NUMERIC(10,3) every
coordinate was rounded to 0.001 deg (~111 m north-south), so a GPS track came
back as a staircase. Scale 7 is ~1 cm and still leaves 9 integer digits, two
more than before, so no existing value can overflow.

Changing scale rewrites both tables. Rows already stored keep their rounded
value; a re-sync of the source overwrites them, because the bulk upsert updates
`value` whenever it differs.

Revision ID: 5e7a1c9d3b20
Revises: a7c3e9f1b2d4

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e7a1c9d3b20"
down_revision: Union[str, None] = "a7c3e9f1b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("data_point_series", "data_point_series_archive")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "value",
            existing_type=sa.Numeric(precision=10, scale=3),
            type_=sa.Numeric(precision=16, scale=7),
            existing_nullable=False,
        )


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "value",
            existing_type=sa.Numeric(precision=16, scale=7),
            type_=sa.Numeric(precision=10, scale=3),
            existing_nullable=False,
        )
