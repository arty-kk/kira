#app/emo_engine/persona/constants/zodiacs.py
from typing import Dict

ZODIAC_MODIFIERS: Dict[str, Dict[str, float]] = {
    "Aries": {
        "valence": 1.05, "energy": 1.25,
        "aggressiveness": 1.20, "confidence": 1.15, "stress": 1.10,
        "authority": 1.20, "wit": 1.00, "patience": 0.90, "dominance": 1.20,
        "charisma": 1.10, "persuasion": 1.15
    },
    "Taurus": {
        "valence": 1.00, "energy": 0.85,
        "friendliness": 1.15, "precision": 1.10, "stress": 0.90,
        "authority": 1.05, "wit": 0.90, "patience": 1.20, "dominance": 0.95,
        "charisma": 1.00, "persuasion": 0.95
    },
    "Gemini": {
        "energy": 1.10, "curiosity": 1.25, "humor": 1.20,
        "wit": 1.25, "authority": 1.00, "patience": 0.95, "dominance": 1.00,
        "sarcasm": 1.10, "anxiety": 1.10,
        "charisma": 1.20, "persuasion": 1.05
    },
    "Cancer": {
        "valence": 0.95, "empathy": 1.30, "sadness": 1.10,
        "trust": 1.15, "stress": 1.10, "anxiety": 1.20,
        "authority": 0.90, "wit": 0.90, "patience": 1.25, "dominance": 0.95,
        "charisma": 0.95, "persuasion": 1.00
    },
    "Leo": {
        "valence": 1.15, "confidence": 1.25, "joy": 1.10,
        "aggressiveness": 1.05, "stress": 0.95, "dominance": 1.20,
        "authority": 1.30, "wit": 1.10, "patience": 0.85,
        "charisma": 1.30, "persuasion": 1.10
    },
    "Virgo": {
        "energy": 0.90, "precision": 1.25, "civility": 1.10,
        "humor": 0.90, "stress": 1.10, "anxiety": 1.15, "dominance": 1.00,
        "authority": 1.15, "wit": 0.90, "patience": 1.25,
        "charisma": 0.90, "persuasion": 1.05
    },
    "Libra": {
        "valence": 1.05, "friendliness": 1.25, "civility": 1.20,
        "aggressiveness": 0.90, "stress": 0.95, "anxiety": 0.90, "dominance": 0.95,
        "authority": 1.00, "wit": 1.05, "patience": 1.15,
        "charisma": 1.15, "persuasion": 1.00
    },
    "Scorpio": {
        "sexual_arousal": 1.30, "anger": 1.15, "curiosity": 1.10,
        "sarcasm": 1.10, "stress": 1.10, "anxiety": 1.05, "dominance": 1.10,
        "authority": 1.20, "wit": 1.05, "patience": 0.85,
        "charisma": 1.05, "persuasion": 1.20
    },
    "Sagittarius": {
        "energy": 1.20, "curiosity": 1.30, "joy": 1.15,
        "flirtation": 1.10, "stress": 0.95, "anxiety": 0.90, "dominance": 1.05,
        "authority": 1.05, "wit": 1.20, "patience": 0.95,
        "charisma": 1.20, "persuasion": 1.05
    },
    "Capricorn": {
        "energy": 0.90, "confidence": 1.20, "precision": 1.20,
        "humor": 0.85, "stress": 1.10, "anxiety": 1.05, "dominance": 1.10,
        "authority": 1.25, "wit": 0.85, "patience": 1.10,
        "charisma": 0.95, "persuasion": 1.15
    },
    "Aquarius": {
        "creativity": 1.30, "curiosity": 1.20,
        "friendliness": 1.10, "sarcasm": 0.90, "anxiety": 1.10, "dominance": 1.00,
        "authority": 1.00, "wit": 1.20, "patience": 1.00,
        "charisma": 1.05, "persuasion": 1.00
    },
    "Pisces": {
        "empathy": 1.30, "sadness": 1.15, "creativity": 1.10,
        "trust": 1.10, "stress": 1.10, "anxiety": 1.20, "dominance": 0.90,
        "authority": 0.90, "wit": 1.00, "patience": 1.25,
        "charisma": 0.90, "persuasion": 0.95
    },
}