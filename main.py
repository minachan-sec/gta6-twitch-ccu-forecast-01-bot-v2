"""Entrypoint: read every case in the input directory, write one forecast each.

Reads $MAKEFUN_INPUT_DIR (else /input), writes $MAKEFUN_OUTPUT_DIR (else
/output). A case that fails to parse still gets a valid fallback forecast,
never a crash and never a missing file: an invalid case ranks below every
valid entry and takes no prize, so robustness comes first.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from model import forecast, _flat_rows  # noqa: E402


def _fallback_rows(case):
    try:
        known = [k for k in (case or {}).get('known') or [] if k.get('hour') == 1]
        p1 = float(known[0]['viewers']) if known else 1.0
    except Exception:
        p1 = 1.0
    if not (p1 > 0):
        p1 = 1.0
    return _flat_rows(p1)


def run_case(case):
    try:
        rows = forecast(case)
    except Exception:
        rows = _fallback_rows(case)
    hours = []
    for i, q in enumerate(rows):
        # Defensive validation: exactly 5 finite quantiles, non-decreasing,
        # inside (0, 1e9]. The model already guarantees this; never trust it.
        vals = []
        prev = 0.0
        for x in list(q)[:5]:
            try:
                v = float(x)
            except Exception:
                v = 1.0
            if v != v or v in (float('inf'), float('-inf')) or v <= 0:
                v = 1.0
            v = min(v, 1e9)
            v = max(v, prev)
            vals.append(v)
            prev = v
        while len(vals) < 5:
            vals.append(vals[-1] if vals else 1.0)
        hours.append({'hour': i + 2, 'q': vals})
    return {'hours': hours}


def main():
    in_dir = os.environ.get('MAKEFUN_INPUT_DIR') or '/input'
    out_dir = os.environ.get('MAKEFUN_OUTPUT_DIR') or '/output'
    os.makedirs(out_dir, exist_ok=True)
    try:
        names = sorted(n for n in os.listdir(in_dir) if n.endswith('.in'))
    except Exception:
        return  # no readable input directory: nothing to forecast
    for name in names:
        case_id = name[:-3]
        try:
            with open(os.path.join(in_dir, name), 'r', encoding='utf-8') as f:
                case = json.load(f)
        except Exception:
            case = {}
        out = run_case(case)
        with open(os.path.join(out_dir, case_id + '.out'), 'w', encoding='utf-8') as f:
            json.dump(out, f)


if __name__ == '__main__':
    main()
