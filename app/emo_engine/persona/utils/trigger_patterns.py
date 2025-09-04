#app/emo_engine/persona/utils/trigger_patterns.py
import re

TRIGGER_PATTERNS = [
    {"pattern": re.compile(r"\?{2,}"), "deltas": {"surprise": 0.05, "anxiety": 0.03}},
    {"pattern": re.compile(r"\.{3,}"), "deltas": {"anticipation": 0.03, "sadness": 0.02}},
    {"pattern": re.compile(r"[!?]{2,}"), "deltas": {"arousal": 0.04, "surprise": 0.04}},
    {"pattern": re.compile(r"!\?|\?!"), "deltas": {"surprise": 0.06, "arousal": 0.06}},
    {"pattern": re.compile(r":\)"), "deltas": {"joy": 0.10, "valence": 0.05}},
    {"pattern": re.compile(r":\("), "deltas": {"sadness": 0.10, "valence": -0.05}},
    {"pattern": re.compile(r";\)"), "deltas": {"humor": 0.08, "joy": 0.05}},
    {"pattern": re.compile(r":D"), "deltas": {"joy": 0.12, "energy": 0.06}},
    {"pattern": re.compile(r":[Pp]"), "deltas": {"humor": 0.07, "flirtation": 0.05}},
    {"pattern": re.compile(r":\/"), "deltas": {"sadness": 0.05, "anxiety": 0.03}},
    {"pattern": re.compile(r";\/"), "deltas": {"sadness": 0.04, "trust": -0.02}},
    {"pattern": re.compile(r"\)\)\)+"), "deltas": {"joy": 0.05, "energy": 0.03}},
    {"pattern": re.compile(r"\(\(\(+"), "deltas": {"sadness": 0.05, "valence": -0.03}},
    {"pattern": re.compile(r"😊|🙂|😀|😃|😄|😁|😂|🤣|😅|😆|😇"), "deltas": {"joy": 0.15, "valence": 0.10}},
    {"pattern": re.compile(r"🙃"), "deltas": {"humor": 0.08, "joy": 0.05}},
    {"pattern": re.compile(r"🥳"), "deltas": {"joy": 0.12, "energy": 0.08}},
    {"pattern": re.compile(r"🤩"), "deltas": {"surprise": 0.10, "joy": 0.07}},
    {"pattern": re.compile(r"🤯"), "deltas": {"surprise": 0.12, "arousal": 0.08}},
    {"pattern": re.compile(r"🥲"), "deltas": {"sadness": 0.10, "valence": -0.04}},
    {"pattern": re.compile(r"😢|😞|😔|😟|😭|😥|😓"), "deltas": {"sadness": 0.15, "valence": -0.10}},
    {"pattern": re.compile(r"😠|😡|😤|🤬"), "deltas": {"anger": 0.15, "arousal": 0.10}},
    {"pattern": re.compile(r"😮|😯|😲|😳|😱"), "deltas": {"surprise": 0.12, "anxiety": 0.05}},
    {"pattern": re.compile(r"😉|😜|😝|😛|🤪"), "deltas": {"humor": 0.10, "flirtation": 0.07}},
    {"pattern": re.compile(r"🤔"), "deltas": {"curiosity": 0.10, "anticipation": 0.05}},
    {"pattern": re.compile(r"🤷(?:‍♂️|‍♀️)?"), "deltas": {"confusion": 0.08, "anxiety": 0.04}},
    {"pattern": re.compile(r"🤦(?:‍♂️|‍♀️)?"), "deltas": {"embarrassment": 0.10, "sadness": 0.05}},
    {"pattern": re.compile(r"😎|🤓"), "deltas": {"confidence": 0.10, "creativity": 0.05}},
    {"pattern": re.compile(r"😇|😍|🥰|😘|😗|😙|😚"), "deltas": {"trust": 0.12, "joy": 0.08}},
    {"pattern": re.compile(r"😐|😑|😶"), "deltas": {"anticipation": 0.04, "stress": 0.02}},
    {"pattern": re.compile(r"😴|😪|😌"), "deltas": {"stress": -0.05, "valence": 0.03}},
    {"pattern": re.compile(r"🤢|🤮|🤧"), "deltas": {"disgust": 0.15, "valence": -0.08}},
    {"pattern": re.compile(r"🤝|🙏"), "deltas": {"trust": 0.10, "empathy": 0.08}},
    {"pattern": re.compile(r"💪|🏆"), "deltas": {"confidence": 0.15, "joy": 0.05}},
    {"pattern": re.compile(r"❤️|💖|💕|💗|💔"), "deltas": {"trust": 0.12, "joy": 0.08, "sadness": 0.10}},
    {"pattern": re.compile(r"🔥|⚡"), "deltas": {"arousal": 0.10, "energy": 0.08}},
    {"pattern": re.compile(r"🌧️|☔|🌧"), "deltas": {"sadness": 0.07, "anticipation": 0.04}},
    {"pattern": re.compile(r"👍"), "deltas": {"trust": 0.10, "valence": 0.05}},
    {"pattern": re.compile(r"👎"), "deltas": {"trust": -0.08, "valence": -0.05}},
    {"pattern": re.compile(r"🎉"), "deltas": {"joy": 0.12, "energy": 0.07}},
    {"pattern": re.compile(r"👀"), "deltas": {"anticipation": 0.08, "curiosity": 0.06}},
    {"pattern": re.compile(r"🤗"), "deltas": {"empathy": 0.12, "joy": 0.07}}
]

def apply_triggers(text: str) -> dict:
    deltas: dict[str, float] = {}
    for trigger in TRIGGER_PATTERNS:
        if trigger["pattern"].search(text):
            for metric, delta in trigger["deltas"].items():
                deltas[metric] = deltas.get(metric, 0.0) + delta
    return deltas
