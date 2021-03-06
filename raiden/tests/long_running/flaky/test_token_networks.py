# -*- coding: utf-8 -*-
import logging

import pytest
import gevent
from flaky import flaky

from raiden.api.python import RaidenAPI
from raiden.tests.utils.blockchain import wait_until_block
from raiden.transfer.state import CHANNEL_STATE_SETTLED

log = logging.getLogger(__name__)


# TODO: add test scenarios for
# - subsequent `connect()` calls with different `funds` arguments
# - `connect()` calls with preexisting channels

@flaky
@pytest.mark.parametrize('number_of_nodes', [6])
@pytest.mark.parametrize('channels_per_node', [0])
@pytest.mark.parametrize('cached_genesis', [False])
@pytest.mark.parametrize('register_tokens', [True, False])
@pytest.mark.parametrize('settle_timeout', [6])
@pytest.mark.parametrize('reveal_timeout', [3])
def test_participant_selection(raiden_network, token_addresses):
    # pylint: disable=too-many-locals
    token_address = token_addresses[0]

    # connect the first node (will register the token if necessary)
    RaidenAPI(raiden_network[0].raiden).connect_token_network(token_address, 100)

    # connect the other nodes
    connect_greenlets = [
        gevent.spawn(RaidenAPI(app.raiden).connect_token_network, token_address, 100)
        for app in raiden_network[1:]
    ]
    gevent.wait(connect_greenlets)

    # wait some blocks to let the network connect
    wait_blocks = 15
    for _ in range(wait_blocks):
        for app in raiden_network:
            wait_until_block(
                app.raiden.chain,
                app.raiden.chain.block_number() + 1
            )

    connection_managers = [
        app.raiden.connection_manager_for_token(token_address) for app in raiden_network
    ]

    def open_channels_count(connection_managers_):
        return [
            connection_manager.open_channels for connection_manager in connection_managers_
        ]

    assert all(open_channels_count(connection_managers))

    def not_saturated(connection_managers):
        return [
            1 for connection_manager in connection_managers
            if connection_manager.open_channels < connection_manager.initial_channel_target
        ]

    chain = raiden_network[-1].raiden.chain
    max_wait = 12

    while not_saturated(connection_managers) and max_wait > 0:
        wait_until_block(chain, chain.block_number() + 1)
        max_wait -= 1

    assert not not_saturated(connection_managers)

    # Ensure unpartitioned network
    addresses = [app.raiden.address for app in raiden_network]
    for connection_manager in connection_managers:
        assert all(
            connection_manager.channelgraph.has_path(
                connection_manager.raiden.address,
                address
            )
            for address in addresses
        )

    # average channel count
    acc = (
        sum(len(connection_manager.open_channels) for connection_manager in connection_managers) /
        float(len(connection_managers))
    )

    try:
        # FIXME: depending on the number of channels, this will fail, due to weak
        # selection algorithm
        # https://github.com/raiden-network/raiden/issues/576
        assert not any(
            len(connection_manager.open_channels) > 2 * acc
            for connection_manager in connection_managers
        )
    except AssertionError:
        pass

    # create a transfer to the leaving node, so we have a channel to settle
    sender = raiden_network[-1].raiden
    receiver = raiden_network[0].raiden

    # assert there is a direct channel receiver -> sender (vv)
    receiver_channel = RaidenAPI(receiver).get_channel_list(
        token_address=token_address,
        partner_address=sender.address
    )
    assert len(receiver_channel) == 1
    receiver_channel = receiver_channel[0]
    assert receiver_channel.external_state.opened_block != 0
    assert not receiver_channel.received_transfers

    # assert there is a direct channel sender -> receiver
    sender_channel = RaidenAPI(sender).get_channel_list(
        token_address=token_address,
        partner_address=receiver.address
    )
    assert len(sender_channel) == 1
    sender_channel = sender_channel[0]
    assert sender_channel.can_transfer
    assert sender_channel.external_state.opened_block != 0

    RaidenAPI(sender).transfer_and_wait(
        token_address,
        1,
        receiver.address
    )

    # now receiver has a transfer
    assert len(receiver_channel.received_transfers)

    # test `leave()` method
    connection_manager = connection_managers[0]
    before = len(connection_manager.receiving_channels)

    timeout = (
        connection_manager.min_settle_blocks *
        connection_manager.raiden.chain.estimate_blocktime() *
        5
    )

    assert timeout > 0
    with gevent.timeout.Timeout(timeout):
        try:
            RaidenAPI(raiden_network[0].raiden).leave_token_network(token_address)
        except gevent.timeout.Timeout:
            log.error('timeout while waiting for leave')

    before_block = connection_manager.raiden.chain.block_number()
    wait_blocks = connection_manager.min_settle_blocks + 10
    wait_until_block(
        connection_manager.raiden.chain,
        before_block + wait_blocks
    )
    assert connection_manager.raiden.chain.block_number >= before_block + wait_blocks
    wait_until_block(
        receiver.chain,
        before_block + wait_blocks
    )
    while receiver_channel.state != CHANNEL_STATE_SETTLED:
        gevent.sleep(receiver.alarm.wait_time)
    after = len(connection_manager.receiving_channels)

    assert before > after
    assert after == 0
