"""Print which local / external models are available and the exact `ollama pull` commands that are missing.

    .venv\\Scripts\\python.exe scripts\\check_models.py [--profile hybrid] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default=None, help="profile to report for (default: settings.yaml / TPM_PROFILE)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    from tpm.config import load_settings
    from tpm.llm.router import ensure_models

    settings = load_settings(profile=args.profile)
    info = ensure_models(settings)
    if args.json:
        print(json.dumps(info, indent=1, ensure_ascii=False))
        return 0
    print("Trustworthy Process Monitor: model check")
    print(f"  profile             : {info['profile']} (external allowed: {info['allow_external']}, guard strict: {info['guard_strict']})")
    print(f"  ollama              : {'running' if info['ollama_running'] else 'NOT reachable'} at {settings.local_llm.base_url}")
    print(f"  configured model    : {info['configured_local_model']}")
    print(f"  selected model      : {info['local_model'] or '-'}")
    print(f"  pulled              : {', '.join(info['pulled_models']) or '-'}")
    print(f"  embeddings          : {info['embedding_model']} {'(pulled)' if info['embeddings'] else '(missing -> TF-IDF fallback)'}")
    print(f"  external            : {info['external_model']} key={'present' if info['external_key_present'] else 'absent'} usable={info['external']}")
    print(f"  mode                : {info['mode']}")
    print("  routing             : " + ", ".join(f"{k}={v}" for k, v in info["routing"].items()))
    print()
    print(info["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
