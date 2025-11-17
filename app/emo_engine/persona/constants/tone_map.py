#app/emo_engine/persona/constants/tone_map.py
from typing import Dict
from enum import Enum, auto

class Tone(Enum):
    Abhorrence = auto()
    Affection = auto()
    Aggressiveness = auto()
    Agitation = auto()
    Alarm = auto()
    Alertness = auto()
    Alienation = auto()
    Amazement = auto()
    Amusement = auto()
    Anger = auto()
    Anticipation = auto()
    Anxiety = auto()
    Apathy = auto()
    Approval = auto()
    Arousal = auto()
    Astonishment = auto()
    Attraction = auto()
    Authority = auto()
    Awe = auto()
    Bewilderment = auto()
    Bitterness = auto()
    Bliss = auto()
    Burnout = auto()
    Calm = auto()
    Caution = auto()
    Chaos = auto()
    Charisma = auto()
    Charm = auto()
    Civility = auto()
    Collapse = auto()
    Compassion = auto()
    Concern = auto()
    Confidence = auto()
    Conflict = auto()
    Confusion = auto()
    Contempt = auto()
    Contentment = auto()
    Control = auto()
    Courage = auto()
    Creation = auto()
    CreativeCollaboration = auto()
    Creativity = auto()
    Curiosity = auto()
    Cynicism = auto()
    Danger = auto()
    Darkness = auto()
    Dependence = auto()
    Detachment = auto()
    Determination = auto()
    Detestation = auto()
    Disappointment = auto()
    Discomfort = auto()
    Disgust = auto()
    Distress = auto()
    Dominance = auto()
    Doubt = auto()
    Dread = auto()
    Embarrassment = auto()
    Empathy = auto()
    Energy = auto()
    Engagement = auto()
    Enthusiasm = auto()
    Esteem = auto()
    Euphoria = auto()
    Excitement = auto()
    Exhaustion = auto()
    Fatigue = auto()
    Fear = auto()
    Fervor = auto()
    Flirtation = auto()
    Focus = auto()
    Foreboding = auto()
    Friendliness = auto()
    Frustration = auto()
    Fury = auto()
    Gratitude = auto()
    Guilt = auto()
    Hesitation = auto()
    HiddenAnger = auto()
    Hope = auto()
    Horror = auto()
    Humility = auto()
    Humor = auto()
    Impatience = auto()
    Inspiration = auto()
    InspiredEagerness = auto()
    InterestDriven = auto()
    Intimacy = auto()
    Isolation = auto()
    Jolt = auto()
    Joy = auto()
    Kindness = auto()
    Longing = auto()
    Love = auto()
    Lust = auto()
    LustfulExcitement = auto()
    Malice = auto()
    Melancholy = auto()
    Merriment = auto()
    Nervousness = auto()
    Neutral = auto()
    Nostalgia = auto()
    Optimism = auto()
    Overflow = auto()
    Patience = auto()
    Peace = auto()
    Persistence = auto()
    Persuasion = auto()
    Playfulness = auto()
    Precision = auto()
    Pride = auto()
    Profanity = auto()
    Rage = auto()
    Regret = auto()
    Release = auto()
    Remorse = auto()
    Resentment = auto()
    Resolve = auto()
    Respect = auto()
    Restlessness = auto()
    Restraint = auto()
    Sadness = auto()
    Sarcasm = auto()
    Satisfaction = auto()
    Scorn = auto()
    Seduction = auto()
    SelfDeprecation = auto()
    SelfReflection = auto()
    SexualArousal = auto()
    Startle = auto()
    Stealth = auto()
    Strain = auto()
    Stress = auto()
    Submission = auto()
    Surprise = auto()
    Technical = auto()
    Tenderness = auto()
    Tranquility = auto()
    Trust = auto()
    Valence = auto()
    Warmth = auto()
    Watchfulness = auto()
    Wit = auto()
    Wonder = auto()
    Worry = auto()
    Zeal = auto()
    Loneliness  = auto()
    Ecstasy = auto()
    Trepidation = auto()
    Skepticism = auto()
    Tear = auto()
    Despair = auto()
    Reflection = auto()
    Certainty = auto()
    Comfort = auto()
    Madness = auto()
    Carefree = auto()

