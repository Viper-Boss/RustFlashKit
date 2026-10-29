# SPDX-License-Identifier: GPL-3.0-only
import pytest

from usbtool import guard

HEAD = '<?xml version="1.0" ?>'


def read_xml(start, count, extra=''):
    return (f'{HEAD}<data><read SECTOR_SIZE_IN_BYTES="512" num_partition_sectors="{count}" '
            f'physical_partition_number="0" start_sector="{start}"{extra}/>\n</data>')


@pytest.fixture
def policy():
    p = guard.ReadOnlyPolicy()
    p.disk_sectors = 1000
    return p


@pytest.mark.parametrize('xml', [
    f'{HEAD}<data><configure MemoryName="eMMC" SkipWrite="1" MaxPayloadSizeToTargetInBytes="32768"/></data>',
    read_xml(0, 34), read_xml(967, 33),
    f'{HEAD}<data><getstorageinfo physical_partition_number="0"/></data>',
    f'{HEAD}<data><nop/></data>',
])
def test_read_only_operations_allowed(policy, xml):
    policy.check(xml)


@pytest.mark.parametrize('xml', [
    f'{HEAD}<data><program SECTOR_SIZE_IN_BYTES="512" num_partition_sectors="1" physical_partition_number="0" start_sector="0"/></data>',
    f'{HEAD}<data><erase SECTOR_SIZE_IN_BYTES="512" num_partition_sectors="1" physical_partition_number="0" start_sector="0"/></data>',
    f'{HEAD}<data><patch byte_offset="0" filename="DISK" physical_partition_number="0" size_in_bytes="8" start_sector="0" value="0"/></data>',
    f'{HEAD}<data><configure MemoryName="eMMC" SkipWrite="0"/></data>',
    f'{HEAD}<data><configure MemoryName="UFS" SkipWrite="1"/></data>',
    f'{HEAD}<data><power value="reset"/></data>',
    f'{HEAD}<data><power value="edl"/></data>',
    f'{HEAD}<data><peek address64="0" SizeInBytes="4"/></data>',
    read_xml(990, 33), read_xml(0, 0), read_xml(0, 1, ' extra="1"'),
    f'{HEAD}<data><nop/><nop/></data>',
    f'{HEAD}<!DOCTYPE x [<!ENTITY a "b">]><data><nop/></data>',
])
def test_mutating_or_out_of_range_refused(policy, xml):
    with pytest.raises(guard.RefusedOperation):
        policy.check(xml)


def test_reset_only_once_when_enabled(policy):
    reset = f'{HEAD}<data><power value="reset"/></data>'
    policy.allow_reset = True
    policy.check(reset)
    with pytest.raises(guard.RefusedOperation):
        policy.check(reset)


def test_install_patches_firehose_class(policy):
    sent = []

    class FakeFirehose:
        def xmlsend(self, data, *args, **kwargs):
            sent.append(data)
            return 'ok'

        def cmd_program(self, *a):
            return True

        def cmd_erase(self, *a):
            return True

    guard.install(FakeFirehose, policy)
    fh = FakeFirehose()
    assert fh.xmlsend(read_xml(0, 1)) == 'ok' and len(sent) == 1
    with pytest.raises(guard.RefusedOperation):
        fh.xmlsend(read_xml(5000, 1))
    assert len(sent) == 1  # 被拒绝的命令没有发出
    for name in guard.MUTATING_METHODS:
        with pytest.raises(guard.RefusedOperation):
            getattr(fh, name)(0, 0, 0)


def test_real_edl_firehose_methods_are_all_covered():
    import sys
    old = sys.argv
    sys.argv = ['edl.py', 'printgpt']
    try:
        from edlclient.Library.firehose import firehose
    finally:
        sys.argv = old
    for name in guard.MUTATING_METHODS:
        assert hasattr(firehose, name), name
