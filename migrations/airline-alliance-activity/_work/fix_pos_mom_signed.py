"""
purpose: Fix the one-sided 'Pos MoM *' measures in Flight Activity.tmdl so they return the
         SIGNED month-over-month change in both directions instead of blanking out on any
         non-increase. Original form `IF([CM X]>[PM X], DIVIDE([CM X],[PM X])-1)` has no else,
         so a flat or declining month renders blank (the validator's "blank-negative MoM"
         finding). Rewrite the guard to `IF([PM X]<>0, DIVIDE([CM X],[PM X])-1)`: return the
         signed growth whenever a valid prior-month baseline exists, blank only when there is
         no prior month (avoids a spurious -100%).
usage:   python migrations/airline-alliance-activity/_work/fix_pos_mom_signed.py
"""

import re
from pathlib import Path

TMDL = Path(__file__).resolve().parents[1] / (
    "fabric/AirlineAllianceActivity.SemanticModel/definition/tables/Flight Activity.tmdl"
)

# Match only the one-sided guard: IF([CM <a>]>[PM <b>],  ->  IF([PM <b>]<>0,
# The DIVIDE([CM <a>],[PM <b>])-1 body is left untouched.
PATTERN = re.compile(r"IF\(\[CM [^\]]+\]>(\[PM [^\]]+\]),")
REPLACEMENT = r"IF(\1<>0,"

# The doc comments described the old one-sided behaviour ("BLANK when not growing").
COMMENT_PATTERN = re.compile(r"; BLANK when not growing\.")
COMMENT_REPLACEMENT = "; signed value in both directions (BLANK only when there is no prior-month baseline)."


def main() -> None:
    text = TMDL.read_text(encoding="utf-8")
    new_text, n = PATTERN.subn(REPLACEMENT, text)
    if n != 13:
        raise SystemExit(f"expected 13 measure replacements, made {n} - aborting without writing")
    new_text, c = COMMENT_PATTERN.subn(COMMENT_REPLACEMENT, new_text)
    if c != 13:
        raise SystemExit(f"expected 13 comment replacements, made {c} - aborting without writing")
    TMDL.write_text(new_text, encoding="utf-8")
    print(f"rewrote {n} 'Pos MoM *' measures to signed-both-directions form ({c} doc comments updated)")


if __name__ == "__main__":
    main()
