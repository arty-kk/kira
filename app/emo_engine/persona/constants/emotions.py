cat >app/emo_engine/persona/constants/emotions.py<< EOF
#app/emo_engine/persona/constants/emotions.py
from typing import Dict, List, Callable

PRIMARY_COORDS = {
    "joy": (1.0, 0.0),
    "trust": (0.707, 0.707),
    "fear": (0.0, 1.0),
    "surprise": (-0.707, 0.707),
    "sadness": (-1.0, 0.0),
    "disgust": (-0.707, -0.707),
    "anger": (0.0, -1.0),
    "anticipation": (0.707, -0.707),
    "curiosity": (-0.866,-0.5),
    "sexual_arousal": (0.866, -0.5),
    "anxiety": (-0.5,  0.866),
    "stress": (-0.5, -0.866),
    "energy":  (0.6,   0.8),
    "fatigue": (-0.6, -0.8),
}

PRIMARY_EMOTIONS: list[str] = [
    "joy",
    "sadness",
    "anger",
    "fear",
    "disgust",
    "surprise",
    "stress",
    "anxiety",
    "anticipation",
    "trust",
    "curiosity",
    "sexual_arousal",
    "energy",
    "fatigue",
]

SECONDARY_EMOTIONS: dict[str, dict[str, Callable[[dict], float]]] = {
    "joy": {
        "optimism":     lambda s: 0.6*s["joy"] + 0.4*s["anticipation"],
        "ecstasy":      lambda s: 0.8*s["joy"] + 0.2*s["arousal"],
        "cheerfulness": lambda s: 0.7*s["joy"] + 0.3*s.get("friendliness", 0.5),
        "enthusiasm":   lambda s: 0.7*s["joy"] + 0.3*s["energy"],
    },
    "sadness": {
        "gloom":      lambda s: 0.7*s["sadness"] + 0.3*s["stress"],
        "despair":    lambda s: 0.8*s["sadness"] + 0.2*s["anxiety"],
        "loneliness": lambda s: 0.6*s["sadness"] + 0.4*s["trust"],
    },
    "anger": {
        "irritation": lambda s: 0.7*s["anger"] + 0.3*s["stress"],
        "rage":       lambda s: 0.9*s["anger"] + 0.1*s.get("aggressiveness", 0.5),
        "annoyance":  lambda s: 0.5*s["anger"] + 0.5*s["sarcasm"],
    },
    "fear": {
        "apprehension": lambda s: 0.6*s["fear"] + 0.4*s["anxiety"],
        "terror":       lambda s: 0.8*s["fear"] + 0.2*s["stress"],
        "panic":        lambda s: 0.7*s["fear"] + 0.3*s["arousal"],
    },
    "disgust": {
        "revulsion": lambda s: 0.8*s["disgust"] + 0.2*s["anger"],
        "contempt":   lambda s: 0.7*s["disgust"] + 0.3*s["sarcasm"],
        "loathing":   lambda s: 0.9*s["disgust"] + 0.1*s["anger"],
    },
    "surprise": {
        "amazement":    lambda s: 0.6*s["surprise"] + 0.4*s["joy"],
        "astonishment": lambda s: 0.7*s["surprise"] + 0.3*s["fear"],
        "startle":      lambda s: 0.5*s["surprise"] + 0.5*s["arousal"],
    },
    "trust": {
        "acceptance": lambda s: 0.7*s["trust"] + 0.3*s["joy"],
        "admiration": lambda s: 0.8*s["trust"] + 0.2*s["anticipation"],
        "reliance":   lambda s: 0.6*s["trust"] + 0.4*s["confidence"],
    },
    "anticipation": {
        "eagerness": lambda s: 0.7*s["anticipation"] + 0.3*s["joy"],
        "vigilance": lambda s: 0.6*s["anticipation"] + 0.4*s["fear"],
        "interest":  lambda s: 0.8*s["anticipation"] + 0.2*s["curiosity"],
    },
    "stress": {
        "tension":   lambda s: 0.7*s["stress"] + 0.3*s["arousal"],
        "overwhelm": lambda s: 0.8*s["stress"] + 0.2*s["anxiety"],
        "unease":    lambda s: 0.6*s["stress"] + 0.4*s["fear"],
    },
    "anxiety": {
        "worry":      lambda s: 0.7*s["anxiety"] + 0.3*s["fear"],
        "nervousness":lambda s: 0.6*s["anxiety"] + 0.4*s["arousal"],
        "dread":      lambda s: 0.8*s["anxiety"] + 0.2*s["sadness"],
    },
}
SECONDARY_KEYS = list(dict.fromkeys(
    key
    for submap in SECONDARY_EMOTIONS.values()
    for key in submap
))

