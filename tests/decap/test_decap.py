import json
import logging
from datetime import datetime

import pytest
import requests
from jinja2 import Template
from netaddr import IPNetwork
from ansible.plugins.filter.core import to_bool

from tests.common.fixtures.ptfhost_utils import change_mac_addresses        # lgtm[py/unused-import]
from tests.common.fixtures.ptfhost_utils import remove_ip_addresses         # lgtm[py/unused-import]
from tests.common.fixtures.ptfhost_utils import copy_ptftests_directory     # lgtm[py/unused-import]
from tests.common.fixtures.ptfhost_utils import set_ptf_port_mapping_mode   # lgtm[py/unused-import]
from tests.common.fixtures.ptfhost_utils import ptf_test_port_map
from tests.common.fixtures.fib_utils import fib_info_files
from tests.ptf_runner import ptf_runner
from tests.common.helpers.assertions import pytest_assert as pt_assert
from tests.common.dualtor.mux_simulator_control import mux_server_url
from tests.common.utilities import wait

logger = logging.getLogger(__name__)

PTFRUNNER_QLEN = 1000

pytestmark = [
    pytest.mark.topology('any')
]


@pytest.fixture
def ttl_dscp_params(duthost, supported_ttl_dscp_params):
    if "uniform" in supported_ttl_dscp_params.values() and ("201811" in duthost.os_version or "201911" in duthost.os_version):
        pytest.skip('uniform ttl/dscp mode is available from 202012. Current version is %s' % duthost.os_version)

    return supported_ttl_dscp_params


def remove_default_decap_cfg(duthosts):
    for duthost in duthosts:
        logger.info('Remove default decap cfg on {}'.format(duthost.hostname))
        for asic_id in duthost.get_frontend_asic_ids():
            swss = 'swss{}'.format(asic_id if asic_id is not None else '')
            cmds = [
                'docker exec {} cp /etc/swss/config.d/ipinip.json /default_ipinip.json'.format(swss),
                'docker exec {} sed -i -e \'s/"OP": *"SET"/"OP": "DEL"/g\' /default_ipinip.json'.format(swss),
                'docker exec {} swssconfig /default_ipinip.json'.format(swss),
                'docker exec {} rm /default_ipinip.json'.format(swss)
            ]
            duthost.shell_cmds(cmds=cmds)


def restore_default_decap_cfg(duthosts):
    for duthost in duthosts:
        logger.info('Restore default decap cfg on {}'.format(duthost.hostname))
        for asic_id in duthost.get_frontend_asic_ids():
            swss = 'swss{}'.format(asic_id if asic_id is not None else '')
            cmd = 'docker exec {} swssconfig /etc/swss/config.d/ipinip.json'.format(swss)
            duthost.shell(cmd)


@pytest.fixture(scope='module')
def ip_ver(request):
    return {
        "outer_ipv4": to_bool(request.config.getoption("outer_ipv4")),
        "outer_ipv6": to_bool(request.config.getoption("outer_ipv6")),
        "inner_ipv4": to_bool(request.config.getoption("inner_ipv4")),
        "inner_ipv6": to_bool(request.config.getoption("inner_ipv6")),
    }


@pytest.fixture(scope='module')
def loopback_ips(duthosts, duts_running_config_facts):
    lo_ips = []
    lo_ipv6s = []
    for duthost in duthosts:
        cfg_facts = duts_running_config_facts[duthost.hostname]
        lo_ip = None
        lo_ipv6 = None
        # Loopback0 ip is same on all ASICs
        for addr in cfg_facts[0]["LOOPBACK_INTERFACE"]["Loopback0"]:
            ip = IPNetwork(addr).ip
            if ip.version == 4 and not lo_ip:
                lo_ip = str(ip)
            elif ip.version == 6 and not lo_ipv6:
                lo_ipv6 = str(ip)
        lo_ips.append(lo_ip)
        lo_ipv6s.append(lo_ipv6)
    return {'lo_ips': lo_ips, 'lo_ipv6s': lo_ipv6s}


@pytest.fixture(scope='module')
def setup_teardown(request, duthosts, duts_running_config_facts, ip_ver, loopback_ips, fib_info_files):

    is_multi_asic = duthosts[0].sonichost.is_multi_asic

    setup_info = {
        "fib_info_files": fib_info_files[:3],  # Test at most 3 DUTs in case of multi-DUT
        "ignore_ttl": True if is_multi_asic else False,
        "max_internal_hops": 3 if is_multi_asic else 0,
        'router_macs': [duthost.facts['router_mac'] for duthost in duthosts]
    }

    setup_info.update(ip_ver)
    setup_info.update(loopback_ips)
    logger.info(json.dumps(setup_info, indent=2))

    # Remove default tunnel
    remove_default_decap_cfg(duthosts)

    yield setup_info

    # Restore default tunnel
    restore_default_decap_cfg(duthosts)