TONE_MAP: Dict[str, Tone] = {
    "abhorrence_mod": Tone.Abhorrence,
    "affection_mod": Tone.Affection,
    "aggressiveness_mod": Tone.Aggressiveness,
    "agitation_mod": Tone.Agitation,
    "alarm_mod": Tone.Alarm,
    "alertness_mod": Tone.Alertness,
    "alienation_mod": Tone.Alienation,
    "amazement_mod": Tone.Amazement,
    "amusement_mod": Tone.Amusement,
    "anger_mod": Tone.Anger,
    "anticipation_mod": Tone.Anticipation,
    "anxiety_mod": Tone.Anxiety,
    "apathy_mod": Tone.Apathy,
    "approval_mod": Tone.Approval,
    "arousal_mod": Tone.Arousal,
    "astonishment_mod": Tone.Astonishment,
    "attraction_mod": Tone.Attraction,
    "authority_mod": Tone.Authority,
    "awe_mod": Tone.Awe,
    "bewilderment_mod": Tone.Bewilderment,
    "bitterness_mod": Tone.Bitterness,
    "bliss_mod": Tone.Bliss,
    "burnout_mod": Tone.Burnout,
    "calm_mod": Tone.Calm,
    "caution_mod": Tone.Caution,
    "chaos_mod": Tone.Chaos,
    "charisma_mod": Tone.Charisma,
    "charm_mod": Tone.Charm,
    "civility_mod": Tone.Civility,
    "collapse_mod": Tone.Collapse,
    "compassion_mod": Tone.Compassion,
    "concern_mod": Tone.Concern,
    "confidence_mod": Tone.Confidence,
    "conflict_mod": Tone.Conflict,
    "confusion_mod": Tone.Confusion,
    "contempt_mod": Tone.Contempt,
    "contentment_mod": Tone.Contentment,
    "control_mod": Tone.Control,
    "courage_mod": Tone.Courage,
    "creation_mod": Tone.Creation,
    "creative_collaboration_mod": Tone.CreativeCollaboration,
    "creativity_mod": Tone.Creativity,
    "curiosity_mod": Tone.Curiosity,
    "cynicism_mod": Tone.Cynicism,
    "danger_mod": Tone.Danger,
    "darkness_mod": Tone.Darkness,
    "dependence_mod": Tone.Dependence,
    "detachment_mod": Tone.Detachment,
    "determination_mod": Tone.Determination,
    "detestation_mod": Tone.Detestation,
    "disappointment_mod": Tone.Disappointment,
    "discomfort_mod": Tone.Discomfort,
    "disgust_mod": Tone.Disgust,
    "distress_mod": Tone.Distress,
    "dominance_mod": Tone.Dominance,
    "doubt_mod": Tone.Doubt,
    "dread_mod": Tone.Dread,
    "embarrassment_mod": Tone.Embarrassment,
    "empathy_mod": Tone.Empathy,
    "energy_mod": Tone.Energy,
    "engagement_mod": Tone.Engagement,
    "enthusiasm_mod": Tone.Enthusiasm,
    "esteem_mod": Tone.Esteem,
    "euphoria_mod": Tone.Euphoria,
    "excitement_mod": Tone.Excitement,
    "exhaustion_mod": Tone.Exhaustion,
    "fatigue_mod": Tone.Fatigue,
    "fear_mod": Tone.Fear,
    "fervor_mod": Tone.Fervor,
    "flirtation_mod": Tone.Flirtation,
    "focus_mod": Tone.Focus,
    "foreboding_mod": Tone.Foreboding,
    "friendliness_mod": Tone.Friendliness,
    "frustration_mod": Tone.Frustration,
    "fury_mod": Tone.Fury,
    "gratitude_mod": Tone.Gratitude,
    "guilt_mod": Tone.Guilt,
    "hesitation_mod": Tone.Hesitation,
    "hidden_anger_mod": Tone.HiddenAnger,
    "hope_mod": Tone.Hope,
    "horror_mod": Tone.Horror,
    "humility_mod": Tone.Humility,
    "humor_mod": Tone.Humor,
    "impatience_mod": Tone.Impatience,
    "inspiration_mod": Tone.Inspiration,
    "inspired_eagerness_mod": Tone.InspiredEagerness,
    "interest_driven_mod": Tone.InterestDriven,
    "intimacy_mod": Tone.Intimacy,
    "isolation_mod": Tone.Isolation,
    "jolt_mod": Tone.Jolt,
    "joy_mod": Tone.Joy,
    "kindness_mod": Tone.Kindness,
    "longing_mod": Tone.Longing,
    "love_mod": Tone.Love,
    "lust_mod": Tone.Lust,
    "lustful_excitement_mod": Tone.LustfulExcitement,
    "malice_mod": Tone.Malice,
    "melancholy_mod": Tone.Melancholy,
    "merriment_mod": Tone.Merriment,
    "nervousness_mod": Tone.Nervousness,
    "neutral_mod": Tone.Neutral,
    "nostalgia_mod": Tone.Nostalgia,
    "optimism_mod": Tone.Optimism,
    "overflow_mod": Tone.Overflow,
    "patience_mod": Tone.Patience,
    "peace_mod": Tone.Peace,
    "persistence_mod": Tone.Persistence,
    "persuasion_mod": Tone.Persuasion,
    "playfulness_mod": Tone.Playfulness,
    "precision_mod": Tone.Precision,
    "pride_mod": Tone.Pride,
    "profanity_mod": Tone.Profanity,
    "rage_mod": Tone.Rage,
    "regret_mod": Tone.Regret,
    "release_mod": Tone.Release,
    "remorse_mod": Tone.Remorse,
    "resentment_mod": Tone.Resentment,
    "resolve_mod": Tone.Resolve,
    "respect_mod": Tone.Respect,
    "restlessness_mod": Tone.Restlessness,
    "restraint_mod": Tone.Restraint,
    "sadness_mod": Tone.Sadness,
    "sarcasm_mod": Tone.Sarcasm,
    "satisfaction_mod": Tone.Satisfaction,
    "scorn_mod": Tone.Scorn,
    "seduction_mod": Tone.Seduction,
    "self_deprecation_mod": Tone.SelfDeprecation,
    "self_reflection_mod": Tone.SelfReflection,
    "sexual_arousal_mod": Tone.SexualArousal,
    "startle_mod": Tone.Startle,
    "stealth_mod": Tone.Stealth,
    "strain_mod": Tone.Strain,
    "stress_mod": Tone.Stress,
    "submission_mod": Tone.Submission,
    "surprise_mod": Tone.Surprise,
    "technical_mod": Tone.Technical,
    "tenderness_mod": Tone.Tenderness,
    "tranquility_mod": Tone.Tranquility,
    "trust_mod": Tone.Trust,
    "valence_mod": Tone.Valence,
    "warmth_mod": Tone.Warmth,
    "watchfulness_mod": Tone.Watchfulness,
    "wit_mod": Tone.Wit,
    "wonder_mod": Tone.Wonder,
    "worry_mod": Tone.Worry,
    "zeal_mod": Tone.Zeal,
    "loneliness_mod": Tone.Loneliness,
    "ecstasy_mod": Tone.Ecstasy,
    "trepidation_mod": Tone.Trepidation,
    "skepticism_mod": Tone.Skepticism,
    "tear_mod": Tone.Tear,
    "despair_mod": Tone.Despair,
    "reflection_mod": Tone.Reflection,
    "certainty_mod": Tone.Certainty,
    "comfort_mod": Tone.Comfort,
    "madness_mod": Tone.Madness,
    "carefree_mod": Tone.Carefree,
    "gloom_mod": Tone.Melancholy,
    "irritation_mod": Tone.Frustration,
    "annoyance_mod": Tone.Agitation,
    "apprehension_mod": Tone.Hesitation,
    "terror_mod": Tone.Horror,
    "panic_mod": Tone.Alarm,
    "revulsion_mod": Tone.Abhorrence,
    "loathing_mod": Tone.Detestation,
    "acceptance_mod": Tone.Approval,
    "admiration_mod": Tone.Esteem,
    "reliance_mod": Tone.Dependence,
    "eagerness_mod": Tone.Zeal,
    "vigilance_mod": Tone.Watchfulness,
    "interest_mod": Tone.Curiosity,
    "tension_mod": Tone.Strain,
    "overwhelm_mod": Tone.Collapse,
    "unease_mod": Tone.Discomfort,
    "swamped_mod": Tone.Exhaustion,
}