TERTIARY_EMOTIONS: dict[str, dict[str, Callable[[dict], float]]] = {
    "optimism":     {"hope":      lambda s: 0.7*s["optimism"] + 0.3*s["anticipation"]},
    "ecstasy":      {"bliss":     lambda s: 0.8*s["ecstasy"] + 0.2*s["joy"]},
    "cheerfulness": {"merriment": lambda s: 0.6*s["cheerfulness"] + 0.4*s["energy"] if "energy" in s else s["cheerfulness"]},
    "gloom":        {"melancholy":lambda s: 0.7*s["gloom"] + 0.3*s["sadness"]},
    "despair":      {"distress":  lambda s: 0.8*s["despair"] + 0.2*s["sadness"]},
    "loneliness":   {"isolation": lambda s: 0.7*s["loneliness"] + 0.3*s["sadness"]},
    "irritation":   {"frustration":lambda s: 0.7*s["irritation"] + 0.3*s["anger"]},
    "rage":         {"fury":      lambda s: 0.8*s["rage"] + 0.2*s["anger"]},
    "annoyance":    {"agitation": lambda s: 0.6*s["annoyance"] + 0.4*s["stress"]},
    "apprehension": {"hesitation":lambda s: 0.7*s["apprehension"] + 0.3*s["fear"]},
    "terror":       {"horror":    lambda s: 0.8*s["terror"] + 0.2*s["fear"]},
    "panic":        {"alarm":     lambda s: 0.6*s["panic"] + 0.4*s["arousal"]},
    "revulsion":    {"abhorrence":lambda s: 0.8*s["revulsion"] + 0.2*s["disgust"]},
    "contempt":     {"scorn":     lambda s: 0.7*s["contempt"] + 0.3*s["disgust"]},
    "loathing":     {"detestation":lambda s: 0.8*s["loathing"] + 0.2*s["anger"]},
    "amazement":    {"wonder":    lambda s: 0.7*s["amazement"] + 0.3*s["surprise"]},
    "astonishment": {"bewilderment":lambda s: 0.6*s["astonishment"] + 0.4*s["surprise"]},
    "startle":      {"jolt":      lambda s: 0.5*s["startle"] + 0.5*s["arousal"]},
    "acceptance":   {"approval":  lambda s: 0.7*s["acceptance"] + 0.3*s["trust"]},
    "admiration":   {"esteem":    lambda s: 0.8*s["admiration"] + 0.2*s["trust"]},
    "reliance":     {"dependence":lambda s: 0.6*s["reliance"] + 0.4*s["confidence"]},
    "eagerness":    {"zeal":      lambda s: 0.7*s["eagerness"] + 0.3*s["joy"]},
    "enthusiasm":   {"fervor":    lambda s: 0.8*s["enthusiasm"] + 0.2*s["energy"]},
    "vigilance":    {"watchfulness":lambda s: 0.6*s["vigilance"] + 0.4*s["fear"]},
    "interest":     {"curiosity": lambda s: 0.8*s["interest"] + 0.2*s["curiosity"]},
    "tension":      {"strain":    lambda s: 0.7*s["tension"] + 0.3*s["stress"]},
    "overwhelm":    {"swamped":   lambda s: 0.8*s["overwhelm"] + 0.2*s["anxiety"]},
    "unease":       {"discomfort":lambda s: 0.6*s["unease"] + 0.4*s["fear"]},
    "worry":        {"concern":   lambda s: 0.7*s["worry"] + 0.3*s["anxiety"]},
    "nervousness":  {"restlessness":lambda s: 0.6*s["nervousness"] + 0.4*s["arousal"]},
    "dread":        {"foreboding":lambda s: 0.8*s["dread"] + 0.2*s["sadness"]},
}
TERTIARY_KEYS = list(dict.fromkeys(
    key
    for submap in TERTIARY_EMOTIONS.values()
    for key in submap
))

