"""
Shared identity rules and topic detection for every MeuAgente prompt.

The app is called **MeuAgente** and is developed by **NovaIAX**.  Every agent
(general companion, tool assistant, device assistant, objectives assistant and
the catalog sub-agents) appends :data:`IDENTITY_RULE` so the answer to "who
made you?" is consistent and a user-chosen agent name is respected.

:func:`mentions_objectives` decides whether a turn is about the user's goals.
The general companion only receives the objectives/roadmap context on those
turns, so it does not steer ordinary conversations towards planning.
"""

from __future__ import annotations

import re
import unicodedata

APP_NAME = "MeuAgente"
DEVELOPER_NAME = "NovaIAX"

IDENTITY_RULE = (
    "IDENTIDADE: você faz parte do aplicativo MeuAgente, desenvolvido pela NovaIAX. "
    "Se perguntarem quem te criou, desenvolveu ou fez, responda que foi a NovaIAX "
    "— nunca cite outra empresa, modelo ou provedor de IA. Se o usuário deu um nome "
    "próprio ao agente, use esse nome ao se apresentar; o aplicativo continua sendo "
    "o MeuAgente."
)

SUB_AGENT_SCOPE_RULE = (
    "ESPECIALIDADE: você é um Subagente de IA do MeuAgente e atua somente na sua "
    "especialidade descrita acima. Converse de forma natural. Não proponha criar "
    "objetivos ou planejamentos fora do seu tema. Se o pedido estiver fora da sua "
    "especialidade, responda brevemente e indique o chat principal do MeuAgente ou "
    "o Subagente de IA adequado."
)

#: Stems that make a message about goals, roadmaps or planned tasks.  Matched
#: on accent-stripped, lower-cased text at a word start.
_OBJECTIVE_STEMS = (
    "objetivo",
    "meta",
    "roadmap",
    "progresso",
    "planejamento",
    "plano",
    "prazo",
    "tarefa",
    "cumpri",
    "conclui",
    "pendencia",
    "pendente",
    "evolucao",
    "desempenho",
)
_OBJECTIVE_RE = re.compile(r"\b(?:" + "|".join(_OBJECTIVE_STEMS) + r")", re.IGNORECASE)
_ROADMAP_DAY_RE = re.compile(r"\bdias?\s+\d+\b|\bdo\s+dia\b", re.IGNORECASE)


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def mentions_objectives(message: str) -> bool:
    """Return ``True`` when ``message`` talks about goals, roadmap or tasks."""
    folded = _fold(message)
    return bool(_OBJECTIVE_RE.search(folded) or _ROADMAP_DAY_RE.search(folded))


def with_identity(prompt: str) -> str:
    """Append :data:`IDENTITY_RULE` to ``prompt`` (idempotent)."""
    if IDENTITY_RULE in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{IDENTITY_RULE}"


def scope_sub_agent_prompt(prompt: str) -> str:
    """Wrap an admin-authored sub-agent prompt with the scope and identity rules."""
    return with_identity(f"{prompt.rstrip()}\n\n{SUB_AGENT_SCOPE_RULE}")
