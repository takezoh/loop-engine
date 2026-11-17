"""The provider transport knows no caller: it imports the standard library and itself, and it reads
no caller's skill configuration. The skills to exclude reach it as a value."""
import ast
from pathlib import Path
import re
import sys
import unittest

from provider_transport import provider_catalog

TRANSPORT_ROOT = Path(provider_catalog.__file__).resolve().parent
SKILL_CONFIG_READ = re.compile(r"['\"]skills/")
DYNAMIC_IMPORT = re.compile(r"(__import__|import_module|importlib)")


def sources():
    found = sorted(TRANSPORT_ROOT.rglob("*.py"))
    assert found
    return found


class CallerIndependenceTests(unittest.TestCase):
    def test_transport_modules_import_only_the_standard_library_and_the_transport(self):
        findings = []
        for path in sources():
            rel = path.relative_to(TRANSPORT_ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom):
                    names = [] if node.level else [node.module or ""]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    continue
                for name in names:
                    top = name.split(".", 1)[0]
                    if top != "provider_transport" and top not in sys.stdlib_module_names:
                        findings.append(f"{rel}:{node.lineno}:{name}")
            for lineno, line in enumerate(text.splitlines(), 1):
                if DYNAMIC_IMPORT.search(line):
                    findings.append(f"{rel}:{lineno}:dynamic-import")
        self.assertEqual([], findings)

    def test_transport_does_not_read_skill_configuration(self):
        findings = []
        for path in sources():
            rel = path.relative_to(TRANSPORT_ROOT).as_posix()
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if SKILL_CONFIG_READ.search(line):
                    findings.append(f"{rel}:{lineno}:{line.strip()}")
        self.assertEqual([], findings)

    def test_the_catalog_names_no_skill_to_exclude(self):
        text = provider_catalog.CATALOG_PATH.read_text(encoding="utf-8")
        self.assertNotIn("excluded_skills", text)
        self.assertNotIn('"names"', text)


if __name__ == "__main__":
    unittest.main()
