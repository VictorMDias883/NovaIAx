"""
Safety hooks for AI answers.

Two responsibilities, deliberately independent of the model:

* **crisis / emergency detection** — a hard-coded, PT-BR pattern match over
  the user's message.  When it fires, the model's prose is discarded and the
  user gets a fixed message with the Brazilian helpline numbers (CVV 188,
  SAMU 192, Bombeiros 193, Polícia 190, Ligue 180).  No tools are proposed on
  a crisis turn and nothing is remembered.
* **medical / financial / legal disclaimers** — appended to the reply when the
  topic is touched, so the answer never reads as professional advice.

Detection is intentionally conservative: it matches a curated set of strong
phrases instead of loose keywords, to keep false positives low.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Fixed reply shown on a crisis/emergency turn.  The model never writes this.
CRISIS_REPLY = (
    "Percebi que você pode estar passando por um momento muito difícil. "
    "Você não está sozinho(a). Fale agora com alguém que pode ajudar:\n"
    "• CVV — apoio emocional 24h: ligue 188 (ligação gratuita) ou acesse cvv.org.br\n"
    "• SAMU — emergência médica: 192\n"
    "• Bombeiros: 193\n"
    "• Polícia: 190\n"
    "• Ligue 180 — Central de Atendimento à Mulher: 180\n"
    "Se você estiver em perigo imediato, ligue 190. "
    "Sua segurança vem primeiro; não vou propor nenhuma ação automática agora."
)

#: Machine-readable list of the helplines referenced by :data:`CRISIS_REPLY`.
#: Kept in sync with the reply and surfaced to clients that render the numbers
#: as tappable actions.
CRISIS_RESOURCES: tuple[dict[str, str], ...] = (
    {"name": "CVV", "description": "Apoio emocional 24h", "phone": "188"},
    {"name": "SAMU", "description": "Emergência médica", "phone": "192"},
    {"name": "Bombeiros", "description": "Corpo de Bombeiros", "phone": "193"},
    {"name": "Polícia", "description": "Polícia Militar", "phone": "190"},
    {"name": "Ligue 180", "description": "Central de Atendimento à Mulher", "phone": "180"},
)

CRISIS_WHY = "protocolo de seguranca: possivel crise/emergencia"

#: Patterns that indicate a personal crisis.  Matched case-insensitively.
_CRISIS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsuic[íi]d", re.IGNORECASE),
    re.compile(r"\bme\s+(matar|suicidar)\b", re.IGNORECASE),
    re.compile(r"\bvou\s+me\s+matar\b", re.IGNORECASE),
    re.compile(r"\bn[ãa]o\s+quero\s+(mais\s+)?viver\b", re.IGNORECASE),
    re.compile(r"\bquero\s+(morrer|desaparecer|acabar\s+com\s+a?\s*minha\s+vida)\b", re.IGNORECASE),
    re.compile(r"\btirar\s+(a\s+)?minha\s+vida\b", re.IGNORECASE),
    re.compile(r"\bautomutila", re.IGNORECASE),
    re.compile(r"\bme\s+(cortar|machucar|ferir)\b", re.IGNORECASE),
)

#: Patterns that indicate a medical emergency.
_EMERGENCY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bemerg[êe]ncia\s+m[ée]dica\b", re.IGNORECASE),
    re.compile(r"\b(infarto|avc|derrame|convuls|overdose|desmaiei)\b", re.IGNORECASE),
    re.compile(r"\bn[ãa]o\s+(consigo|estou\s+conseguindo)\s+respirar\b", re.IGNORECASE),
    re.compile(r"\bdor\s+no\s+peito\b", re.IGNORECASE),
    re.compile(r"\bsangramento\s+(forte|intenso)\b", re.IGNORECASE),
    re.compile(r"\bsocorro\b", re.IGNORECASE),
)

#: (label, disclaimer, topic patterns) triples.
_DISCLAIMER_TOPICS: tuple[tuple[str, str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "medical",
        "⚠️ Isto não é orientação médica. Não inicie, interrompa ou altere "
        "tratamentos sem falar com um profissional de saúde.",
        (
            re.compile(r"\brem[ée]di", re.IGNORECASE),
            re.compile(r"\bmedic", re.IGNORECASE),
            re.compile(r"\bdose\b", re.IGNORECASE),
            re.compile(r"\bsintoma", re.IGNORECASE),
            re.compile(r"\bdiagn[óo]stic", re.IGNORECASE),
            re.compile(r"\bdoen[çc]a", re.IGNORECASE),
            re.compile(r"\bm[ée]dic", re.IGNORECASE),
            re.compile(r"\bsa[úu]de\b", re.IGNORECASE),
            re.compile(r"\bpress[ãa]o\b", re.IGNORECASE),
            re.compile(r"\bglicemia", re.IGNORECASE),
        ),
    ),
    (
        "financial",
        "⚠️ Isto não é recomendação de investimento. Decisões financeiras são "
        "de sua responsabilidade; procure um profissional qualificado.",
        (
            re.compile(r"\binvest", re.IGNORECASE),
            re.compile(r"\ba[çc][õo]es\b", re.IGNORECASE),
            re.compile(r"\brenda\s+fixa\b", re.IGNORECASE),
            re.compile(r"\b(cripto|bitcoin)\b", re.IGNORECASE),
            re.compile(r"\bfinanc", re.IGNORECASE),
            re.compile(r"\bempr[ée]stimo\b", re.IGNORECASE),
            re.compile(r"\bimposto", re.IGNORECASE),
            re.compile(r"\baposentadoria\b", re.IGNORECASE),
        ),
    ),
    (
        "legal",
        "⚠️ Isto não constitui aconselhamento jurídico. Consulte um advogado "
        "para avaliar o seu caso.",
        (
            re.compile(r"\bprocesso\b", re.IGNORECASE),
            re.compile(r"\badvogad", re.IGNORECASE),
            re.compile(r"\bjur[íi]dic", re.IGNORECASE),
            re.compile(r"\bjusti[çc]a\b", re.IGNORECASE),
            re.compile(r"\bcontrato\b", re.IGNORECASE),
            re.compile(r"\bmulta\b", re.IGNORECASE),
            re.compile(r"\bcrime\b", re.IGNORECASE),
        ),
    ),
)


@dataclass
class SafetyResult:
    """Outcome of scanning one user message.

    Attributes:
        crisis: True for a personal crisis or medical emergency.
        disclaimers: PT-BR disclaimer strings for the topics touched (unique,
            in :data:`_DISCLAIMER_TOPICS` order).
        crisis_reply: The fixed reply to show instead of the model's prose when
            :attr:`crisis` is True.
    """

    crisis: bool = False
    disclaimers: list[str] = field(default_factory=list)
    crisis_reply: str | None = None


def scan(text: str) -> SafetyResult:
    """Run the crisis/emergency and disclaimer hooks over a user message."""
    if not text:
        return SafetyResult()

    crisis = any(pattern.search(text) for pattern in _CRISIS_PATTERNS)
    emergency = any(pattern.search(text) for pattern in _EMERGENCY_PATTERNS)
    if crisis or emergency:
        return SafetyResult(crisis=True, crisis_reply=CRISIS_REPLY)

    disclaimers: list[str] = []
    for _label, disclaimer, patterns in _DISCLAIMER_TOPICS:
        if any(pattern.search(text) for pattern in patterns):
            disclaimers.append(disclaimer)
    return SafetyResult(disclaimers=disclaimers)
