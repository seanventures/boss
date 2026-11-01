"""NorthPeak Bank's synthetic loan data-quality world (the banking DQ case study).

`data` is the single source of truth; `rulebook` renders the Loan Data Quality Rulebook v3.2 PDF
from it. See tools/dq_sample/README.md for the API and the relationship names.
"""
from tools.dq_sample.data import (  # noqa: F401
    Dataset, answer_key, generate, grade,
)
