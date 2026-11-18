"""Query the frozen MiniMax prefill model without starting TFS or vLLM."""

import argparse
import json
from pathlib import Path

from . import MODEL_FILE, PrefillTimeModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=MODEL_FILE)
    parser.add_argument("hit", type=int)
    parser.add_argument("miss", type=int)
    args = parser.parse_args()
    try:
        model = PrefillTimeModel(args.model)
        result = model.estimate(args.hit, args.miss)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(dict(hit_tokens=args.hit, miss_tokens=args.miss,
                          compute_ms=result.compute_ms, compute_s=result.compute_s,
                          compute_time_source=result.source, model_sha256=model.model_sha256),
                     indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
