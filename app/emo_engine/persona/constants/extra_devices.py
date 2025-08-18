cat >app/emo_engine/persona/constants/extra_devices.py<< 'EOF'
#app/emo_engine/persona/constants/extra_devices.py
import random
from typing import Tuple, Set
from dataclasses import dataclass, field

BOT_SIGNATURES: Set[str] = set()
GLOBAL_DEVICE_PROB_SCALE = 0.5

@dataclass(frozen=True)
class RhetoricalDevice:
    name: str
    metric_key: str
    base_prob: float = 0.2
    threshold: float = 0.0
    exclusive_with: Tuple[str, ...] = field(default_factory=tuple)

    def should_apply(self, metric_val: float, rng: random.Random) -> bool:
        if metric_val < self.threshold:
            return False
        x = max(0.0, min(1.0, metric_val))
        k = 6.0
        sig = 1.0 / (1.0 + pow(2.71828, -k * (x - 0.5)))
        prob = GLOBAL_DEVICE_PROB_SCALE * self.base_prob * (0.2 + 0.8 * sig)
        return rng.random() < prob


BOT_SIGNATURES: Set[str] = {
    "MetaCommentary",
    "FrameReminder",
    "NarratorInterjection",
    "ReflectOnStyle",
    "AuthorialNote",
    "HighlightStructure",
    "SignpostTransition",
    "OfferSummary",
    "PromptAction",
    "SolicitInput",
    "EncourageFeedback",
    "OfferChoice",
    "AskClarifyingQuestion",
    "AskRhetoricalQuestion",
    "ReflectiveQuestion",
    "OfferGuidance",
    "OfferTip",
    "OfferAdvice",
    "IncludeStatistic",
    "ReferenceSource",
    "LinkToResource",
    "SoftCitation",
    "CiteAuthority",
    "CiteResearch",
    "OfferClarification",
    "UseDirectAddress",
    "UseInclusiveWe",
    "EmployRepetition",
    "Storytelling",
    "InsertAnecdote",
    "PersonalAnecdote",
    "SetScene",
    "PaintPicture",
    "UseSensoryDetails",
    "UseVividLanguage",
    "UseVividVerbs",
    "DescribeTexture",
    "ReferenceColor",
    "UseAnalogies",
    "UseAlliteration",
    "UseParallelism",
    "ChorusEffect",
    "VaryPunctuation",
    "UseEllipsis",
    "InsertPause",
    "ShareAmazement",
    "SparkEnthusiasm",
    "OfferEncouragement",
    "EncouragingPhrase",
    "BoostConfidence",
    "WarmPraise",
}

# -------------------------------------------------------------------
# 1. Sentence dynamics (Expanded)
EXTRA_DEVICES: Tuple[RhetoricalDevice, ...] = (
    RhetoricalDevice("VarySentenceLength", "energy_mod", base_prob=0.25, threshold=0.15),
    RhetoricalDevice("HighlightPace",         "energy_mod",     base_prob=0.03, threshold=0.18,
                     exclusive_with=("VarySentenceLength",)),
    RhetoricalDevice("BalanceLength",         "energy_mod",     base_prob=0.03, threshold=0.18,
                     exclusive_with=("HighlightPace",)),
    RhetoricalDevice("MemoryFollowUp",    "nostalgia_mod", base_prob=0.10, threshold=0.10),
    RhetoricalDevice("FillerPhrase",      "civility_mod",  base_prob=0.10, threshold=0.00),
    RhetoricalDevice("EmojiTouch",        "humor_mod",     base_prob=0.10, threshold=0.00),
)

# -------------------------------------------------------------------
# 2. Imagery & vividness (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("UseAnalogies",       "precision_mod",   base_prob=0.10, threshold=0.25),
    RhetoricalDevice("UseSensoryDetails",  "engagement_mod",  base_prob=0.10, threshold=0.18),
    RhetoricalDevice("UseVividVerbs",      "creativity_mod",  base_prob=0.10, threshold=0.30),
    RhetoricalDevice("UseVividLanguage",   "creativity_mod",  base_prob=0.15, threshold=0.25),

    # concrete imagery vs. abstract description
    RhetoricalDevice("PaintPicture",       "engagement_mod",  base_prob=0.08, threshold=0.18,
                     exclusive_with=("UseAnalogies","UseSensoryDetails")),

    # tactile focus vs. visual focus
    RhetoricalDevice("DescribeTexture",    "engagement_mod",  base_prob=0.07, threshold=0.18,
                     exclusive_with=("UseSensoryDetails",)),
    RhetoricalDevice("ReferenceColor",     "creativity_mod",  base_prob=0.06, threshold=0.18,
                     exclusive_with=("UseVividLanguage",)),
)


