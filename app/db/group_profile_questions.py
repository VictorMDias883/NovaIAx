"""
Optional profile questions asked when the user opens an audience group.

The "Agentes Pessoais" page shows these questions for the chosen group
(profession, means of transport, field of work...).  Answers are stored as
ordinary user memories under the ``perfil_`` prefix, so they reach every
prompt through the existing memory block, appear on the "Memórias" screen and
are erased by "Apagar todas as memórias" / account deletion — no new table.

The questions live in code (keyed by group slug) and are served by
``GET /agent-groups``, so changing them needs a deploy but never an app
release.  A group without an entry simply has no questions.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Memory-key prefix for answers to group profile questions.
PROFILE_MEMORY_PREFIX = "perfil_"

#: Longest accepted answer (memories hold up to 500 chars; keep answers short).
PROFILE_ANSWER_MAX_LENGTH = 200


@dataclass(frozen=True)
class ProfileQuestion:
    """One optional question; ``key`` becomes the memory key ``perfil_<key>``."""

    key: str
    label: str
    hint: str = ""

    @property
    def memory_key(self) -> str:
        return f"{PROFILE_MEMORY_PREFIX}{self.key}"


_PROFESSION = ProfileQuestion("profissao", "Qual é a sua profissão ou função?", "Ex.: vendedora, enfermeiro, pedreiro")
_SCHEDULE = ProfileQuestion("horario_trabalho", "Como é o seu horário de trabalho?", "Ex.: comercial, 12x36, noturno")
_TRANSPORT = ProfileQuestion("meio_transporte", "Qual meio de transporte você usa?", "Ex.: carro, moto, bicicleta, ônibus")
_FIELD = ProfileQuestion("area_atuacao", "Qual é a sua área de atuação?", "Ex.: design, reformas, beleza, aulas")

GROUP_PROFILE_QUESTIONS: dict[str, tuple[ProfileQuestion, ...]] = {
    "religiosos": (
        ProfileQuestion(
            "fe",
            "Qual é a sua religião ou comunidade de fé? (opcional)",
            "Só se quiser contar — ajuda o agente a respeitar suas práticas",
        ),
    ),
    "estudantes": (
        ProfileQuestion("curso", "O que você estuda?", "Ex.: ensino médio, Direito, inglês, concurso"),
        ProfileQuestion("rotina_estudo", "Quando você costuma estudar?", "Ex.: à noite, depois do trabalho"),
    ),
    "trabalhadores_turnos": (_PROFESSION, _SCHEDULE, _TRANSPORT),
    "maes_solteiras": (
        ProfileQuestion("filhos", "Quantos filhos você tem e de que idades?", "Ex.: 2 — 4 e 9 anos"),
        _PROFESSION,
    ),
    "motoristas": (
        _TRANSPORT,
        ProfileQuestion("tipo_corrida", "Com o que você trabalha?", "Ex.: app de corrida, entregas, frete"),
    ),
    "autonomos_freelancers": (_FIELD, _TRANSPORT),
    "domesticas": (_SCHEDULE, _TRANSPORT),
    "babas": (ProfileQuestion("idades_criancas", "Qual a idade das crianças que você cuida?", "Ex.: 3 e 6 anos"),),
    "caminhoneiros": (ProfileQuestion("rotas", "Quais rotas você costuma fazer?", "Ex.: SP–BA, regional"),),
    "pequenos_negocios": (_FIELD,),
}


def questions_for(slug: str) -> tuple[ProfileQuestion, ...]:
    """Return the profile questions of the group ``slug`` (possibly empty)."""
    return GROUP_PROFILE_QUESTIONS.get(slug, ())
