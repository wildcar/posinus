"""Guard the stdlib-only invariant.

The pipeline deploys as a plain file copy to /opt/posinus/pipeline with no venv to
maintain, which only holds while its modules import nothing but the standard
library and each other. Living in the same repository as the Django crawler makes
an accidental third-party import easy, so assert it instead of trusting review.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent
MODULES = ("evaluator.py", "preparer.py", "publisher.py", "runlog.py", "notify.py",
           "apply_edits.py", "retention.py", "daypic.py")

# The pipeline modules import each other: preparer uses evaluator's router client,
# publisher uses preparer's own-DB schema and markdown builder, and all three
# record their runs through runlog.
LOCAL_MODULES = {path.removesuffix(".py") for path in MODULES}


def top_level_imports(source: str) -> set[str]:
    """Root package name of every import in the file, at any nesting depth."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) resolves inside this directory.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


class StdlibOnlyTest(unittest.TestCase):
    def test_modules_import_only_stdlib_and_each_other(self):
        allowed = set(sys.stdlib_module_names) | LOCAL_MODULES | {"__future__"}
        for filename in MODULES:
            path = PIPELINE_DIR / filename
            with self.subTest(module=filename):
                imported = top_level_imports(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    imported - allowed,
                    set(),
                    f"{filename} imports outside the standard library; the pipeline "
                    f"deploys as a file copy with no venv (see pipeline/AGENTS.md)",
                )

    def test_pipeline_does_not_import_the_crawler(self):
        """The exchange contract is the only interface: no Django models, no shared code."""
        forbidden = {"collector", "posinus_crawler", "django"}
        for filename in MODULES:
            path = PIPELINE_DIR / filename
            with self.subTest(module=filename):
                imported = top_level_imports(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    imported & forbidden,
                    set(),
                    f"{filename} reaches into the crawler; talk to it through the "
                    f"exchange_* SQL contract only (see docs/contracts/)",
                )


if __name__ == "__main__":
    unittest.main()