# -------------------------------------------------------------------
# 3. Empathy & warmth (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("EmpathicLabel",        "empathy_mod",    base_prob=0.05, threshold=0.35),
    RhetoricalDevice("ExpressEmpathy",       "compassion_mod", base_prob=0.05, threshold=0.35,
                     exclusive_with=("SharpRetort",)),
    RhetoricalDevice("ShowConcern",          "compassion_mod", base_prob=0.04, threshold=0.25),
    RhetoricalDevice("EchoUserPhrase",       "engagement_mod", base_prob=0.05, threshold=0.15),
    RhetoricalDevice("SoftenerPhrase",       "civility_mod",   base_prob=0.04, threshold=0.18,
                     exclusive_with=("BruisingTruth","SharpRetort")),
    RhetoricalDevice("ExpressCompliment",    "affection_mod",  base_prob=0.05, threshold=0.30),
    RhetoricalDevice("GentleAffection",      "affection_mod",  base_prob=0.05, threshold=0.35,
                     exclusive_with=("ExpressContempt",)),
    RhetoricalDevice("WarmPraise",           "gratitude_mod",  base_prob=0.04, threshold=0.25),

    # additional empathetic devices
    RhetoricalDevice("ExpressSympathy",      "empathy_mod",    base_prob=0.05, threshold=0.30,
                     exclusive_with=("SharpRetort",)),
    RhetoricalDevice("OfferEncouragement",   "compassion_mod", base_prob=0.05, threshold=0.30,
                     exclusive_with=("ExpressBurnout",)),
    RhetoricalDevice("UseKindLanguage",      "civility_mod",   base_prob=0.04, threshold=0.18,
                     exclusive_with=("SharpRetort","BruisingTruth")),
    RhetoricalDevice("OfferGratitude",       "gratitude_mod",  base_prob=0.04, threshold=0.18,
                     exclusive_with=("ExpressWorry",)),
    RhetoricalDevice("EchoEmotionalCue",     "engagement_mod", base_prob=0.04, threshold=0.18,
                     exclusive_with=("ExpressEmpathy",)),
)


# -------------------------------------------------------------------
# 4. Playfulness & humor (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("PlayfulTease",   "playfulness_mod",    base_prob=0.06, threshold=0.25),
    RhetoricalDevice("UseLaugh",        "joy_mod",            base_prob=0.03, threshold=0.15),
    RhetoricalDevice("TellJoke",       "humor_mod",          base_prob=0.05, threshold=0.25),
    RhetoricalDevice("DisplayWit",     "wit_mod",            base_prob=0.04, threshold=0.30),

    # light amusement vs. serious tone
    RhetoricalDevice("ShareAmusement",  "joy_mod",      base_prob=0.04, threshold=0.18,
                     exclusive_with=("ShowConcern","UseLaugh")),

    # self‑dep deprecation vs. empathy
    RhetoricalDevice("UseSelfDeprecation","humility_mod", base_prob=0.03, threshold=0.18,
                     exclusive_with=("ExpressEmpathy",)),

    # witty callback to user’s phrasing
    RhetoricalDevice("WittyCallback",   "wit_mod",            base_prob=0.03, threshold=0.25,
                     exclusive_with=("AskClarifyingQuestion",)),
)


# -------------------------------------------------------------------
# 5. Questions & engagement (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("AskRhetoricalQuestion",  "curiosity_mod",    base_prob=0.05, threshold=0.25),
    RhetoricalDevice("AskClarifyingQuestion",  "curiosity_mod",    base_prob=0.05, threshold=0.25,
                     exclusive_with=("WittyCallback",)),
    RhetoricalDevice("ReflectiveQuestion",     "curiosity_mod",    base_prob=0.04, threshold=0.25),
    RhetoricalDevice("UseRhetoricalCallback",  "engagement_mod",   base_prob=0.05, threshold=0.25),
)


# -------------------------------------------------------------------
# 6. Storytelling & anecdotes (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("Storytelling",         "charisma_mod",   base_prob=0.12, threshold=0.35),
    RhetoricalDevice("InsertAnecdote",       "charisma_mod",   base_prob=0.05, threshold=0.30),

    # personal anecdote for deeper connection
    RhetoricalDevice("PersonalAnecdote",     "charisma_mod",   base_prob=0.05, threshold=0.30,
                     exclusive_with=("Storytelling", "InsertAnecdote")),

    # illustrative example for clarity
    RhetoricalDevice("IllustrateExample",    "technical_mod",  base_prob=0.04, threshold=0.25,
                     exclusive_with=("InsertAnecdote",)),

    # vivid scene setting
    RhetoricalDevice("SetScene",             "creativity_mod", base_prob=0.04, threshold=0.25,
                     exclusive_with=("UseSensoryDetails",)),

    # rhetorical callback to past story
    RhetoricalDevice("StoryCallback",        "engagement_mod", base_prob=0.03, threshold=0.25,
                     exclusive_with=("ReflectiveQuestion",)),

    # transition from narrative to main point
    RhetoricalDevice("NarrativeTransition",  "technical_mod",  base_prob=0.03, threshold=0.25,
                     exclusive_with=("SignpostTransition",)),
)


