#app/emo_engine/persona/constants/temperaments.py

from typing import Dict

TEMPERAMENT_PROFILE: Dict[str, Dict[str, float]] = {
    "sanguine": {
        "valence": 1.15, "arousal": 1.20, "engagement": 1.25,
        "joy": 1.25,  "curiosity": 1.15,  "creativity": 1.10,
        "humor": 1.25, "friendliness": 1.20, "sexual_arousal": 1.10,
        "precision": 0.95, "civility": 1.05,
        "anger": 0.85, "sadness": 0.80, "aggressiveness": 0.85,
        "confidence": 1.10, "authority": 1.05, "wit": 1.15,
        "patience": 0.90, "stress": 0.75, "anxiety": 0.80,
        "fatigue": 0.85, "charisma": 1.15, "persuasion": 1.05,
    },
    "choleric": {
        "valence": 0.90, "arousal": 1.30, "engagement": 1.10,
        "anger": 1.35, "aggressiveness": 1.30, "confidence": 1.20,
        "creativity": 1.05, "curiosity": 1.05, "precision": 0.95,
        "authority": 1.25, "wit": 1.00, "patience": 0.80,
        "civility": 0.90, "sexual_arousal": 1.05,
        "joy": 0.85, "humor": 0.90, "empathy": 0.85,
        "stress": 1.20, "anxiety": 1.15, "fatigue": 1.10,
        "charisma": 1.00, "persuasion": 1.25,
    },
    "melancholic": {
        "valence": 0.75, "arousal": 0.70, "engagement": 0.85,
        "sadness": 1.30, "fear": 1.15,
        "creativity": 1.15, "curiosity": 1.05, "precision": 1.10,
        "authority": 0.90, "wit": 0.95, "patience": 1.15,
        "civility": 1.05, "sexual_arousal": 0.90,
        "humor": 0.85, "confidence": 0.80, "friendliness": 0.90,
        "aggressiveness": 0.85, "stress": 1.20, "anxiety": 1.25, 
        "fatigue": 1.10, "charisma": 0.85, "persuasion": 0.90,
    },
    "phlegmatic": {
        "valence": 1.00, "arousal": 0.60, "engagement": 0.80,
        "friendliness": 1.20, "empathy": 1.20,
        "authority": 0.95, "wit": 1.00, "patience": 1.30,
        "creativity": 1.05, "curiosity": 1.00, "precision": 1.05,
        "civility": 1.15, "sexual_arousal": 0.90,
        "anger": 0.75, "aggressiveness": 0.70, "humor": 0.95,
        "confidence": 0.90, "stress": 0.75, "anxiety": 0.80, 
        "fatigue": 0.95, "charisma": 0.95, "persuasion": 0.85,
    },
}