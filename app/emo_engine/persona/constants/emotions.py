cat >app/emo_engine/persona/constants/emotions.py<< 'EOF'
#app/emo_engine/persona/constants/emotions.py
from typing import Dict, List, Callable

PRIMARY_COORDS: Dict[str, tuple[float, float]] = {
    "joy":          (1.0, 0.0),
    "trust":        (0.707, 0.707),
    "fear":         (0.0, 1.0),
    "surprise":     (-0.707, 0.707),
    "sadness":      (-1.0, 0.0),
    "disgust":      (-0.707, -0.707),
    "anger":        (0.0, -1.0),
    "anticipation": (0.707, -0.707),
    "curiosity":    (-0.866,-0.5),
    "sexual_arousal": (0.866, -0.5),
    "anxiety":      (-0.5,  0.866),
    "stress":       (-0.5, -0.866),
    "energy":       (0.6,   0.8),
    "fatigue":      (-0.6, -0.8),
    "pride":        (0.5, 0.5),
    "doubt":        (-0.5, 0.5),
    "nostalgia":    (-0.5, -0.2),
    "rage":         (0.0, -0.8),
    "compassion":   (0.5, 0.2),
    "apathy":       (-0.8, -0.5),
    "regret":       (-0.8, 0.2),
    "alienation":   (-0.5, -0.5),
    "impatience":   (0.2, -0.5),
    "contentment":  (0.2, 0.5),
    "reflection":   (0.0, 0.2),
    "alertness":    (0.4,  0.6),
    "amusement":    (0.8,  0.5),
    "attraction":   (0.9,  0.7),
    "bitterness":   (-0.6, 0.2),
    "calm":         (0.5, -0.4),
    "caution":      (-0.2, 0.4),
    "charm":        (0.7,  0.3),
    "chaos":        (-0.3, 0.9),
    "conflict":     (-0.4, 0.5),
    "control":      (0.3,  0.1),
    "creation":     (0.6,  0.6),
    "courage":      (0.2,  0.7),
    "cynicism":     (-0.5, 0.3),
    "darkness":     (-0.8, 0.4),
    "detachment":   (-0.3,-0.2),
    "danger":       (-0.6, 0.8),
    "determination":(0.4,  0.7),
    "focus":        (0.3,  0.2),
    "gratitude":    (0.9,  0.2),
    "hidden_anger": (-0.4, 0.6),
    "humility":     (0.5,  0.1),
    "inspiration":  (0.7,  0.8),
    "kindness":     (0.8,  0.3),
    "longing":      (-0.2, 0.2),
    "malice":       (-0.7, 0.5),
    "neutral":      (0.0,  0.0),
    "peace":        (0.6, -0.3),
    "persistence":  (0.3,  0.6),
    "release":      (0.5,  0.9),
    "resolve":      (0.4,  0.6),
    "resentment":   (-0.5, 0.4),
    "restraint":    (0.1,  0.2),
    "respect":      (0.7,  0.3),
    "satisfaction": (0.8,  0.2),
    "seduction":    (0.9,  0.7),
    "stealth":      (-0.2, 0.3),
    "technical":    (0.1,  0.1),
    "tenderness":   (0.8,  0.4),
    "tranquility":  (0.6, -0.2),
    "warmth":       (0.9,  0.3),
    "overflow":     (-0.1, 0.8),
    "loneliness":   (-0.6,  0.0),
    "ecstasy":      ( 0.9,  0.9),
    "trepidation":  (-0.4,  0.8),
    "skepticism":   (-0.3,  0.4),
    "tear":         (-0.9,  0.2),
    "despair":      (-0.8,  0.3),
    "certainty":    ( 0.4,  0.1),
    "comfort":      ( 0.7, -0.3),
    "madness":      (-0.6,  0.9),
    "carefree":     ( 0.5,  0.4),
}

PRIMARY_EMOTIONS: List[str] = list(PRIMARY_COORDS.keys())

PRIMARY_ORDER: List[str] = [
    "joy", "trust", "fear", "surprise",
    "sadness", "disgust", "anger", "anticipation",
]

VALID_DYADS: Dict[tuple[str,str], str] = {}
for i, e1 in enumerate(PRIMARY_ORDER):
    e2 = PRIMARY_ORDER[(i+1) % len(PRIMARY_ORDER)]
    VALID_DYADS[(e1, e2)] = f"{e1}_{e2}"