def apply_decap_cfg(duthosts, ip_ver, loopback_ips, ttl_mode, dscp_mode, ecn_mode, op):

    decap_conf_template = Template(open("../ansible/roles/test/templates/decap_conf.j2").read())

    # apply test decap configuration (SET or DEL)
    for idx, duthost in enumerate(duthosts):
        decap_conf_vars = {
            'lo_ip': loopback_ips['lo_ips'][idx],
            'lo_ipv6': loopback_ips['lo_ipv6s'][idx],
            'ttl_mode': ttl_mode,
            'dscp_mode': dscp_mode,
            'ecn_mode': ecn_mode,
            'op': op,
        }
        decap_conf_vars.update(ip_ver)
        duthost.copy(
            content=decap_conf_template.render(**decap_conf_vars),
            dest='/tmp/decap_conf_{}.json'.format(op))

        for asic_id in duthost.get_frontend_asic_ids():
            swss = 'swss{}'.format(asic_id if asic_id is not None else '')
            cmds = [
                'docker cp /tmp/decap_conf_{}.json {}:/decap_conf_{}.json'.format(op, swss, op),
                'docker exec {} swssconfig /decap_conf_{}.json'.format(swss, op),
                'docker exec {} rm /decap_conf_{}.json'.format(swss, op)
            ]
            duthost.shell_cmds(cmds=cmds)
        duthost.shell('rm /tmp/decap_conf_{}.json'.format(op))


@pytest.fixture
def decap_config(duthosts, ttl_dscp_params, ip_ver, loopback_ips):
    ecn_mode = "copy_from_outer"
    ttl_mode = ttl_dscp_params['ttl']
    dscp_mode = ttl_dscp_params['dscp']
    if duthosts[0].facts['asic_type'] in ['mellanox']:
        ecn_mode = 'standard'

    # Add test decap configuration
    apply_decap_cfg(duthosts, ip_ver, loopback_ips, ttl_mode, dscp_mode, ecn_mode, 'SET')

    yield ttl_mode, dscp_mode

    # Remove test decap configuration
    apply_decap_cfg(duthosts, ip_ver, loopback_ips, ttl_mode, dscp_mode, ecn_mode, 'DEL')


def set_mux_side(tbinfo, mux_server_url, side):
    if 'dualtor' in tbinfo['topo']['name']:
        res = requests.post(mux_server_url, json={"active_side": side})
        pt_assert(res.status_code==200, 'Failed to set active side: {}'.format(res.text))
        return res.json()   # Response is new mux_status of all mux Y-cables.
    return {}


@pytest.fixture
def set_mux_random(tbinfo, mux_server_url):
    return set_mux_side(tbinfo, mux_server_url, 'random')


def test_decap(tbinfo, duthosts, ptfhost, setup_teardown, decap_config, mux_server_url, set_mux_random):

    setup_info = setup_teardown

    ttl_mode, dscp_mode = decap_config

    if 'dualtor' in tbinfo['topo']['name']:
        wait(30, 'Wait some time for mux active/standby state to be stable after toggled mux state')

    log_file = "/tmp/decap.{}.log".format(datetime.now().strftime('%Y-%m-%d-%H:%M:%S'))
    ptf_runner(ptfhost,
               "ptftests",
               "IP_decap_test.DecapPacketTest",
                platform_dir="ptftests",
                params={"outer_ipv4": setup_info["outer_ipv4"],
                        "outer_ipv6": setup_info["outer_ipv6"],
                        "inner_ipv4": setup_info["inner_ipv4"],
                        "inner_ipv6": setup_info["inner_ipv6"],
                        "lo_ips": setup_info["lo_ips"],
                        "lo_ipv6s": setup_info["lo_ipv6s"],
                        "router_macs": setup_info["router_macs"],
                        "ttl_mode": ttl_mode,
                        "dscp_mode": dscp_mode,
                        "ignore_ttl": setup_info["ignore_ttl"],
                        "max_internal_hops": setup_info["max_internal_hops"],
                        "fib_info_files": setup_info["fib_info_files"],
                        "ptf_test_port_map": ptf_test_port_map(ptfhost, tbinfo, duthosts, mux_server_url)
                        },
                qlen=PTFRUNNER_QLEN,
                log_file=log_file)
