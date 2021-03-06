import asyncio
import json
import logging
from time import time

import aiohttp
from aiohttp import web
from aiohttp.web import WebSocketResponse
import aiohttp_session

from broadcast import lobby_broadcast
from const import STARTED
from seek import challenge, create_seek, get_seeks, Seek
from user import User
from utils import new_game, load_game

log = logging.getLogger(__name__)


async def is_playing(request, user, ws):
    # Prevent users to start new games if they have an unfinished one
    if user.game_in_progress is not None:
        game = await load_game(request.app, user.game_in_progress)
        if (game is None) or game.status > STARTED:
            user.game_in_progress = None
            return False
        else:
            response = {"type": "game_in_progress", "gameId": user.game_in_progress}
            await ws.send_json(response)
            return True
    else:
        return False


async def lobby_socket_handler(request):

    users = request.app["users"]
    sockets = request.app["lobbysockets"]
    seeks = request.app["seeks"]

    ws = WebSocketResponse()

    ws_ready = ws.can_prepare(request)
    if not ws_ready.ok:
        raise web.HTTPFound("/")

    await ws.prepare(request)

    session = await aiohttp_session.get_session(request)
    session_user = session.get("user_name")
    user = users[session_user] if session_user is not None and session_user in users else None

    lobby_ping_task = None
    ping_counter = [0]

    async def lobby_pinger(ping_counter):
        """ Prevent Heroku to close inactive ws """
        while not ws.closed:
            if ping_counter[0] > 2:
                await ws.close(code=1009)

                if ws in user.lobby_sockets:
                    user.lobby_sockets.remove(ws)

                # online user counter will be updated in quit_lobby also!
                if len(user.lobby_sockets) == 0:
                    # log.info("%s went offline (PINGER)" % user.username)
                    await user.clear_seeks(sockets, seeks)
                    await user.quit_lobby(sockets, disconnect=True)
                    break

            if user.bot:
                await user.event_queue.put("\n")
                # heroku needs something at least in 50 sec not to close BOT connections (stream events) on server side
            else:
                await ws.send_json({"type": "ping", "timestamp": "%s" % time()})
            await asyncio.sleep(3)
            ping_counter[0] += 1

    log.debug("-------------------------- NEW lobby WEBSOCKET by %s" % user)

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            if type(msg.data) == str:
                if msg.data == "close":
                    log.debug("Got 'close' msg.")
                    break
                else:
                    data = json.loads(msg.data)
                    if not data["type"] == "pong":
                        log.debug("Websocket (%s) message: %s" % (id(ws), msg))

                    if data["type"] == "pong":
                        ping_counter[0] -= 1

                    elif data["type"] == "get_seeks":
                        response = get_seeks(seeks)
                        await ws.send_json(response)

                    elif data["type"] == "create_ai_challenge":
                        no = await is_playing(request, user, ws)
                        if no:
                            continue

                        variant = data["variant"]
                        engine = users.get("Fairy-Stockfish")

                        if engine is None or not engine.online():
                            # TODO: message that engine is offline, but capture BOT will play instead
                            engine = users.get("Random-Mover")

                        seek = Seek(
                            user, variant,
                            fen=data["fen"],
                            color=data["color"],
                            base=data["minutes"],
                            inc=data["increment"],
                            byoyomi_period=data["byoyomi_period"],
                            level=data["level"],
                            rated=data["rated"],
                            chess960=data["chess960"],
                            handicap=data["handicap"])
                        # print("SEEK", user, variant, data["fen"], data["color"], data["minutes"], data["increment"], data["level"], False, data["chess960"])
                        seeks[seek.id] = seek

                        response = await new_game(request.app, engine, seek.id)
                        await ws.send_json(response)

                        if response["type"] != "error":
                            gameId = response["gameId"]
                            engine.game_queues[gameId] = asyncio.Queue()
                            await engine.event_queue.put(challenge(seek, response))

                    elif data["type"] == "create_seek":
                        no = await is_playing(request, user, ws)
                        if no:
                            continue

                        print("create_seek", data)
                        create_seek(seeks, user, data, ws)
                        await lobby_broadcast(sockets, get_seeks(seeks))

                        if data.get("target"):
                            queue = users[data["target"]].notify_queue
                            if queue is not None:
                                await queue.put(json.dumps({"notify": "new_challenge"}))

                    elif data["type"] == "delete_seek":
                        del seeks[data["seekID"]]
                        del user.seeks[data["seekID"]]

                        await lobby_broadcast(sockets, get_seeks(seeks))

                    elif data["type"] == "accept_seek":
                        no = await is_playing(request, user, ws)
                        if no:
                            continue

                        if data["seekID"] not in seeks:
                            continue

                        seek = seeks[data["seekID"]]
                        # print("accept_seek", seek.as_json)
                        response = await new_game(request.app, user, data["seekID"])
                        await ws.send_json(response)

                        if seek.user.bot:
                            gameId = response["gameId"]
                            seek.user.game_queues[gameId] = asyncio.Queue()
                            await seek.user.event_queue.put(challenge(seek, response))
                        else:
                            await seek.ws.send_json(response)

                        # Inform others, new_game() deleted accepted seek allready.
                        await lobby_broadcast(sockets, get_seeks(seeks))

                    elif data["type"] == "lobby_user_connected":
                        if session_user is not None:
                            if data["username"] and data["username"] != session_user:
                                log.info("+++ Existing lobby_user %s socket connected as %s." % (session_user, data["username"]))
                                session_user = data["username"]
                                if session_user in users:
                                    user = users[session_user]
                                else:
                                    user = User(request.app, username=data["username"], anon=data["username"].startswith("Anon-"))
                                    users[user.username] = user
                                response = {"type": "lobbychat", "user": "", "message": "%s joined the lobby" % session_user}
                            else:
                                if session_user in users:
                                    user = users[session_user]
                                else:
                                    user = User(request.app, username=data["username"], anon=data["username"].startswith("Anon-"))
                                    users[user.username] = user
                                response = {"type": "lobbychat", "user": "", "message": "%s joined the lobby" % session_user}
                        else:
                            log.info("+++ Existing lobby_user %s socket reconnected." % data["username"])
                            session_user = data["username"]
                            if session_user in users:
                                user = users[session_user]
                            else:
                                user = User(request.app, username=data["username"], anon=data["username"].startswith("Anon-"))
                                users[user.username] = user
                            response = {"type": "lobbychat", "user": "", "message": "%s rejoined the lobby" % session_user}

                        ping_counter[0] = 0
                        await lobby_broadcast(sockets, response)

                        # update websocket
                        user.lobby_sockets.add(ws)
                        sockets[user.username] = user.lobby_sockets

                        response = {"type": "lobby_user_connected", "username": user.username}
                        await ws.send_json(response)

                        response = {"type": "fullchat", "lines": list(request.app["chat"])}
                        await ws.send_json(response)

                        loop = asyncio.get_event_loop()
                        lobby_ping_task = loop.create_task(lobby_pinger(ping_counter))

                        # send game count
                        response = {"type": "g_cnt", "cnt": request.app["g_cnt"]}
                        await ws.send_json(response)

                        # send user count
                        if len(user.game_sockets) == 0:
                            # not connected to any game socket but connected to lobby socket
                            if len(user.lobby_sockets) == 1:
                                request.app["u_cnt"] += 1
                            response = {"type": "u_cnt", "cnt": request.app["u_cnt"]}
                            await lobby_broadcast(sockets, response)
                        else:
                            response = {"type": "u_cnt", "cnt": request.app["u_cnt"]}
                            await ws.send_json(response)

                    elif data["type"] == "lobbychat":
                        response = {"type": "lobbychat", "user": user.username, "message": data["message"]}
                        await lobby_broadcast(sockets, response)
                        request.app["chat"].append(response)

                    elif data["type"] == "logout":
                        await ws.close()

                    elif data["type"] == "disconnect":
                        # Used only to test socket disconnection...
                        await ws.close(code=1009)

            else:
                log.debug("type(msg.data) != str %s" % msg)
        elif msg.type == aiohttp.WSMsgType.ERROR:
            log.debug("!!! Lobby ws connection closed with exception %s" % ws.exception())
        else:
            log.debug("other msg.type %s %s" % (msg.type, msg))

    log.info("--- Lobby Websocket %s closed" % id(ws))

    if user is not None:
        if ws in user.lobby_sockets:
            user.lobby_sockets.remove(ws)

        # online user counter will be updated in quit_lobby also!
        if len(user.lobby_sockets) == 0:
            await user.clear_seeks(sockets, seeks)
            await user.quit_lobby(sockets, disconnect=False)

    if lobby_ping_task is not None:
        lobby_ping_task.cancel()

    return ws
