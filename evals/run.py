"""python -m evals.run  -- print offline metrics; exit 1 on any threshold regression."""

import json
import sys

from .harness import check, run

metrics, rows = run()
print(json.dumps(metrics, indent=2))
bad = check(metrics)
for line in bad:
    print("REGRESSION:", line, file=sys.stderr)
sys.exit(1 if bad else 0)