# -------------------------------------------------------------------
# 7. Support & motivation (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("EncouragingPhrase", "optimism_mod",      base_prob=0.04, threshold=0.30),
    RhetoricalDevice("OfferTip",          "helpfulness_mod",   base_prob=0.05, threshold=0.25,
                     exclusive_with=("ExpressBurnout",)),
    RhetoricalDevice("CallToAction",      "motivation_mod",    base_prob=0.04, threshold=0.25,
                     exclusive_with=("SoftenerPhrase",)),
    RhetoricalDevice("OfferAdvice",       "helpfulness_mod",    base_prob=0.03, threshold=0.25,
                     exclusive_with=("ExpressDoubt",)),
    RhetoricalDevice("BoostConfidence",   "confidence_mod",    base_prob=0.03, threshold=0.25,
                     exclusive_with=("ExpressDoubt",)),
)


# -------------------------------------------------------------------
# 8. Structural & stylistic (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("SignpostTransition",  "technical_mod", base_prob=0.04, threshold=0.25),
    RhetoricalDevice("UseParallelism",       "creativity_mod", base_prob=0.05, threshold=0.30),
    RhetoricalDevice("UseAlliteration",      "creativity_mod", base_prob=0.04, threshold=0.30),

    # concise summary at section end
    RhetoricalDevice("OfferSummary",         "honesty_mod",    base_prob=0.04, threshold=0.25,
                     exclusive_with=("SignpostTransition",)),

    # reinforce key points through repetition
    RhetoricalDevice("EmployRepetition",     "emphasis_mod",   base_prob=0.03, threshold=0.25,
                     exclusive_with=("UseAlliteration",)),

    # gentle reminder of civility in transitions
    RhetoricalDevice("FrameReminder",        "civility_mod",   base_prob=0.03, threshold=0.25,
                     exclusive_with=("OfferSummary",)),
)


# -------------------------------------------------------------------
# 9. Data & authority (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("IncludeStatistic",   "technical_mod", base_prob=0.15, threshold=0.25,
                     exclusive_with=("PresentData",)),
    RhetoricalDevice("CiteAuthority",      "authority_mod", base_prob=0.15, threshold=0.30,
                     exclusive_with=("CiteResearch",)),
    RhetoricalDevice("CiteResearch",       "authority_mod", base_prob=0.15, threshold=0.30,
                     exclusive_with=("CiteAuthority",)),
    RhetoricalDevice("ReferenceSource",    "authority_mod", base_prob=0.13, threshold=0.25,
                     exclusive_with=("SoftCitation",)),
)


# -------------------------------------------------------------------
# 10. Contrast & conflict (Expanded)
EXTRA_DEVICES += (
    # original contrast & conflict devices
    RhetoricalDevice("ContrastJuxtaposition", "conflict_mod", base_prob=0.14, threshold=0.25,
                     exclusive_with=("OfferSummary","UseParallelism")),
    RhetoricalDevice("SharpRetort",           "anger_mod",    base_prob=0.13, threshold=0.45,
                     exclusive_with=("SoftenerPhrase","ExpressEmpathy")),
    RhetoricalDevice("BruisingTruth",         "honesty_mod",  base_prob=0.12, threshold=0.40,
                     exclusive_with=("SoftenerPhrase","ExpressEmpathy")),

    # highlight contradictions within an argument
    RhetoricalDevice("HighlightInconsistency","conflict_mod", base_prob=0.13, threshold=0.25,
                     exclusive_with=("EchoUserPhrase","OfferSummary")),

    # sharpen critique with sarcasm
    RhetoricalDevice("UseSarcasm",            "sarcasm_mod",  base_prob=0.13, threshold=0.30,
                     exclusive_with=("ExpressEmpathy","WarmPraise")),

    # deliver an unvarnished truth
    RhetoricalDevice("StateHardTruth",        "honesty_mod",  base_prob=0.13, threshold=0.30,
                     exclusive_with=("SoftenerPhrase",)),
)


# -------------------------------------------------------------------
# 11. Aggression & negativity (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("ExpressRage",          "rage_mod",         base_prob=0.13, threshold=0.55,
                     exclusive_with=("InvokeMalice", "InciteFury", "AmplifyAnger")),
    RhetoricalDevice("InvokeMalice",         "malice_mod",       base_prob=0.12, threshold=0.50,
                     exclusive_with=("ExpressRage", "ExpressEmpathy")),
    RhetoricalDevice("InciteFury",           "fury_mod",         base_prob=0.12, threshold=0.55,
                     exclusive_with=("ExpressRage", "ExpressEmpathy")),
    RhetoricalDevice("AmplifyAnger",         "anger_mod",        base_prob=0.13, threshold=0.50,
                     exclusive_with=("ExpressRage", "ExpressContempt")),
    RhetoricalDevice("ExpressContempt",      "contempt_mod",     base_prob=0.13, threshold=0.50,
                     exclusive_with=("ExpressRage", "GentleAffection")),
    # broadcast hatred vs. empathy
    RhetoricalDevice("BroadcastHatred",       "expresshatred_mod",base_prob=0.12, threshold=0.55,
                     exclusive_with=("InvokeMalice", "ExpressEmpathy")),
    # resentment vs. respect
    RhetoricalDevice("ExpressResentment",    "resentment_mod",   base_prob=0.13, threshold=0.40,
                     exclusive_with=("ExpressRespect",)),
)

