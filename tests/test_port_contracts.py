"""Every implementation of a registered port runs that port's contract tests.

A fake that drifts from the production adapter lets a defect pass every test
that uses the fake. Per port, the registry names the methods that identify an
implementation, the contract test mixin, and each implementation (the
production adapter and the fakes) with the contract test class that runs it.
"""
import ast
import inspect
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

from engine_support import SRC


TESTS = Path(__file__).resolve().parent
# Each scanned directory and the directory its modules are named from: the source of both packages, and
# every test with the doubles it holds.
SCANNED = {Path(SRC): Path(SRC), TESTS: TESTS}

PORTS = {
    'agent-turns': {
        'methods': frozenset({'known', 'run', 'end_session', 'close'}),
        'contract': 'test_transport_agents.AgentTurnsContract',
        'implementations': {
            'loop_engine.composition.TransportAgents': 'test_transport_agents.TransportAgentsTest',
            'engine_support.ScriptedAgents': 'test_transport_agents.ScriptedAgentsContractTest',
        },
    },
    'engine-client': {
        'methods': frozenset({'request', 'status', 'wait', 'result'}),
        'contract': 'test_unit_tools.EngineClientContract',
        'implementations': {
            # the entry's client is a module of functions, not a class
            'loop_engine.client': 'test_unit_tools.EntryClientContractTest',
            'loop_engine.testing.FakeEngineClient': 'test_unit_tools.FakeEngineClientContractTest',
        },
    },
    'protocol-driver': {
        'methods': frozenset({'host_key', 'run_turn', 'progress', 'cancel', 'release'}),
        'contract': 'transport.test_protocol_drivers.ProtocolDriverContract',
        'implementations': {
            'provider_transport.codex_driver.CodexDriver':
                'transport.test_protocol_drivers.CodexDriverContractTests',
            'provider_transport.acp_driver.AcpDriver':
                'transport.test_protocol_drivers.AcpDriverContractTests',
            'provider_transport.pi_driver.PiDriver':
                'transport.test_protocol_drivers.PiDriverContractTests',
            'transport.protocol_driver_fake.FakeDriver':
                'transport.test_protocol_drivers.FakeDriverContractTests',
            # the scripted driver the agent port's adapter is tested over
            'test_transport_agents.Driver': 'test_transport_agents.ScriptedDriverContractTests',
        },
    },
    'process-cell': {
        'methods': frozenset({'spawn', 'confirm', 'process_identity', 'process_exists', 'release_certain',
                              'terminate_roots', 'terminate_root', 'terminate_all', 'resolve_unconfirmed_empty',
                              'wait_empty', 'close'}),
        'contract': 'transport.test_cell_membership.CellMembershipContract',
        'implementations': {
            'provider_transport.linux_process.ProcSubreaperCell':
                'transport.test_cell_membership.LinuxCellContractTests',
            'provider_transport.windows_process.WindowsJobCell':
                'transport.test_cell_membership.WindowsJobCellContractTests',
            'provider_transport.testing.cell_fake.FakeCellMembership':
                'transport.test_cell_membership.FakeCellContractTests',
        },
    },
    'working-directory-authority': {
        'methods': frozenset({'admit', 'close'}),
        'contract': 'transport.test_cwd_authority.CwdAuthorityContract',
        'implementations': {
            'provider_transport.posix_filesystem.PosixDirectoryAuthority':
                'transport.test_cwd_authority.PosixDirectoryAuthorityContractTests',
            'provider_transport.windows_filesystem.WindowsDirectoryAuthority':
                'transport.test_cwd_authority.WindowsDirectoryAuthorityContractTests',
            'provider_transport.testing.directory_fake.FakeCwdAuthority':
                'transport.test_cwd_authority.FakeCwdAuthorityContractTests',
        },
    },
}