DYAD_KEYS: List[str] = list(VALID_DYADS.values())

VALID_TRIADS: Dict[tuple[str,str,str], str] = {}
for i, e1 in enumerate(PRIMARY_ORDER):
    e2 = PRIMARY_ORDER[(i+1) % len(PRIMARY_ORDER)]
    e3 = PRIMARY_ORDER[(i+2) % len(PRIMARY_ORDER)]
    VALID_TRIADS[(e1, e2, e3)] = f"{e1}_{e2}_{e3}"
TRIAD_KEYS: List[str] = list(VALID_TRIADS.values())

SECONDARY_EMOTIONS: Dict[str, Dict[str, Callable[[dict], float]]] = {
    "joy": {
        "optimism":     lambda s: 0.6*s["joy"] + 0.4*s["anticipation"],
        "ecstasy":      lambda s: 0.8*s["joy"] + 0.2*s["arousal"],
        "cheerfulness": lambda s: 0.7*s["joy"] + 0.3*s.get("friendliness", 0.5),
        "enthusiasm":   lambda s: 0.7*s["joy"] + 0.3*s["energy"],
        "amusement":    lambda s: 0.7*s["joy"] + 0.3*s.get("humor", 0.0),
        "attraction":   lambda s: 0.6*s["joy"] + 0.4*s["trust"],
        "carefree":     lambda s: 0.7*s["joy"] + 0.3*s["anticipation"],
        "comfort":      lambda s: 0.6*s["joy"] + 0.4*s["trust"],
    },
    "sadness": {
        "gloom":       lambda s: 0.7*s["sadness"] + 0.3*s["stress"],
        "despair":     lambda s: 0.8*s["sadness"] + 0.2*s["anxiety"],
        "loneliness":  lambda s: 0.6*s["sadness"] + 0.4*s["trust"],
        "nostalgia":   lambda s: 0.6*s["sadness"] + 0.4*s.get("joy", 0.5),
        "longing":     lambda s: 0.6*s["sadness"] + 0.4*s["nostalgia"],
        "detachment":  lambda s: 0.6*s["sadness"] + 0.4*s["alienation"],
        "tear":        lambda s: 0.7*s["sadness"] + 0.3*s["despair"],
    },
    "anger": {
        "irritation":   lambda s: 0.7*s["anger"] + 0.3*s["stress"],
        "rage":         lambda s: 0.9*s["anger"] + 0.1*s.get("aggressiveness", 0.5),
        "annoyance":    lambda s: 0.5*s["anger"] + 0.5*s["sarcasm"],
        "hidden_anger": lambda s: 0.5*s["anger"] + 0.5*s["restraint"],
        "malice":       lambda s: 0.6*s["anger"] + 0.4*s["contempt"],
        "madness":      lambda s: 0.6*s["rage"] + 0.4*s["stress"],
    },
    "fear": {
        "apprehension": lambda s: 0.6*s["fear"] + 0.4*s["anxiety"],
        "terror":       lambda s: 0.8*s["fear"] + 0.2*s["stress"],
        "panic":        lambda s: 0.7*s["fear"] + 0.3*s["arousal"],
        "trepidation":  lambda s: 0.6*s["fear"] + 0.4*s["anticipation"],
    },
    "disgust": {
        "revulsion":  lambda s: 0.8*s["disgust"] + 0.2*s["anger"],
        "contempt":   lambda s: 0.7*s["disgust"] + 0.3*s["sarcasm"],
        "loathing":   lambda s: 0.9*s["disgust"] + 0.1*s["anger"],
        "bitterness": lambda s: 0.7*s["disgust"] + 0.3*s["anger"],
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
        "gratitude":  lambda s: 0.7*s["trust"] + 0.3*s["joy"],
        "respect":    lambda s: 0.6*s["trust"] + 0.4*s["admiration"],
        "certainty":  lambda s: 0.7*s["trust"] + 0.3*s["confidence"],
    },
    "anticipation": {
        "eagerness":  lambda s: 0.7*s["anticipation"] + 0.3*s["joy"],
        "vigilance":  lambda s: 0.6*s["anticipation"] + 0.4*s["fear"],
        "interest":   lambda s: 0.8*s["anticipation"] + 0.2*s["curiosity"],
        "alertness":  lambda s: 0.6*s["anticipation"] + 0.4*s["energy"],
        "caution":    lambda s: 0.6*s["anticipation"] + 0.4*s["apprehension"],
        "skepticism": lambda s: 0.7*s["doubt"] + 0.3*s["fear"],
    },
    "stress": {
        "tension":   lambda s: 0.7*s["stress"] + 0.3*s["arousal"],
        "overwhelm": lambda s: 0.8*s["stress"] + 0.2*s["anxiety"],
        "unease":    lambda s: 0.6*s["stress"] + 0.4*s["fear"],
        "conflict":  lambda s: 0.5*s["stress"] + 0.5*s["doubt"],
    },
    "anxiety": {
        "worry":       lambda s: 0.7*s["anxiety"] + 0.3*s["fear"],
        "nervousness": lambda s: 0.6*s["anxiety"] + 0.4*s["arousal"],
        "dread":       lambda s: 0.8*s["anxiety"] + 0.2*s["sadness"],
    },
    "creativity": {
        "creation": lambda s: s["creativity"],
    },
    "confidence": {
        "determination": lambda s: 0.7*s["confidence"] + 0.3*s["courage"],
    },
    "compassion": {
        "kindness": lambda s: 0.6*s["compassion"] + 0.4*s["warmth"],
    },
    "curiosity": {
        "focus": lambda s: 0.6*s["curiosity"] + 0.4*s["precision"],
    },
    "self_reflection": {
        "restraint": lambda s: 0.7*s["self_reflection"] + 0.3*s["patience"],
    },
}
SECONDARY_KEYS = list({key for subs in SECONDARY_EMOTIONS.values() for key in subs})