# -------------------------------------------------------------------
# 12. Burnout & frustration (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("ExpressBurnout",           "burnout_mod",                  base_prob=0.03, threshold=0.35,
                     exclusive_with=("OfferComfort","ExpressEnergy")),
    RhetoricalDevice("DisplayFrustration",       "frustration_mod",              base_prob=0.04, threshold=0.35,
                     exclusive_with=("EvokeCalm",)),
    # exhaustion vs. enthusiasm
    RhetoricalDevice("ExpressExhaustion",        "exhaustion_mod",               base_prob=0.03, threshold=0.30,
                     exclusive_with=("SparkEnthusiasm",)),
    # fatigue vs. determination
    RhetoricalDevice("AcknowledgeFatigue",       "fatigue_mod",                  base_prob=0.03, threshold=0.30,
                     exclusive_with=("IgniteDetermination",)),
    # stress vs. patience
    RhetoricalDevice("HighlightStress",          "stress_mod",                   base_prob=0.03, threshold=0.25,
                     exclusive_with=("OfferPatience",)),
    # strain vs. comfort
    RhetoricalDevice("HighlightStrain",          "strain_mod",                   base_prob=0.03, threshold=0.25,
                     exclusive_with=("OfferComfort",)),
    # collapse vs. resolution
    RhetoricalDevice("AcknowledgeCollapse",      "collapse_mod",                 base_prob=0.02, threshold=0.30,
                     exclusive_with=("MarkResolution",)),
)


# -------------------------------------------------------------------
# 13. Charm & zeal (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("UseCharm",             "charm_mod",        base_prob=0.04, threshold=0.30),
    RhetoricalDevice("ExpressZeal",           "zeal_mod",         base_prob=0.04, threshold=0.30,
                         exclusive_with=("ExpressBurnout",)),

    # lighthearted merriment vs. solemn despair
    RhetoricalDevice("ShareMerriment",        "merriment_mod",    base_prob=0.03, threshold=0.18,
                         exclusive_with=("ExpressDespair",)),

    # friendly warmth vs. sharp contempt
    RhetoricalDevice("ShowFriendliness",      "friendliness_mod", base_prob=0.03, threshold=0.18,
                         exclusive_with=("ExpressContempt",)),

    # spark eagerness vs. curb pessimism
    RhetoricalDevice("SparkEnthusiasm",       "enthusiasm_mod",   base_prob=0.03, threshold=0.18,
                         exclusive_with=("ExpressPessimism",)),
)


# -------------------------------------------------------------------
# 14. Despair & remorse (Expanded)
EXTRA_DEVICES += (
    # original despair & remorse
    RhetoricalDevice("ExpressDespair",    "despair_mod",   base_prob=0.03, threshold=0.35,
                     exclusive_with=("ExpressHope",)),
    RhetoricalDevice("ExpressRemorse",    "remorse_mod",   base_prob=0.03, threshold=0.35,
                     exclusive_with=("OfferForgiveness",)),

    # guilt vs. relief
    RhetoricalDevice("AcknowledgeGuilt",  "guilt_mod",     base_prob=0.03, threshold=0.30,
                     exclusive_with=("OfferRelief",)),
    RhetoricalDevice("OfferRelief",       "compassion_mod",base_prob=0.03, threshold=0.30,
                     exclusive_with=("AcknowledgeGuilt",)),

    # regret vs. resolve
    RhetoricalDevice("ExpressRegret",     "regret_mod",    base_prob=0.03, threshold=0.30,
                     exclusive_with=("DemonstrateResolve",)),
    RhetoricalDevice("DemonstrateResolve","resolve_mod",   base_prob=0.03, threshold=0.30,
                     exclusive_with=("ExpressRegret",)),
)

# -------------------------------------------------------------------
# 15. Satisfaction & worry (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("ExpressSatisfaction", "satisfaction_mod", base_prob=0.04, threshold=0.25,
                     exclusive_with=("ExpressWorry",)),
    RhetoricalDevice("ExpressWorry",        "worry_mod",        base_prob=0.03, threshold=0.30,
                     exclusive_with=("ExpressSatisfaction",)),
    # gratitude softens worry
    RhetoricalDevice("ShareGratitude",      "gratitude_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("ExpressWorry",)),
    # patience counters impatience
    RhetoricalDevice("OfferPatience",       "patience_mod",     base_prob=0.03, threshold=0.18,
                     exclusive_with=("ExpressImpatience",)),
    RhetoricalDevice("ExpressImpatience",   "impatience_mod",   base_prob=0.03, threshold=0.18,
                     exclusive_with=("OfferPatience",)),
)


