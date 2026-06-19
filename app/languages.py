"""The language registry.

Adding a new language is a *config entry*, not a code change: define a
``Language`` describing how to name the source file, how to compile it (if at
all), and how to run it. The runner and grader are entirely language-agnostic —
they only ever consult this registry.

Design notes
------------
- ``compile_cmd`` runs inside the sandbox with the source mounted; a non-zero
  exit produces a Compilation Error (CE). For interpreted languages we still run
  a *compile* step (``py_compile``) purely to surface syntax errors as CE rather
  than as a Runtime Error on every test case.
- ``run_cmd`` is what the supervisor exec's for each test case. The compiled
  artifact (or source) lives at ``/work`` inside the container.
- Commands are argv lists (never shell strings) so there is no shell parsing or
  injection surface inside the sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    """Everything the judge needs to build and run a submission in one language."""

    name: str  # stable id used in the API and DB (e.g. "cpp", "python")
    display_name: str  # human-friendly label
    source_filename: str  # filename the submitted source is written to under /work
    run_cmd: list[str]  # argv to execute a graded run (cwd is a writable scratch dir)
    compile_cmd: list[str] | None = None  # argv to compile/syntax-check; None == nothing to do
    # If the compile step only validates (does not emit a binary the run step
    # needs), the run step reads the source directly. This flag is informational
    # for docs/UX; the runner does not branch on it.
    interpreted: bool = False


# C++: compiled with optimisations to a static-ish binary named ``main``.
# -O2 mirrors typical contest judge settings; gnu++17 enables common extensions.
_CPP = Language(
    name="cpp",
    display_name="C++17 (g++)",
    source_filename="main.cpp",
    compile_cmd=["g++", "-O2", "-pipe", "-std=gnu++17", "-o", "main", "main.cpp"],
    run_cmd=["./main"],
)

# Python 3: "compiled" via py_compile so a SyntaxError is reported as CE up front
# instead of failing identically on every test case as an RE.
_PYTHON = Language(
    name="python",
    display_name="Python 3",
    source_filename="main.py",
    compile_cmd=["python3", "-m", "py_compile", "main.py"],
    run_cmd=["python3", "main.py"],
    interpreted=True,
)


LANGUAGES: dict[str, Language] = {lang.name: lang for lang in (_CPP, _PYTHON)}


class UnknownLanguageError(KeyError):
    """Raised when a submission references a language not in the registry."""


def get_language(name: str) -> Language:
    try:
        return LANGUAGES[name]
    except KeyError as exc:
        supported = ", ".join(sorted(LANGUAGES))
        raise UnknownLanguageError(
            f"Unsupported language {name!r}. Supported languages: {supported}."
        ) from exc


def supported_languages() -> list[str]:
    return sorted(LANGUAGES)