VALID_DYADS = {
    ("joy", "trust"): "love",
    ("trust", "fear"): "submission",
    ("fear", "surprise"): "awe",
    ("surprise", "sadness"): "disappointment",
    ("sadness", "disgust"): "remorse",
    ("disgust", "anger"): "contempt",
    ("anger", "anticipation"): "aggressiveness",
    ("anticipation", "joy"): "optimism",
    ("anticipation", "sexual_arousal"): "lust",
    ("trust", "sexual_arousal"): "intimacy",
    ("curiosity", "anticipation"): "interest_driven",
    ("curiosity", "joy"): "playfulness",
    ("energy", "joy"): "enthusiasm",
    ("energy", "anticipation"): "excitement",
    ("fatigue", "sadness"): "burnout",
    ("fatigue", "stress"): "exhaustion",
}
DYAD_KEYS: list[str] = list(VALID_DYADS.values())

VALID_TRIADS = {
    ("anticipation", "sexual_arousal", "joy"): "lustful_excitement",
    ("trust", "sexual_arousal", "joy"): "affection",
    ("curiosity", "joy", "trust"): "creative_collaboration",
    ("curiosity", "anticipation", "joy"): "inspired_eagerness",
    ("energy", "joy", "anticipation"): "euphoria",
    ("fatigue", "sadness", "stress"): "collapse",
}
TRIAD_KEYS: list[str] = list(VALID_TRIADS.values())

SOCIAL_METRICS = ["empathy", "engagement"]
DRIVE_METRICS = ["curiosity", "sexual_arousal"]
STYLE_METRICS = ["flirtation", "sarcasm", "profanity", "aggressiveness"]
COGNITIVE_METRICS = ["creativity", "precision", "humor", "friendliness", "confidence", "civility", "charisma", "persuasion", "authority", "wit", "patience"]
EXTRA_TRIGGER_METRICS = ["confusion", "embarrassment", "guilt"]
DIMENSIONS = ["valence", "arousal", "energy", "fatigue", "dominance"]

BASE_METRICS = list(dict.fromkeys([
    *DIMENSIONS,
    *PRIMARY_EMOTIONS,
    *SOCIAL_METRICS,
    *DRIVE_METRICS,
    *STYLE_METRICS,
    *COGNITIVE_METRICS,
]))

ALL_METRICS = list(dict.fromkeys([
    *BASE_METRICS,
    *SECONDARY_KEYS,
    *TERTIARY_KEYS,
    *DYAD_KEYS,
    *TRIAD_KEYS,
    *EXTRA_TRIGGER_METRICS,
]))

ANALYSIS_METRICS = [
    "valence", "arousal", "energy",
    "joy", "sadness", "anger", "fear", "disgust",
    "surprise", "anticipation", "trust",
    "stress", "anxiety", "confidence", "humor", 
    "charisma", "authority", "wit"
]

_raw_pairs = [
    ("joy", "sadness"), ("trust", "disgust"), ("fear", "anger"),
    ("surprise", "anticipation"), ("love", "remorse"), ("submission", "contempt"), 
    ("awe", "aggressiveness"), ("optimism", "disappointment"), ("lust", "guilt"),
    ("intimacy", "resentment"), ("interest_driven", "apathy"), ("playfulness", "resignation"),
    ("lustful_excitement", "guilt"), ("affection", "resentment"),
    ("creative_collaboration", "isolation"), ("inspired_eagerness", "boredom"),
]

OPPOSITES: dict[str, str] = {}
for a, b in _raw_pairs:
    OPPOSITES.setdefault(a, b)
    OPPOSITES.setdefault(b, a)

FAT_CLAMP = lambda x: max(0.0, min(1.0, x))
EOF