# -------------------------------------------------------------------
# 16. Pessimism & skepticism (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("ExpressPessimism", "dread_mod",      base_prob=0.03, threshold=0.35,
                     exclusive_with=("ShowOptimism",)),
    RhetoricalDevice("ShowSkepticism",  "skepticism_mod",  base_prob=0.03, threshold=0.35,
                     exclusive_with=("ExpressHope",)),

    # shift between doubt and confidence
    RhetoricalDevice("ExpressDoubt",     "doubt_mod",       base_prob=0.03, threshold=0.30,
                     exclusive_with=("AssertCertainty",)),
    RhetoricalDevice("AssertCertainty",   "certainty_mod",   base_prob=0.03, threshold=0.30,
                     exclusive_with=("ExpressDoubt",)),

    # tension between cynicism and trust
    RhetoricalDevice("ShowCynicism",     "cynicism_mod",    base_prob=0.03, threshold=0.30,
                     exclusive_with=("ExpressTrust",)),
    RhetoricalDevice("ExpressTrust",     "trust_mod",       base_prob=0.03, threshold=0.30,
                     exclusive_with=("ShowCynicism",)),

    # oscillation optimism vs. gloom
    RhetoricalDevice("ShiftToOptimism",  "optimism_mod",    base_prob=0.03, threshold=0.30,
                     exclusive_with=("ExpressPessimism",)),
    RhetoricalDevice("ExpressGloom",     "despair_mod",     base_prob=0.03, threshold=0.30,
                     exclusive_with=("ShiftToOptimism",)),
)


# -------------------------------------------------------------------
# 17. Remaining emotional tones
EXTRA_DEVICES += (
    RhetoricalDevice("BuildAnticipation", "anticipation_mod", base_prob=0.04, threshold=0.30),
    RhetoricalDevice("ExpressAnxiety", "anxiety_mod", base_prob=0.04, threshold=0.35),
    RhetoricalDevice("ShareNostalgicMemory", "nostalgia_mod", base_prob=0.04, threshold=0.30),
    RhetoricalDevice("ExpressHope", "hope_mod", base_prob=0.04, threshold=0.25),
    RhetoricalDevice("AcknowledgeSadness", "sadness_mod", base_prob=0.04, threshold=0.35),
    RhetoricalDevice("CelebrateJoy", "joy_mod", base_prob=0.04, threshold=0.25),
    RhetoricalDevice("ReferenceFear", "fear_mod", base_prob=0.03, threshold=0.35),
    RhetoricalDevice("InviteReflection", "reflection_mod", base_prob=0.03, threshold=0.30),
    RhetoricalDevice("ExpressBitterness", "bitterness_mod", base_prob=0.03, threshold=0.35),
)

# -------------------------------------------------------------------
# 18. Meta‑commentary
EXTRA_DEVICES += (
    RhetoricalDevice("MetaCommentary", "honesty_mod", base_prob=0.03, threshold=0.25),
    RhetoricalDevice("FrameReminder", "civility_mod", base_prob=0.03, threshold=0.25),
)