# A port registry of the fixtures below, for the checks that follow a registered double through its subclasses.
FIXTURE_PORTS = {
    'execution-host': {
        'methods': frozenset({'call', 'wait'}),
        'contract': 'port_double_fixture.contract.HostContract',
        'implementations': {'fixture.fakes.ScriptedHost': 'port_double_fixture.contract.HostContract'},
    },
}


def _load(name):
    module, _, attribute = name.rpartition('.')
    return getattr(import_module(module), attribute)


def contract_violations(ports):
    """Implementations whose registered test class does not run the port contract on them."""
    violations = []
    for port, row in sorted(ports.items()):
        contract = _load(row['contract'])
        first_test = min(name for name in dir(contract) if name.startswith('test_'))
        for implementation, test_class in sorted(row['implementations'].items()):
            case_class = _load(test_class)
            if not (issubclass(case_class, contract) and issubclass(case_class, unittest.TestCase)):
                violations.append(f'{port}: {test_class} does not run {row["contract"]}')
                continue
            case = case_class(first_test)
            case.setUp()
            try:
                host = case.open_host()
                try:
                    expected = _load(implementation)
                    if not (host is expected if inspect.ismodule(expected) else isinstance(host, expected)):
                        violations.append(f'{port}: {test_class} opens {type(host).__name__}, not {implementation}')
                finally:
                    case.close_host(host)
            finally:
                case.doCleanups()
    return violations


def _module_name(path, base):
    return '.'.join(path.relative_to(base).with_suffix('').parts)


