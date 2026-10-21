import asyncio
import pytest
from rumi_market.game.handshake import GameHandshake
from rumi_market.game.inventory_state import InventoryState
from rumi_market.game.order_state import OrderState
from rumi_market.game.order_codec import OrderProtoCodec
from test_game_handshake import complete_server
from test_game_client import MemoryTransport
from test_game_login import profile


async def test_single_login_receiver_observes_inventory_and_orders_and_closes_both():
    transport = MemoryTransport()
    inventory, orders = InventoryState(True), OrderState(True)
    handshake = GameHandshake(transport, inventory_state=inventory, order_state=orders)
    server = asyncio.create_task(complete_server(transport))
    await handshake.start(profile())
    await server
    codec = OrderProtoCodec()
    transport.respond(3301, 0, codec.encode(3301, order_archi={'slot_list': []}), notify=1)
    for _ in range(10):
        if orders.snapshot()['valid']:
            break
        await asyncio.sleep(0)
    assert orders.snapshot()['valid']
    assert transport.connections == 1
    await handshake.close()
    assert not orders.snapshot()['valid'] and not inventory.snapshot()['valid']
