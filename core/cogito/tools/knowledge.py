"""`ask` and `recall`: the tools that let the agent read the store.

`ask` answers from the project's prose. `recall` answers from the conversation's own
history, including the parts compaction has folded away.

Searching costs no LLM tokens (Estella does it), which is why these beat pasting a document
into the prompt. How hard to look is decided by the ladder in `graph/recall.py`, not by the
tool descriptions: a retrieval policy written into a prompt holds only while the model
follows it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cogito.graph.recall import DocumentKnowledge, Knowledge, Recall
from cogito.provider.base import (
    PayloadShape,
    Provenance,
    Reproducibility,
    Retention,
    ToolSpec,
)
from cogito.tools.base import ToolContext, ToolResult, parameters

#: Code goes to the code graph and to `grep`. Estella extracts facts from sentences, and a
#: source file is not sentences.
_CODE_SUFFIXES = frozenset(
    {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h", ".cpp",
        ".hpp", ".cs", ".rb", ".php", ".sh", ".sql", ".toml", ".yaml", ".yml", ".json",
    }
)


def _looks_like_code_path(question: str) -> str:
    """The suffix of a code file, if the question is only a path to one.

    A question that mentions a file is fine. A question that is a filename is really a
    `read`, and answering it from the prose graph would give a confident wrong answer.
    """
    candidate = question.strip().strip("`\"'")
    if " " in candidate or "\n" in candidate:
        return ""
    suffix = Path(candidate).suffix.lower()
    return suffix if suffix in _CODE_SUFFIXES else ""


#: With `[estella] ask_code`. Cogito Estella 0.18's small-model probe found that saying
#: `ask` finds the lines even when the wording differs, which an exact search does not,
#: drew it where "call it first" had the model splitting its calls with grep.
_CODE_DESCRIPTION = (
    "Ask a question about this project — its source code, configuration and documentation — "
    "and get back the lines that answer it, with the file and line each one came from. It "
    "finds them even when the wording differs, which an exact text search does not, and "
    "costs far less context than reading files. Use read to see a whole file it points to."
)


class AskTool:
    """Questions about the project's documentation, answered from the prose graph."""

    def __init__(
        self,
        knowledge: Knowledge,
        *,
        code: bool = False,
        allowed: Callable[[], bool] | None = None,
    ) -> None:
        self._knowledge = knowledge
        #: APP-ONLY: the person's settings, read on every step («Documentación» in Búsqueda,
        #: «Memoria de documentos» in Herramientas), so turning one off takes the tool away
        #: from the next step on and turning it on gives it back. None: always shown.
        self._allowed = allowed
        self.spec = ToolSpec(
            name="ask",
            #: An answer over prose that is still being indexed, so it can move.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.COMPACT,
            ),
            description=_CODE_DESCRIPTION if code else (
                "Ask a question about this project's documentation — its README, design "
                "notes, guides and specifications — and get back the facts and the source "
                "sentences that answer it, with the file each one came from. Costs far "
                "less context than reading the documents. Prose only: for source code use "
                "grep or read."
            ),
            parameters=parameters(
                {
                    "question": {
                        "type": "string",
                        "description": (
                            "A full question, not keywords. 'How does the guard decide to "
                            "refuse?' beats 'guard refuse'."
                        ),
                    },
                    "budget": {
                        "type": "integer",
                        "description": (
                            "Token ceiling for the answer, 100-4000. Leave it out unless a "
                            "previous answer was too thin."
                        ),
                    },
                },
                required=("question",),
            ),
            aliases={"question": ("query", "q", "prompt", "text")},
            budget_tokens=900,
            core=True,
            summary="Answer a question from the project's documentation.",
        )

    def available(self) -> bool:
        """Shown unless the person's settings turned it off (APP-ONLY; always in `chat`)."""
        return self._allowed is None or self._allowed()

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        question = str(arguments.get("question", "")).strip()
        if not question:
            return ToolResult.error(
                "ask needs a question. Pass the whole question as `question`.", "bad_arguments"
            )
        if suffix := _looks_like_code_path(question):
            return ToolResult.error(
                f"ask reads prose, and {question!r} is a {suffix} file. Use read to see it, "
                f"or grep to find what mentions it.",
                "wrong_tool",
            )
        budget = arguments.get("budget")
        answer = self._knowledge.ask(question, int(budget) if budget else None)
        return ToolResult(
            content=answer.text or f"Nothing in the documentation answers {question!r}.",
            metadata={
                "rung": answer.rung,
                "found": answer.found,
                **({"confidence": answer.confidence} if answer.confidence is not None else {}),
            },
        )


