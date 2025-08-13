cat >app/emo_engine/persona/constants/labels.py<< 'EOF'
#app/emo_engine/persona/constants/labels.py
from typing import Dict
from .tone_map import Tone, TONE_MAP

EMO_LABEL_MAP: Dict[str, str] = {}

for key, tone in TONE_MAP.items():
    EMO_LABEL_MAP[key] = tone.name
    if key.endswith("_mod"):
        base_key = key[:-4]
        EMO_LABEL_MAP[base_key] = tone.name
EOF