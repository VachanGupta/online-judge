"""Online judge: a sandboxed, resource-limited automated code-grading service.

The package is split into cleanly separated modules so each concern can be read,
tested, and swapped independently:

- ``config``      runtime settings (env-driven) and tunable limits
- ``enums``       the judge's vocabulary: verdicts, statuses, comparison modes
- ``languages``   the language registry (adding a language is a config entry)
- ``db``          SQLAlchemy engine/session wiring and SQLite pragmas
- ``models``      ORM models (Problem, TestCase, Submission, TestResult)
- ``schemas``     Pydantic request/response models for the API
- ``runner``      the Docker execution sandbox + resource-limit enforcement
- ``verdict``     pure output comparison + verdict classification
- ``grader``      compile -> run tests -> aggregate verdict -> persist
- ``queue``       DB-backed submission queue (atomic claim, stale reaping)
- ``worker``      the worker process loop / pool
- ``stress``      stress-test / counterexample finder
- ``main``        the FastAPI application
"""

__version__ = "0.1.0"
