"""
Auto-learning of long-lived memories from explicit user statements.

Learned memories come with three hard guards (all three are satisfied here):

1. **Explicit statements only** — the statement must be a first-person,
   non-interrogative, affirmative sentence ("Eu moro em ...", "Eu trabalho
   com ..."), so a mood or a passing question is never turned into a fact.
2. **Only with explicit consent and when learning is enabled** — the service
    checks the user's active ``learning`` consent and profile toggle.
3. **Sensitive topics are never stored** unless the user explicitly asks to
   save them — health, religion, sexual orientation, politics and finances
   statements are dropped unless the statement itself asks the assistant to
   remember ("quero que você salve isso", "anote que ...").

Extraction is deliberately deterministic (no extra AI call on top of the chat
turn), so behaviour is reproducible and cheap: it fires after a successful
conversation turn and up to three candidates per statement are saved.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consent import ConsentRecord
from app.models.user_memory import UserMemory
from app.repositories.user_agent_repository import UserAgentRepository
from app.repositories.user_memory_repository import UserMemoryRepository
from app.utils.slug import slugify

#: Maximum number of learned candidates saved from a single statement.
_MAX_CANDIDATES = 3

#: Minimum number of content words *after* the trigger for a sentence to count
#: as a memorable fact ("Eu sou." has none; "Eu moro em Lisboa." has two).
_MIN_CONTENT_WORDS = 1

#: First-person statement triggers.  The statement must be an ``eu <verbo>``
#: form; with an explicit save-intent ("quero que você salve/lembre ...") the
#: ``eu`` clause may appear anywhere in the sentence.
_EU_STATEMENT_RE = re.compile(
    r"\beu (sou|tenho|moro|vivo|trabalho|estudo|gosto|uso|odeio|adoro|amo|"
    r"prefiro|cuido|pratico|bebo|como|jogo|canto|desenho|escrevo|pinto|nado|corro)\b",
    re.IGNORECASE,
)

#: Feature triggers for possessive statements ("meu filho chama-se ...",
#: "meu aniversário é ...").  Analysed against the accent-stripped text.
_POSSESSIVE_STATEMENT_RE = re.compile(
    r"\b(meu|minha|meus|minhas)\s+"
    r"(nome|filho|filha|aniversario|animal|cachorro|cachorra|gato|gata|"
    r"time|cidade|bairro|endereco|telefone|email|cargo|profissao|salario|"
    r"meta|hobby|rotina)\b",
    re.IGNORECASE,
)

#: Verbs extracted from the possessive triggers to build a stable key.
_POSSESSIVE_FEATURES = {
    "nome": "nome",
    "filho": "filho",
    "filha": "filha",
    "aniversario": "aniversario",
    "animal": "animal",
    "cachorro": "animal",
    "cachorra": "animal",
    "gato": "animal",
    "gata": "animal",
    "time": "time",
    "cidade": "cidade",
    "bairro": "bairro",
    "endereco": "endereco",
    "telefone": "telefone",
    "email": "email",
    "cargo": "cargo",
    "profissao": "profissao",
    "salario": "salario",
    "meta": "meta",
    "hobby": "hobby",
    "rotina": "rotina",
}

# Normalised (accent-stripped) keywords per sensitive topic.  Token-equality
# matching means "dor" inside "dormir" is never flagged, only a real "dor".
_SENSITIVE_TOPIC_TOKENS: dict[str, frozenset[str]] = {
    "saúde": frozenset(
        {
            "saude",
            "doenca",
            "doencas",
            "diabetes",
            "diabete",
            "doutor",
            "medico",
            "medicos",
            "remedio",
            "remedios",
            "sintoma",
            "sintomas",
            "dieta",
            "peso",
            "cirurgia",
            "exame",
            "exames",
            "vacina",
            "dor",
            "dores",
            "alergia",
        }
    ),
    "religião": frozenset(
        {
            "igreja",
            "religiao",
            "deus",
            "crente",
            "crentes",
            "rezar",
            "culto",
            "oracao",
            "candomble",
            "umbanda",
            "espirita",
            "evangelico",
            "evangelica",
            "catolico",
            "catolica",
            "muculmano",
        }
    ),
    "orientação sexual": frozenset(
        {
            "orientacao",
            "orientacao-sexual",
            "sexual",
            "homossexual",
            "gay",
            "lesbica",
            "bissexual",
            "trans",
            "lgbt",
        }
    ),
    "política": frozenset(
        {
            "politica",
            "partido",
            "presidente",
            "presidenciais",
            "eleicao",
            "eleicoes",
            "voto",
            "governo",
            "vereador",
            "vereadora",
            "deputado",
            "deputada",
        }
    ),
    "finanças": frozenset(
        {
            "salario",
            "salarios",
            "dinheiro",
            "renda",
            "banco",
            "bancos",
            "divida",
            "dividas",
            "cartao",
            "fatura",
            "investimento",
            "investimentos",
            "pix",
            "contas",
            "emprestimo",
            "emprestimos",
            "hipoteca",
        }
    ),
}

#: A flat set of every sensitive token (topic labels are only for the reason).
_SENSITIVE_TOKENS: frozenset[str] = frozenset(
    token for tokens in _SENSITIVE_TOPIC_TOKENS.values() for token in tokens
)

#: Words/signals that ask the assistant to persist the statement.
_EXPLICIT_SAVE_RE = re.compile(
    r"\b(salv[ae]|guarde|guarda|lembre|memoriz[ae]|anote|anota|registre)", re.IGNORECASE
)

#: Sentence splitter: statements end at ``. ! ? ;`` or a newline.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?;\n]+")

#: Minimum number of content words *after* the trigger for a sentence to count
#: as a memorable fact ("Eu sou." has none; "Eu moro em Lisboa." has two).
_MIN_CONTENT_WORDS = 1

_RESERVED_PREFIX = "auto_"


def _normalize_casefold(text: str) -> str:
    """Strip accents and lower-case ``text`` for accent-insensitive matching."""
    deaccented = "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
    )
    return deaccented.casefold()


def _tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", _normalize_casefold(text)))


def _features(sentence: str) -> tuple[str, str] | None:
    """Return ``(key_prefix, verb)`` when ``sentence`` is an explicit statement.

    ``key_prefix`` is the stable group used to build the memory key, ``verb``
    the matched trigger word.  Returns ``None`` for questions or moods.
    """
    stripped = sentence.strip()
    if not stripped or "?" in stripped:
        return None

    norm = _normalize_casefold(stripped)
    intent = _explicit_save_intent(stripped)

    # With an explicit save-intent the ``eu ...`` clause may appear anywhere
    # in the sentence ("quero que você salve que eu moro em ..."); otherwise
    # it must open the sentence ("Eu moro em ...").
    eu = _EU_STATEMENT_RE.match(norm)
    if eu is None and intent:
        eu = _EU_STATEMENT_RE.search(norm)
    if eu is not None:
        verb = eu.group(1)
        rest_words = [word for word in re.split(r"\s+", norm[eu.end() :]) if word]
        if len(rest_words) < _MIN_CONTENT_WORDS:
            return None
        prefix = " ".join(rest_words[:2]) or verb
        return f"{verb} {prefix}".strip(), verb

    poss = _POSSESSIVE_STATEMENT_RE.match(norm)
    if poss is None and intent:
        poss = _POSSESSIVE_STATEMENT_RE.search(norm)
    if poss is not None:
        feature = _POSSESSIVE_FEATURES.get(poss.group(2), "info")
        rest_words = [word for word in re.split(r"\s+", norm[poss.end() :]) if word]
        if len(rest_words) < _MIN_CONTENT_WORDS:
            return None
        prefix = " ".join(rest_words[:2])
        return f"{feature} {prefix}".strip(), feature

    return None


def _is_sensitive(sentence: str) -> bool:
    return bool(_tokens(sentence) & _SENSITIVE_TOKENS)


def _explicit_save_intent(sentence: str) -> bool:
    return _EXPLICIT_SAVE_RE.search(sentence) is not None


def extract_candidates(statement: str) -> list[tuple[str, str]]:
    """Turn ``statement`` into ``(key, value)`` learned-memory candidates.

    A candidate requires an explicit first-person statement, a non-question,
    enough words to be a fact and — for sensitive topics — an explicit request
    to save it.  Up to :data:`_MAX_CANDIDATES` candidates are returned, in
    statement order.
    """
    candidates: list[tuple[str, str]] = []
    for sentence in _SENTENCE_SPLIT_RE.split(statement):
        if len(candidates) >= _MAX_CANDIDATES:
            break
        sentence = sentence.strip()
        if not sentence:
            continue
        features = _features(sentence)
        if features is None:
            continue
        if _is_sensitive(sentence) and not _explicit_save_intent(sentence):
            continue
        key_prefix, _verb = features
        key = f"{_RESERVED_PREFIX}{slugify(key_prefix, max_length=50)}"
        if not key or key == _RESERVED_PREFIX:
            continue
        value = re.sub(r"\s+", " ", sentence).strip()
        candidates.append((key, value[:500]))
    return candidates


class MemoryLearningService:
    """Service that persists explicit statements as learned memories."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.profile_repo = UserAgentRepository(session)
        self.memory_repo = UserMemoryRepository(session)

    async def learn_from_statement(self, user_id: int, statement: str) -> list[UserMemory]:
        """Persist learned memories from ``statement`` (best-effort).

        No-op when ``statement`` is empty, the user has not granted the
        ``learning`` consent, the profile disables learning, or the statement
        is not an explicit, non-sensitive (or explicitly-to-be-saved)
        first-person fact. The profile row is created lazily when needed.

        Returns the saved/refreshed :class:`UserMemory` rows.
        """
        if not statement or not statement.strip():
            return []

        consent = await self.session.execute(
            select(ConsentRecord.id).where(
                ConsentRecord.user_id == user_id,
                ConsentRecord.type == "learning",
            )
        )
        if consent.scalar_one_or_none() is None:
            return []

        profile, _ = await self.profile_repo.get_or_create(user_id)
        if not profile.learning_enabled:
            return []

        saved: list[UserMemory] = []
        for key, value in extract_candidates(statement):
            saved.append(await self.memory_repo.upsert_learned(user_id, key, value))
        await self.session.commit()
        return saved
