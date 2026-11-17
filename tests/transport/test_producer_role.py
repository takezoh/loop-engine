"""Every provider the transport starts, and every process it starts in turn, carries the execution role."""
import os
import subprocess
import sys
import unittest

from provider_transport.admitted_process import (
    EXECUTION_ROLE_VARIABLE, PRODUCER_ROLE, launch_admitted, producer_environment,
)
from provider_transport.cwd_authority import ChildLedger, SpawnAdmission
from provider_transport.directory_authority import create_cwd_authority
from provider_transport.testing.cell_fake import FakeCellMembership


class ProducerRoleTests(unittest.TestCase):
    def test_role_is_added_without_dropping_the_user_environment(self):
        environment = producer_environment({'PATH': '/usr/bin', EXECUTION_ROLE_VARIABLE: 'other'})
        self.assertEqual({'PATH': '/usr/bin', EXECUTION_ROLE_VARIABLE: PRODUCER_ROLE}, environment)

    @unittest.skipIf(os.name == 'nt', 'the fake cell starts POSIX sessions')
    def test_launched_provider_and_its_children_carry_the_producer_role(self):
        cell = FakeCellMembership()
        authority = create_cwd_authority()
        self.addCleanup(authority.close)
        admission = SpawnAdmission(
            reservation=ChildLedger(cell_id='cell', cell=cell).reserve(), cwd_lease=authority.admit('.'),
            cell_id='cell', epoch=0, fence_check=lambda _epoch: True, cell_membership=cell)
        process, receipt = launch_admitted(
            (sys.executable, '-c', 'import os,subprocess,sys;'
             'print(os.environ.get(%r));'
             'print(subprocess.run([sys.executable,"-c","import os;print(os.environ.get(%r))"],'
             'capture_output=True,text=True).stdout.strip())' % (EXECUTION_ROLE_VARIABLE, EXECUTION_ROLE_VARIABLE)),
            spawn_admission=admission, host_cell_id='cell',
            options={'stdout': subprocess.PIPE, 'text': True})
        output, _ = process.communicate(timeout=30)
        self.assertEqual(process.pid, receipt.pid)
        self.assertEqual([PRODUCER_ROLE, PRODUCER_ROLE], output.split())


if __name__ == '__main__':
    unittest.main()