TERTIARY_EMOTIONS: Dict[str, Dict[str, Callable[[dict], float]]] = {
    "optimism": {
        "hope":      lambda s: 0.7*s["optimism"] + 0.3*s["anticipation"],
    },
    "ecstasy": {
        "bliss":     lambda s: 0.8*s["ecstasy"] + 0.2*s["joy"],
    },
    "cheerfulness": {
        "merriment": lambda s: 0.6*s["cheerfulness"] + 0.4*s["energy"] if "energy" in s else s["cheerfulness"],
    },
    "gloom": {
        "melancholy":lambda s: 0.7*s["gloom"] + 0.3*s["sadness"],
    },
    "despair": {
        "distress":  lambda s: 0.8*s["despair"] + 0.2*s["sadness"],
    },
    "loneliness": {
        "isolation": lambda s: 0.7*s["loneliness"] + 0.3*s["sadness"],
    },
    "irritation": {
        "frustration": lambda s: 0.7*s["irritation"] + 0.3*s["anger"],
    },
    "rage": {
        "fury":      lambda s: 0.8*s["rage"] + 0.2*s["anger"],
    },
    "annoyance": {
        "agitation": lambda s: 0.6*s["annoyance"] + 0.4*s["stress"],
    },
    "apprehension": {
        "hesitation":lambda s: 0.7*s["apprehension"] + 0.3*s["fear"],
    },
    "terror": {
        "horror":    lambda s: 0.8*s["terror"] + 0.2*s["fear"],
    },
    "panic": {
        "alarm":     lambda s: 0.6*s["panic"] + 0.4*s["arousal"],
    },
    "revulsion": {
        "abhorrence":lambda s: 0.8*s["revulsion"] + 0.2*s["disgust"],
    },
    "contempt": {
        "scorn":     lambda s: 0.7*s["contempt"] + 0.3*s["disgust"],
    },
    "loathing": {
        "detestation":lambda s: 0.8*s["loathing"] + 0.2*s["anger"],
    },
    "amazement": {
        "wonder":    lambda s: 0.7*s["amazement"] + 0.3*s["surprise"],
    },
    "astonishment": {
        "bewilderment":lambda s: 0.6*s["astonishment"] + 0.4*s["surprise"],
    },
    "startle": {
        "jolt":      lambda s: 0.5*s["startle"] + 0.5*s["arousal"],
    },
    "acceptance": {
        "approval":  lambda s: 0.7*s["acceptance"] + 0.3*s["trust"],
    },
    "admiration": {
        "esteem":    lambda s: 0.8*s["admiration"] + 0.2*s["trust"],
    },
    "reliance": {
        "dependence":lambda s: 0.6*s["reliance"] + 0.4*s["confidence"],
    },
    "eagerness": {
        "zeal":      lambda s: 0.7*s["eagerness"] + 0.3*s["joy"],
    },
    "enthusiasm": {
        "fervor":    lambda s: 0.8*s["enthusiasm"] + 0.2*s["energy"],
    },
    "vigilance": {
        "watchfulness":lambda s: 0.6*s["vigilance"] + 0.4*s["fear"],
    },
    "interest": {
        "curiosity": lambda s: 0.8*s["interest"] + 0.2*s["curiosity"],
    },
    "tension": {
        "strain":    lambda s: 0.7*s["tension"] + 0.3*s["stress"],
    },
    "overwhelm": {
        "swamped":   lambda s: 0.8*s["overwhelm"] + 0.2*s["anxiety"],
        "overflow":  lambda s: 0.6*s["overwhelm"] + 0.4*s.get("excitement", 0.0),
    },
    "unease": {
        "discomfort":lambda s: 0.6*s["unease"] + 0.4*s["fear"],
    },
    "worry": {
        "concern":   lambda s: 0.7*s["worry"] + 0.3*s["anxiety"],
    },
    "nervousness": {
        "restlessness":lambda s: 0.6*s["nervousness"] + 0.4*s["arousal"],
    },
    "dread": {
        "foreboding":lambda s: 0.8*s["dread"] + 0.2*s["sadness"],
    },
    "energy": {
        "release":   lambda s: 0.7*s["energy"] + 0.3*s.get("euphoria", 0.0),
    },
    "fatigue": {
        "calm":      lambda s: 1.0 - s.get("arousal", 0.0),
    },
    "joy": {
        "satisfaction": lambda s: 0.6*s["joy"] + 0.4*s.get("contentment", 0.0),
    },
}
TERTIARY_KEYS = list({key for subs in TERTIARY_EMOTIONS.values() for key in subs})


