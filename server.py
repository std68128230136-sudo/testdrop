import argparse
import asyncio
import json
import uuid
import logging
import time
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Store rooms and peers
# rooms = {
#     "room_id": {
#         "peers": {
#             "ws_id": ws_connection
#         }
#     }
# }
rooms = {}

async def cleanup_rooms(app):
    """Background task to remove expired rooms"""
    try:
        while True:
            await asyncio.sleep(60)  # Check every minute
            now = time.time()
            rooms_to_delete = []
            
            for room_id, room_data in rooms.items():
                if 'expires_at' in room_data and now > room_data['expires_at']:
                    rooms_to_delete.append(room_id)
            
            for room_id in rooms_to_delete:
                logger.info(f"Room {room_id} expired and removed")
                if room_id in rooms:
                    for pid, p_ws in rooms[room_id]['peers'].items():
                        await p_ws.send_json({'type': 'error', 'message': 'Room expired'})
                        await p_ws.close()
                    del rooms[room_id]

    except asyncio.CancelledError:
        pass

async def start_background_tasks(app):
    app['cleanup_task'] = asyncio.create_task(cleanup_rooms(app))

async def cleanup_background_tasks(app):
    app['cleanup_task'].cancel()
    await app['cleanup_task']

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    peer_id = str(uuid.uuid4())
    current_room_id = None
    
    logger.info(f"New connection: {peer_id}")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                action = data.get('action')

                if action == 'create_room':
                    room_id = data.get('room_id')
                    config = data.get('config', {})
                    
                    if room_id in rooms:
                        pass
                    
                    # Default TTL: 24 hours (1440 mins)
                    ttl_minutes = int(config.get('ttl', 1440))
                    max_peers = int(config.get('max_peers', 0)) # 0 = unlimited
                    
                    rooms[room_id] = {
                        'peers': {},
                        'created_at': time.time(),
                        'expires_at': time.time() + (ttl_minutes * 60),
                        'max_peers': max_peers
                    }
                    rooms[room_id]['peers'][peer_id] = ws
                    current_room_id = room_id
                    
                    logger.info(f"Room created: {room_id} by {peer_id} (TTL: {ttl_minutes}m, MaxPeers: {max_peers})")
                    await ws.send_json({
                        'type': 'room_created',
                        'room_id': room_id,
                        'peer_id': peer_id
                    })

                elif action == 'join_room':
                    room_id = data.get('room_id')
                    
                    if room_id in rooms:
                        room = rooms[room_id]
                        
                        # Check expiration
                        if 'expires_at' in room and time.time() > room['expires_at']:
                            await ws.send_json({'type': 'error', 'message': 'Room expired'})
                            continue
                        
                        # Check Max Peers (One-time access)
                        max_peers = room.get('max_peers', 0)
                        if max_peers > 0:
                            # If max_peers is 2, it means only 1 additional peer (one-time access)
                            # Creator is already in the room
                            if len(room['peers']) >= max_peers:
                                await ws.send_json({'type': 'error', 'message': 'Room is full (One-time access limit reached)'})
                                continue
                        
                        # Mark room as accessed if one-time access
                        if max_peers == 2:  # One-time access mode
                            room['accessed'] = True
                            room['access_count'] = room.get('access_count', 0) + 1

                        room['peers'][peer_id] = ws
                        current_room_id = room_id
                        
                        logger.info(f"Peer {peer_id} joined room {room_id} (Access count: {room.get('access_count', 0)})")
                        
                        # Notify other peers in the room
                        for pid, p_ws in rooms[room_id]['peers'].items():
                            if pid != peer_id:
                                await p_ws.send_json({
                                    'type': 'peer_joined',
                                    'peer_id': peer_id
                                })
                        
                        await ws.send_json({
                            'type': 'room_joined',
                            'room_id': room_id,
                            'peer_id': peer_id,
                            'expires_at': room.get('expires_at', 0)  # ส่งเวลาหมดอายุให้ joiner
                        })
                    else:
                        await ws.send_json({'type': 'error', 'message': 'Room not found or expired'})

                elif action == 'signal':
                    target_peer_id = data.get('target_peer_id')
                    room_id = current_room_id
                    
                    if room_id and target_peer_id and target_peer_id in rooms[room_id]['peers']:
                        target_ws = rooms[room_id]['peers'][target_peer_id]
                        await target_ws.send_json({
                            'type': 'signal',
                            'sender_peer_id': peer_id,
                            'data': data.get('data')
                        })
                        logger.info(f"Signal relayed from {peer_id} to {target_peer_id}")

            elif msg.type == WSMsgType.ERROR:
                logger.error(f'ws connection closed with exception {ws.exception()}')

    finally:
        if current_room_id and current_room_id in rooms:
            if peer_id in rooms[current_room_id]['peers']:
                del rooms[current_room_id]['peers'][peer_id]
                logger.info(f"Peer {peer_id} left room {current_room_id}")
                
                # Notify remaining peers
                if not rooms[current_room_id]['peers']:
                    del rooms[current_room_id]
                    logger.info(f"Room {current_room_id} deleted (empty)")
                else:
                    for pid, p_ws in rooms[current_room_id]['peers'].items():
                        await p_ws.send_json({
                            'type': 'peer_left',
                            'peer_id': peer_id
                        })

    return ws

async def index(request):
    return web.FileResponse('Frontend.html')

def main():
    parser = argparse.ArgumentParser(description="Drop2Me Signaling Server")
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on')
    args = parser.parse_args()

    app = web.Application()
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    app.router.add_get('/', index)
    app.router.add_get('/ws', websocket_handler)
    
    print(f"Starting server at http://localhost:{args.port}")
    web.run_app(app, port=args.port)

if __name__ == '__main__':
    main()