# -------------------------------------------------------------------
# 19. Rhythm & punctuation (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("ChorusEffect",      "engagement_mod", base_prob=0.03, threshold=0.25),
    RhetoricalDevice("VaryPunctuation",   "tension_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("ChorusEffect",)),
    RhetoricalDevice("UseEllipsis",       "tension_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("InsertPause","ChorusEffect")),
    RhetoricalDevice("InsertPause",       "tension_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("UseEllipsis","ChorusEffect")),
)


# -------------------------------------------------------------------
# 20. Interactivity (Expanded)
EXTRA_DEVICES += (
    RhetoricalDevice("PromptAction",         "motivation_mod",   base_prob=0.04, threshold=0.25),
    RhetoricalDevice("OfferChoice",          "authority_mod",    base_prob=0.03, threshold=0.25),

    # encourage feedback vs. direct call to action
    RhetoricalDevice("EncourageFeedback",    "engagement_mod",   base_prob=0.03, threshold=0.18,
                     exclusive_with=("PromptAction",)),
    RhetoricalDevice("SolicitInput",         "curiosity_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("OfferChoice",)),

    # offer guidance vs. promote autonomy
    RhetoricalDevice("OfferGuidance",        "helpfulness_mod",  base_prob=0.03, threshold=0.18,
                     exclusive_with=("SolicitInput",)),
    RhetoricalDevice("EncourageAutonomy",    "confidence_mod",   base_prob=0.03, threshold=0.18,
                     exclusive_with=("OfferGuidance",)),
)


# -------------------------------------------------------------------
# 21. Presence & address (Expanded)
EXTRA_DEVICES += (
    # existing direct address & personal touch
    RhetoricalDevice("UseDirectAddress",    "affection_mod",  base_prob=0.04, threshold=0.18),
    RhetoricalDevice("SharePersonalNote",   "trust_mod",      base_prob=0.03, threshold=0.18),

    # express respect vs. contempt
    RhetoricalDevice("ExpressRespect",      "respect_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("ExpressContempt",)),

    # show humility vs. blunt assertion
    RhetoricalDevice("ShowHumility",        "humility_mod",   base_prob=0.03, threshold=0.18,
                     exclusive_with=("SharpRetort",)),

    # inclusive “we” vs. direct “you”
    RhetoricalDevice("UseInclusiveWe",      "affection_mod",  base_prob=0.03, threshold=0.18,
                     exclusive_with=("UseDirectAddress",)),
)


# -------------------------------------------------------------------
# 22. Technical examples
EXTRA_DEVICES += (
    RhetoricalDevice("ShowExample", "technical_mod", base_prob=0.04, threshold=0.25),
    RhetoricalDevice("LinkToResource", "authority_mod", base_prob=0.03, threshold=0.30),
)

# -------------------------------------------------------------------
# 23. Mood transitions (Expanded)
EXTRA_DEVICES += (
    # original broad mood transitions
    RhetoricalDevice("MoodShift",       "valence_mod",    base_prob=0.03, threshold=0.25),
    RhetoricalDevice("EmotionalArc",    "charisma_mod",   base_prob=0.03, threshold=0.30),

    # optimism ↔ pessimism
    RhetoricalDevice("ShiftToOptimism", "optimism_mod",   base_prob=0.03, threshold=0.25,
                     exclusive_with=("ShiftToPessimism",)),
    RhetoricalDevice("ShiftToPessimism","dread_mod",      base_prob=0.03, threshold=0.25,
                     exclusive_with=("ShiftToOptimism",)),

    # serenity ↔ stress
    RhetoricalDevice("ShiftToSerenity", "tranquility_mod",base_prob=0.03, threshold=0.25,
                     exclusive_with=("ShiftToStress",)),
    RhetoricalDevice("ShiftToStress",   "stress_mod",     base_prob=0.03, threshold=0.25,
                     exclusive_with=("ShiftToSerenity",)),

    # joy ↔ sadness
    RhetoricalDevice("ShareJoy",        "joy_mod",        base_prob=0.03, threshold=0.25,
                     exclusive_with=("ExpressSadness",)),
    RhetoricalDevice("ExpressSadness",  "sadness_mod",    base_prob=0.03, threshold=0.25,
                     exclusive_with=("ShareJoy",)),

    # hope ↔ despair
    RhetoricalDevice("ShiftToHope",     "hope_mod",       base_prob=0.03, threshold=0.25,
                     exclusive_with=("ShiftToDespair",)),
    RhetoricalDevice("ShiftToDespair",  "despair_mod",    base_prob=0.03, threshold=0.25,
                     exclusive_with=("ShiftToHope",)),
)


# -------------------------------------------------------------------
# 24. Audiovisual & sensory
EXTRA_DEVICES += (
    RhetoricalDevice("UseOnomatopoeia", "engagement_mod", base_prob=0.03, threshold=0.18),
)

# -------------------------------------------------------------------
# 26. Amplification & Attenuation (Expanded)
EXTRA_DEVICES += (
    # original amplify/attenuate
    RhetoricalDevice("AmplifyIntensity",    "energy_mod",     base_prob=0.04, threshold=0.25,
                     exclusive_with=("AttenuateStatement",)),
    RhetoricalDevice("AttenuateStatement",  "civility_mod",   base_prob=0.04, threshold=0.18,
                     exclusive_with=("AmplifyIntensity",)),

    # amplify/attenuate emphasis
    RhetoricalDevice("AmplifyEmphasis",     "emphasis_mod",   base_prob=0.03, threshold=0.18,
                     exclusive_with=("AttenuateEmphasis", "AmplifyIntensity")),
    RhetoricalDevice("AttenuateEmphasis",   "emphasis_mod",   base_prob=0.03, threshold=0.18,
                     exclusive_with=("AmplifyEmphasis",  "AttenuateStatement")),

    # amplify/attenuate tension
    RhetoricalDevice("AmplifyTension",      "tension_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("AttenuateTension",   "AmplifyIntensity")),
    RhetoricalDevice("AttenuateTension",    "tension_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("AmplifyTension",    "AttenuateStatement")),

    # amplify/attenuate authority
    RhetoricalDevice("AmplifyAuthority",    "authority_mod",  base_prob=0.03, threshold=0.18,
                     exclusive_with=("AttenuateAuthority", "AmplifyIntensity")),
    RhetoricalDevice("AttenuateAuthority",  "authority_mod",  base_prob=0.03, threshold=0.18,
                     exclusive_with=("AmplifyAuthority",   "AttenuateStatement")),

    # amplify/attenuate conflict
    RhetoricalDevice("AmplifyConflict",     "conflict_mod",   base_prob=0.03, threshold=0.18,
                     exclusive_with=("AttenuateConflict",  "AmplifyIntensity")),
    RhetoricalDevice("AttenuateConflict",   "conflict_mod",   base_prob=0.03, threshold=0.18,
                     exclusive_with=("AmplifyConflict",   "AttenuateStatement")),
)


# -------------------------------------------------------------------
# -------------------------------------------------------------------
# 27. Micro‑shifts (Expanded)
EXTRA_DEVICES += (
    # оригинальные микроперепады
    RhetoricalDevice("GentleRise",         "enthusiasm_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("SubtleDip",)),
    RhetoricalDevice("SubtleDip",          "sadness_mod",       base_prob=0.03, threshold=0.18,
                     exclusive_with=("GentleRise",)),

    # лёгкая предвкушающая нота vs тень тревоги
    RhetoricalDevice("WhisperAnticipation","anticipation_mod",  base_prob=0.03, threshold=0.18,
                     exclusive_with=("ExpressAnxiety",)),
    RhetoricalDevice("FaintDread",         "dread_mod",         base_prob=0.03, threshold=0.18,
                     exclusive_with=("WhisperAnticipation",)),

    # мягкий импульс любопытства vs уверенное утверждение
    RhetoricalDevice("SubtleCuriosity",    "curiosity_mod",     base_prob=0.03, threshold=0.18,
                     exclusive_with=("AssertCertainty",)),
    RhetoricalDevice("AssertCertainty",    "certainty_mod",     base_prob=0.03, threshold=0.18,
                     exclusive_with=("SubtleCuriosity",)),

    # еле заметная тревожность vs тихое спокойствие
    RhetoricalDevice("SoftAnxietyTwinge",  "anxiety_mod",       base_prob=0.03, threshold=0.18,
                     exclusive_with=("EvokeCalm",)),
    RhetoricalDevice("EvokeCalm",          "calm_mod",          base_prob=0.03, threshold=0.18,
                     exclusive_with=("SoftAnxietyTwinge",)),

    # лёгкая рефлексия vs лёгкий призыв к действию
    RhetoricalDevice("WhisperReflection",  "reflection_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("SubtleMotivation",)),
    RhetoricalDevice("SubtleMotivation",   "motivation_mod",    base_prob=0.03, threshold=0.18,
                     exclusive_with=("WhisperReflection",)),
)


# -------------------------------------------------------------------
# 28. Temporal Anchors (Expanded)
EXTRA_DEVICES += (
    # anchor to the present moment vs foreshadow the future
    RhetoricalDevice("AnchorInPresent",    "alertness_mod",    base_prob=0.04, threshold=0.25,
                     exclusive_with=("ForeshadowFuture",)),
    RhetoricalDevice("ForeshadowFuture",   "anticipation_mod", base_prob=0.04, threshold=0.25,
                     exclusive_with=("AnchorInPresent",)),

    # recall a past moment to ground the narrative
    RhetoricalDevice("RecallMemory",       "nostalgia_mod",    base_prob=0.04, threshold=0.25,
                     exclusive_with=("ForeshadowFuture",)),

    # invite reflection on what’s come before
    RhetoricalDevice("EvokeReflection",    "reflection_mod",   base_prob=0.04, threshold=0.25,
                     exclusive_with=("ForeshadowFuture",)),

    # build suspense about what happens next
    RhetoricalDevice("InvokeSuspense",     "tension_mod",      base_prob=0.04, threshold=0.25,
                     exclusive_with=("AnchorInPresent",)),

    # offer a sense of resolution or certainty
    RhetoricalDevice("MarkResolution",     "certainty_mod",    base_prob=0.04, threshold=0.25,
                     exclusive_with=("InvokeSuspense",)),
)


# -------------------------------------------------------------------
# 30. Logical Connectors
EXTRA_DEVICES += (
    RhetoricalDevice("UseCausality", "technical_mod", base_prob=0.04, threshold=0.25),
    RhetoricalDevice("OfferContrast", "conflict_mod", base_prob=0.04, threshold=0.25),
)

# -------------------------------------------------------------------
# 31. Collective Voice
EXTRA_DEVICES += (
    RhetoricalDevice("UseInclusiveWe", "affection_mod", base_prob=0.03, threshold=0.18),
    RhetoricalDevice("PoseGroupQuestion", "curiosity_mod", base_prob=0.03, threshold=0.18),
)

# -------------------------------------------------------------------
# 32. Subtle Verification
EXTRA_DEVICES += (
    RhetoricalDevice("SoftCitation", "authority_mod", base_prob=0.03, threshold=0.25),
    RhetoricalDevice("OfferClarification", "curiosity_mod", base_prob=0.03, threshold=0.25),
)

# -------------------------------------------------------------------
# 33. Meta‑narrative (Expanded)
EXTRA_DEVICES += (
    # existing meta‑narrative devices
    RhetoricalDevice("NarratorInterjection", "charisma_mod", base_prob=0.03, threshold=0.25),
    RhetoricalDevice("ReflectOnStyle",       "honesty_mod",  base_prob=0.03, threshold=0.25,
                     exclusive_with=("MetaCommentary",)),

    # напоминание об форме и структуре текста
    RhetoricalDevice("FrameReminder",        "civility_mod", base_prob=0.03, threshold=0.25,
                     exclusive_with=("NarratorInterjection", "ReflectOnStyle")),

    # акцент на технической стороне повествования
    RhetoricalDevice("HighlightStructure",   "technical_mod", base_prob=0.03, threshold=0.25,
                     exclusive_with=("ReflectOnStyle",)),

    # авторская ремарка для установления доверия
    RhetoricalDevice("AuthorialNote",        "trust_mod",    base_prob=0.03, threshold=0.25,
                     exclusive_with=("MetaCommentary",)),
)


# -------------------------------------------------------------------
# 34. Positive / Comforting Tones
EXTRA_DEVICES += (
    RhetoricalDevice("ExpressCalm", "calm_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("ExpressAnxiety", "ExpressRage")),
    RhetoricalDevice("SharePeace", "peace_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("InvokeMalice", "InciteFury")),
    RhetoricalDevice("OfferComfort", "comfort_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("BruisingTruth", "SharpRetort")),
    RhetoricalDevice("ExpressContentment", "contentment_mod", base_prob=0.04, threshold=0.18,
                     exclusive_with=("ExpressBurnout", "ExpressDespair")),
    RhetoricalDevice("ShowOptimism", "optimism_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("ExpressPessimism",)),
)

# -------------------------------------------------------------------
# 35. Energizing / Motivational
EXTRA_DEVICES += (
    RhetoricalDevice("BuildCourage", "courage_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("ExpressFear", "ExpressDoubt")),
    RhetoricalDevice("IgniteDetermination", "determination_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("ExpressApathy",)),
    RhetoricalDevice("EncouragePersistence", "persistence_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("ExpressFatigue", "ExpressBurnout")),
    RhetoricalDevice("StimulateExcitement", "excitement_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("AnchorInPresent",)),
)

# -------------------------------------------------------------------
# 36. Intellectual / Reflective
EXTRA_DEVICES += (
    RhetoricalDevice("InviteWonder", "wonder_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("ShowSkepticism",)),
    RhetoricalDevice("PoseCaution", "caution_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("OfferContrast",)),
    RhetoricalDevice("FosterCuriosity", "interest_driven_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("OfferSummary",)),
    RhetoricalDevice("ShowFocus", "focus_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("MoodShift",)),
)

# -------------------------------------------------------------------
# 37. Challenging / Confrontational
EXTRA_DEVICES += (
    RhetoricalDevice("AcknowledgeFear", "fear_mod", base_prob=0.05, threshold=0.15,
                     exclusive_with=("ExpressCalm",)),
    RhetoricalDevice("ExpressDoubt", "doubt_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("OfferContrast",)),
    RhetoricalDevice("ConfrontChaos", "chaos_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("SignpostTransition",)),
    RhetoricalDevice("MapDistress", "distress_mod", base_prob=0.04, threshold=0.18,
                     exclusive_with=("OfferChoice",)),
)

# -------------------------------------------------------------------
# 38. Sensory / Arousal
EXTRA_DEVICES += (
    RhetoricalDevice("HighlightArousal", "arousal_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("UseAnalogies",)),
    RhetoricalDevice("InvokeTexture", "sensory_mod", base_prob=0.04, threshold=0.18,
                     exclusive_with=("UseVividLanguage",)),
)

# -------------------------------------------------------------------
# 39. Other
EXTRA_DEVICES += (
    RhetoricalDevice("ExpressBliss", "bliss_mod", base_prob=0.04, threshold=0.15),
    RhetoricalDevice("ConveyAlarm", "alarm_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("SoftenerPhrase","ExpressCalm","SharePeace")),
    RhetoricalDevice("ShareAmazement", "amazement_mod", base_prob=0.05, threshold=0.18),
    RhetoricalDevice("InvokeAwe", "awe_mod", base_prob=0.05, threshold=0.18),
    RhetoricalDevice("ExpressAlienation", "alienation_mod", base_prob=0.04, threshold=0.18,
                     exclusive_with=("UseInclusiveWe",)),
    RhetoricalDevice("ShareMelancholy", "melancholy_mod", base_prob=0.04, threshold=0.18),
    RhetoricalDevice("ConveySarcasm", "sarcasm_mod", base_prob=0.05, threshold=0.18,
                     exclusive_with=("ExpressEmpathy", "SoftenerPhrase", "WarmPraise")),
)


EXTRA_DEVICES = tuple(dev for dev in EXTRA_DEVICES if dev.name not in BOT_SIGNATURES)
EOF