class RecallTool:
    """Questions about this conversation, answered across its whole lineage."""

    def __init__(self, recall: Recall, *, allowed: Callable[[], bool] | None = None) -> None:
        self._recall = recall
        #: APP-ONLY, as `AskTool`'s: «Memoria de documentos», read on every step.
        self._allowed = allowed
        self.spec = ToolSpec(
            name="recall",
            #: Later events change what a search of the store returns, so this is mutable.
            #: Stable would need the query pinned to a sequence.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.COMPACT,
            ),
            description=(
                "Look something up in this conversation's own history, including the parts "
                "that have already been summarised away: what was decided, what was tried, "
                "what a command printed. Ask with `query`. When the answer comes back as a "
                "list of pointers, read one with `at` set to its `<session>:<seq>` address."
            ),
            parameters=parameters(
                {
                    "query": {
                        "type": "string",
                        "description": (
                            "What you are trying to remember, as a question or a phrase."
                        ),
                    },
                    "at": {
                        "type": "string",
                        "description": (
                            "A message address copied from a pointer list, e.g. 'K3F9:12'. "
                            "Returns that one message in full."
                        ),
                    },
                },
            ),
            aliases={"query": ("question", "q", "search", "text"), "at": ("address", "id")},
            budget_tokens=1000,
            core=True,
            # Before the first fold everything recall could reach is still in the window,
            # so its definition would cost tokens on every step for nothing.
            after_compaction=True,
            summary="Search this conversation's history, compacted parts included.",
        )

    def available(self) -> bool:
        """Shown unless the person's settings turned it off (APP-ONLY; always in `chat`)."""
        return self._allowed is None or self._allowed()

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        address = str(arguments.get("at", "")).strip()
        query = str(arguments.get("query", "")).strip()
        if not address and not query:
            return ToolResult.error(
                "recall needs either `query` (what to look for) or `at` (a message address "
                "from a pointer list).",
                "bad_arguments",
            )
        answer = self._recall.at(address) if address else self._recall.ask(query)
        return ToolResult(
            content=answer.text,
            metadata={
                "rung": answer.rung,
                "found": answer.found,
                **({"confidence": answer.confidence} if answer.confidence is not None else {}),
            },
        )


class DocumentAskTool:
    """Questions about a long document in the conversation, answered from its index with
    the page each line came from (`graph.recall.DocumentKnowledge`).

    Registered only when ``[estella] documents`` is on, or in the desktop app with
    ``[estella] documents_in_app``. Shown to the model only once the harness holds such a
    document: attached, opened by `read` and past the indexing threshold, or offloaded
    because it did not fit the window (`graph.documents.DocumentIndex`). As with
    `recall`'s ``after_compaction``, the harness decides from a fact it already holds and
    never asks the model.

    ``stable`` (APP-ONLY, a core the desktop started) shows it from the start, with a
    description that never changes. Appearing with the first document and naming each new
    one changed the tool list, which a llama.cpp or Motoris chat template renders before
    everything else, so the whole conversation was read again (cogito-lab
    research/2026-11-10-single-model-review). A call before any document is there says so.
    """

    NAME = "ask_document"

    def __init__(
        self,
        knowledge: DocumentKnowledge,
        index: Any,
        *,
        allowed: Callable[[], bool] | None = None,
        stable: bool = False,
    ) -> None:
        self._knowledge = knowledge
        self._index = index
        self._stable = stable
        self._spec: ToolSpec | None = None
        self._generation = -1
        #: APP-ONLY, as `AskTool`'s: «Memoria de documentos», read on every step.
        self._allowed = allowed

    def available(self) -> bool:
        """Whether the model is shown this tool now: while the person has not turned its
        memory off, and, unless ``stable``, only once a document is there."""
        if self._allowed is not None and not self._allowed():
            return False
        return self._stable or bool(self._index.documents())

    @property
    def spec(self) -> ToolSpec:
        # The description names the documents, so rebuild it only when that list changes
        # (once per new document, not once per step). A stable one is built once.
        if self._spec is None or (not self._stable and self._generation != self._index.generation):
            self._generation = self._index.generation
            self._spec = self._build_spec()
        return self._spec

    def _build_spec(self) -> ToolSpec:
        if self._stable:
            documents = (
                "A long document is an attachment, a file read or a result too large for "
                "the window."
            )
        else:
            named = "; ".join(d.short() for d in self._index.documents()[-6:]) or "none yet"
            documents = f"Documents: {named}."
        return ToolSpec(
            name=self.NAME,
            #: An answer from an index that is still being built, so it can move.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.COMPACT,
            ),
            description=(
                "Ask a question about a long document in this conversation and get back the "
                "passages that answer it, each with its page (or sheet or slide) and the read "
                "call that opens that page. Far less context than reading the document. "
                f"{documents} When it is not sure it says so and names the pages to read "
                "instead."
            ),
            parameters=parameters(
                {
                    "question": {
                        "type": "string",
                        "description": (
                            "A full question, e.g. 'What was the total for Q3 in the North "
                            "region?'."
                        ),
                    },
                    "document": {
                        "type": "string",
                        "description": (
                            "Which document, by name, when there is more than one. Default: "
                            "the most recent."
                        ),
                    },
                },
                required=("question",),
            ),
            aliases={
                "question": ("query", "q", "prompt", "text"),
                "document": ("path", "file", "file_path", "filename", "name"),
            },
            budget_tokens=900,
            core=True,
            summary="Answer a question from a long document, with pages.",
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        question = str(arguments.get("question", "")).strip()
        if not question:
            return ToolResult.error(
                "ask_document needs a question. Pass the whole question as `question`.",
                "bad_arguments",
            )
        if not self._index.documents():
            return ToolResult.error(
                "No long document is in this conversation yet. Use read or grep on files, "
                "and ask_document once an attachment or a result was too large for the "
                "window.",
                "no_document",
            )
        wanted = arguments.get("document")
        answer = self._knowledge.ask(question, str(wanted) if wanted else None)
        return ToolResult(
            content=answer.text,
            is_error=answer.rung == "address",
            error_kind="unknown_document" if answer.rung == "address" else "",
            metadata={
                "rung": answer.rung,
                "found": answer.found,
                **({"confidence": answer.confidence} if answer.confidence is not None else {}),
            },
        )