def unregistered_implementations(ports, roots):
    """Classes that define every method of a registered port but are not registered implementations.

    A subclass that defines only some of the methods keeps its registered parent's behavior
    for the rest; one that defines all of them is a new implementation.
    """
    found = []
    for root, base in roots.items():
        for path in sorted(root.rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            module = _module_name(path, base)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if any(ast.unparse(item).rpartition('.')[2] == 'Protocol' for item in node.bases):
                    continue  # the port's declaration, not an implementation
                defined = {item.name for item in node.body
                           if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
                for port, row in sorted(ports.items()):
                    name = f'{module}.{node.name}'
                    if row['methods'] <= defined and name not in row['implementations']:
                        found.append(f'{port}: {name}')
    return found


def _delegates(function, method):
    """True when the function calls ``super().<method>``."""
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == method
               and isinstance(node.func.value, ast.Call) and getattr(node.func.value.func, 'id', None) == 'super'
               for node in ast.walk(function))


def _classes(roots):
    """Every class in the scanned files with its base names, and every imported alias."""
    classes, aliases = [], {}
    for root, base in roots.items():
        for path in sorted(root.rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            module = _module_name(path, base)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = {ast.unparse(item).rpartition('.')[2] for item in node.bases}
                    classes.append((f'{module}.{node.name}', node, bases))
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.asname:
                            aliases.setdefault(alias.name, set()).add(alias.asname)
    return classes, aliases


def replaced_methods(ports, roots):
    """Descendants of a registered implementation whose port method never reaches the implementation.

    A descendant may add behavior around a port method, but one that replaces it is an
    unregistered implementation of that method. Descent follows class bases and import
    aliases by name across the scanned files.
    """
    classes, aliases = _classes(roots)
    found = []
    for port, row in sorted(ports.items()):
        names = {name.rpartition('.')[2] for name in row['implementations']}
        while True:
            grown = names | {alias for name in names for alias in aliases.get(name, ())}
            grown |= {qualified.rpartition('.')[2] for qualified, _, bases in classes if bases & grown}
            if grown == names:
                break
            names = grown
        for qualified, node, bases in classes:
            if qualified in row['implementations'] or not bases & names:
                continue
            for item in node.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in row['methods']
                        and not _delegates(item, item.name)):
                    found.append(f'{port}: {qualified}.{item.name}')
    return found


class PortContractTests(unittest.TestCase):
    def test_every_registered_implementation_runs_its_port_contract(self):
        self.assertEqual([], contract_violations(PORTS))

    def test_no_class_implements_a_registered_port_without_its_contract(self):
        self.assertEqual([], unregistered_implementations(PORTS, SCANNED))

    def test_no_subclass_replaces_a_registered_port_method(self):
        self.assertEqual([], replaced_methods(PORTS, SCANNED))

    def test_a_subclass_that_replaces_a_port_method_is_reported(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'fixture').mkdir()
            (base / 'fixture' / 'doubles.py').write_text(
                'class Denying(ScriptedHost):\n'
                '    def call(self, name, arguments):\n'
                '        if name == "start":\n            return {}\n'
                '        return super().call(name, arguments)\n'
                'class Replacing(ScriptedHost):\n'
                '    def wait(self, execution_id, timeout):\n        return True\n'
                'class Grandchild(Denying):\n'
                '    def wait(self, execution_id, timeout):\n        return True\n'
                'from fixture.fakes import ScriptedHost as Host\n'
                'class Aliased(Host):\n'
                '    def call(self, name, arguments):\n        return {}\n')
            self.assertEqual(['execution-host: fixture.doubles.Replacing.wait',
                              'execution-host: fixture.doubles.Grandchild.wait',
                              'execution-host: fixture.doubles.Aliased.call'],
                             replaced_methods(FIXTURE_PORTS, {base / 'fixture': base}))

    def test_an_unregistered_implementation_is_reported(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'fixture').mkdir()
            (base / 'fixture' / 'doubles.py').write_text(
                'class LooseHost:\n'
                '    def call(self, name, arguments):\n        return {}\n'
                '    def wait(self, execution_id, timeout):\n        return True\n'
                'class Tuned(ScriptedHost):\n'
                '    def call(self, name, arguments):\n        return {}\n'
                '    def wait(self, execution_id, timeout):\n        return True\n'
                'class Denying(ScriptedHost):\n'
                '    def call(self, name, arguments):\n        return {}\n'
                'class Host(Protocol):\n'
                '    def call(self, name, arguments): ...\n'
                '    def wait(self, execution_id, timeout): ...\n')
            self.assertEqual(['execution-host: fixture.doubles.LooseHost', 'execution-host: fixture.doubles.Tuned'],
                             unregistered_implementations(FIXTURE_PORTS, {base / 'fixture': base}))

    def test_a_contract_class_that_opens_another_implementation_is_reported(self):
        with TemporaryDirectory() as directory:
            package = Path(directory) / 'port_double_fixture'
            package.mkdir()
            (package / '__init__.py').write_text('')
            (package / 'fakes.py').write_text(
                'class ScriptedHost:\n'
                '    def call(self, name, arguments):\n        return {}\n'
                '    def wait(self, execution_id, timeout):\n        return True\n'
                'class OtherHost:\n    pass\n')
            (package / 'contract.py').write_text(
                'import unittest\n'
                'from port_double_fixture.fakes import OtherHost\n'
                'class HostContract:\n'
                '    def open_host(self):\n        return OtherHost()\n'
                '    def close_host(self, host):\n        pass\n'
                '    def test_call(self):\n        pass\n'
                'class OpensOther(HostContract, unittest.TestCase):\n    pass\n')
            sys.path.insert(0, directory)
            self.addCleanup(sys.path.remove, directory)
            self.addCleanup(lambda: [sys.modules.pop(name, None) for name in list(sys.modules)
                                     if name.startswith('port_double_fixture')])
            registry = {'execution-host': {**FIXTURE_PORTS['execution-host'], 'implementations': {
                'port_double_fixture.fakes.ScriptedHost': 'port_double_fixture.contract.OpensOther'}}}
            violations = contract_violations(registry)
        self.assertEqual(1, len(violations), violations)
        self.assertIn('opens OtherHost', violations[0])


if __name__ == '__main__':
    unittest.main()
