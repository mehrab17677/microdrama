"""Weekly: deactivate learned rules with hits >= 20 and wins / hits < 0.5 (guide 11.6).  Run: python -m scripts.retire_rules"""
from app.stages import learning


def main() -> int:
    n = learning.retire_rules()
    print(f"retired {n} rule(s)")
    return n


if __name__ == "__main__":
    main()