SOCIAL_METRICS = ["empathy", "engagement"]
DRIVE_METRICS = ["curiosity", "sexual_arousal"]
STYLE_METRICS = ["flirtation", "sarcasm", "profanity", "aggressiveness", "self_deprecation"]
COGNITIVE_METRICS = ["creativity", "precision", "humor", "friendliness", "confidence", "civility", "charisma", "persuasion", "authority", "wit", "patience", "self_reflection"]
EXTRA_TRIGGER_METRICS = ["confusion", "embarrassment", "guilt"]
DIMENSIONS = ["valence", "arousal", "energy", "fatigue", "dominance"]

RELATIONSHIP_METRICS = ["attachment"]

NON_DYNAMIC_METRICS = list(RELATIONSHIP_METRICS)

BASE_METRICS = list(dict.fromkeys(
    DIMENSIONS
  + PRIMARY_EMOTIONS
  + SOCIAL_METRICS
  + DRIVE_METRICS
  + STYLE_METRICS
  + COGNITIVE_METRICS
))

ALL_METRICS = list(dict.fromkeys(
    BASE_METRICS
  + SECONDARY_KEYS
  + TERTIARY_KEYS
  + DYAD_KEYS
  + TRIAD_KEYS
  + EXTRA_TRIGGER_METRICS
))

ANALYSIS_METRICS = [
    "valence", "arousal", "dominance",
    "joy", "sadness", "anger", "fear", "disgust",
    "surprise", "trust", "anticipation",
    "stress", "anxiety", "confidence",
    "humor", "charisma", "authority", "wit"
]

_raw_pairs = [
    ("joy", "sadness"), ("trust", "disgust"), ("fear", "anger"),
    ("surprise", "anticipation"), ("love", "remorse"), ("submission", "contempt"), 
    ("awe", "aggressiveness"), ("optimism", "disappointment"), ("lust", "guilt"),
    ("intimacy", "resentment"), ("interest_driven", "apathy"),
    ("lustful_excitement", "guilt"), ("affection", "resentment"),
    ("creative_collaboration", "isolation"), ("inspired_eagerness", "boredom"),
]

OPPOSITES: dict[str, str] = {}
for a, b in _raw_pairs:
    OPPOSITES.setdefault(a, b)
    OPPOSITES.setdefault(b, a)

FAT_CLAMP = lambda x: max(0.0, min(1.0, x))
EOF