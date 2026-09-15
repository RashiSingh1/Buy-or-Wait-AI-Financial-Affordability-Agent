"""CLI for the deterministic Buy or Wait agent.

Development mode reads only ``sample_requests.csv``. Reading the evaluation
file is an explicit opt-in operation to prevent accidental access.
"""

from __future__ import annotations

import os
from pathlib import Path

if __package__:
    from .agent import load_dataset, run, write_output
else:
    from agent import load_dataset, run, write_output


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    dataset = root / "dataset"
    final = os.environ.get("RUN_FINAL_EVALUATION") == "1"
    if final:
        # This is the only code path that may read the evaluation requests.
        data = load_dataset(dataset, dataset / "requests.csv")
        output = root / "output.csv"
    else:
        data = load_dataset(dataset, dataset / "sample_requests.csv")
        output = root / "code" / "development_output.csv"
    write_output(output, run(data))
    print(f"Wrote {len(data.requests)} decisions to {output}")
    if not final:
        print("Development mode: requests.csv was not read. Set RUN_FINAL_EVALUATION=1 for final evaluation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
